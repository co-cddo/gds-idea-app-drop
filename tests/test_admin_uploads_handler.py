"""Unit tests for the admin uploads Lambda handler."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from botocore.exceptions import ClientError
from cognito_auth.exceptions import (
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
)

from backend_src.admin_uploads import handler as admin

_UUID = "123e4567-e89b-12d3-a456-426614174000"
_KEY = f"uploads/2026/01/02/{_UUID}/report.pdf"


def _event(path="/api/admin/uploads", *, method="GET", query=None):
    return {
        "httpMethod": method,
        "path": path,
        "headers": {},
        "queryStringParameters": query,
    }


def _user(groups=("gds-idea",)):
    return SimpleNamespace(
        email="admin@example.gov.uk",
        sub="sub-admin",
        is_gds_idea="gds-idea" in groups,
    )


@pytest.fixture
def as_admin():
    with patch.object(admin._auth, "get_auth_user", return_value=_user()) as m:
        yield m


def _obj(n, *, key=None):
    return {
        "Key": key or f"uploads/2026/01/02/{_UUID[:-1]}{n}/file{n}.txt",
        "Size": 10 * n,
        "LastModified": datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=n),
    }


def _patch_listing(objects):
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": objects}]
    return patch.object(admin.s3_client, "get_paginator", return_value=paginator)


def _head(**metadata):
    return {"Metadata": metadata, "ContentType": "text/plain"}


def _body(response):
    return json.loads(response["body"])


# --- authentication / authorisation ---------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        MissingTokenError("x"),
        InvalidTokenError("x"),
        ExpiredTokenError("x"),
        RuntimeError("boom"),
    ],
)
def test_unverifiable_identity_is_401_and_touches_no_s3(exc):
    with (
        patch.object(admin._auth, "get_auth_user", side_effect=exc),
        patch.object(admin, "s3_client") as s3,
    ):
        response = admin.handler(_event(), None)

    assert response["statusCode"] == 401
    s3.get_paginator.assert_not_called()
    s3.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize("groups", [(), ("some-other-group",), ("gds-idea-ish",)])
def test_non_gds_idea_user_is_403_and_touches_no_s3(groups):
    with (
        patch.object(admin._auth, "get_auth_user", return_value=_user(groups)),
        patch.object(admin, "s3_client") as s3,
    ):
        for path in ("/api/admin/uploads", "/api/admin/download"):
            response = admin.handler(_event(path, query={"key": _KEY}), None)
            assert response["statusCode"] == 403

    s3.get_paginator.assert_not_called()
    s3.head_object.assert_not_called()
    s3.generate_presigned_url.assert_not_called()


def test_rejects_non_get_methods(as_admin):
    response = admin.handler(_event(method="POST"), None)
    assert response["statusCode"] == 405


def test_unknown_path_is_404(as_admin):
    response = admin.handler(_event("/api/admin/other"), None)
    assert response["statusCode"] == 404


def test_responses_are_never_cacheable(as_admin):
    response = admin.handler(_event("/api/admin/other"), None)
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"


def test_unexpected_error_is_500_without_details(as_admin):
    with patch.object(
        admin.s3_client, "get_paginator", side_effect=RuntimeError("secret detail")
    ):
        response = admin.handler(_event(), None)

    assert response["statusCode"] == 500
    assert "secret detail" not in response["body"]


# --- listing ---------------------------------------------------------------


def test_lists_newest_first_with_uploader_metadata(as_admin):
    with (
        _patch_listing([_obj(1), _obj(2)]),
        patch.object(
            admin.s3_client,
            "head_object",
            side_effect=lambda Bucket, Key: _head(
                **{
                    "uploaded-by-email": "u@example.gov.uk",
                    "uploaded-by-name": "U Ser",
                    "uploaded-at": "2026-01-02T00:00:00+00:00",
                }
            ),
        ),
    ):
        response = admin.handler(_event(), None)

    assert response["statusCode"] == 200
    body = _body(response)
    assert [i["filename"] for i in body["items"]] == ["file2.txt", "file1.txt"]
    first = body["items"][0]
    assert first["uploadedByEmail"] == "u@example.gov.uk"
    assert first["uploadedByName"] == "U Ser"
    assert first["uploadedAt"] == "2026-01-02T00:00:00+00:00"
    assert first["contentType"] == "text/plain"
    assert first["size"] == 20
    assert body["nextCursor"] is None
    # Data minimisation: the uploader's sub is not exposed.
    assert "sub" not in json.dumps(body).lower()


def test_unattributed_upload_has_null_uploader(as_admin):
    with (
        _patch_listing([_obj(1)]),
        patch.object(admin.s3_client, "head_object", return_value=_head()),
    ):
        body = _body(admin.handler(_event(), None))

    item = body["items"][0]
    assert item["uploadedByEmail"] is None
    assert item["uploadedByName"] is None
    # Falls back to S3's LastModified.
    assert item["uploadedAt"].startswith("2026-01-02T00:01")


def test_head_failure_still_lists_the_object(as_admin):
    err = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    with (
        _patch_listing([_obj(1)]),
        patch.object(admin.s3_client, "head_object", side_effect=err),
    ):
        response = admin.handler(_event(), None)

    assert response["statusCode"] == 200
    assert _body(response)["items"][0]["filename"] == "file1.txt"


def test_paginates_with_stable_cursor(as_admin):
    objects = [_obj(n) for n in range(1, 6)]  # file5 is newest
    with (
        _patch_listing(objects),
        patch.object(admin.s3_client, "head_object", return_value=_head()),
    ):
        page1 = _body(admin.handler(_event(query={"limit": "2"}), None))
        assert [i["filename"] for i in page1["items"]] == ["file5.txt", "file4.txt"]
        assert page1["nextCursor"]

        # A newer upload arriving between pages must not shift page 2.
        objects.append(_obj(6))
        page2 = _body(
            admin.handler(
                _event(
                    query={"limit": "2", "cursor": quote(page1["nextCursor"], safe="")}
                ),
                None,
            )
        )
        assert [i["filename"] for i in page2["items"]] == ["file3.txt", "file2.txt"]

        page3 = _body(
            admin.handler(
                _event(query={"limit": "2", "cursor": page2["nextCursor"]}), None
            )
        )
        assert [i["filename"] for i in page3["items"]] == ["file1.txt"]
        assert page3["nextCursor"] is None


@pytest.mark.parametrize("limit", ["0", "-1", "abc", str(admin.MAX_PAGE_SIZE + 1)])
def test_rejects_bad_limit(as_admin, limit):
    response = admin.handler(_event(query={"limit": limit}), None)
    assert response["statusCode"] == 400


def test_rejects_bad_cursor(as_admin):
    response = admin.handler(_event(query={"cursor": "not-a-cursor"}), None)
    assert response["statusCode"] == 400


def test_fails_loudly_when_listing_is_too_large(as_admin):
    with (
        patch.object(admin, "MAX_LISTED_KEYS", 2),
        _patch_listing([_obj(1), _obj(2), _obj(3)]),
    ):
        response = admin.handler(_event(), None)

    assert response["statusCode"] == 500


# --- download --------------------------------------------------------------


def test_download_returns_short_lived_attachment_url(as_admin):
    with (
        patch.object(admin.s3_client, "head_object", return_value=_head()),
        patch.object(
            admin.s3_client, "generate_presigned_url", return_value="https://s3/signed"
        ) as sign,
    ):
        response = admin.handler(
            _event("/api/admin/download", query={"key": quote(_KEY, safe="")}), None
        )

    assert response["statusCode"] == 200
    assert _body(response) == {"url": "https://s3/signed"}

    args, kwargs = sign.call_args
    assert args == ("get_object",)
    assert kwargs["ExpiresIn"] == admin.DOWNLOAD_URL_EXPIRY_SECONDS <= 60
    params = kwargs["Params"]
    assert params["Key"] == _KEY
    assert params["ResponseContentDisposition"] == 'attachment; filename="report.pdf"'
    assert params["ResponseContentType"] == "application/octet-stream"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "report.pdf",
        f"other/2026/01/02/{_UUID}/report.pdf",
        f"uploads/2026/01/02/{_UUID}/../../secret",
        f"uploads/../uploads/2026/01/02/{_UUID}/x.txt",
        "uploads/2026/01/02/not-a-uuid/report.pdf",
        f"uploads/2026/01/02/{_UUID}/",
        f"uploads/2026/01/02/{_UUID}/a/b.txt",
        f'uploads/2026/01/02/{_UUID}/a"b.txt',
        f"uploads/2026/01/02/{_UUID}/a\r\nb.txt",
    ],
)
def test_download_rejects_invalid_keys_without_calling_s3(as_admin, key):
    with patch.object(admin, "s3_client") as s3:
        response = admin.handler(
            _event("/api/admin/download", query={"key": key}), None
        )

    assert response["statusCode"] == 400
    s3.head_object.assert_not_called()
    s3.generate_presigned_url.assert_not_called()


def test_download_of_missing_object_is_404(as_admin):
    err = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    with (
        patch.object(admin.s3_client, "head_object", side_effect=err),
        patch.object(admin.s3_client, "generate_presigned_url") as sign,
    ):
        response = admin.handler(
            _event("/api/admin/download", query={"key": _KEY}), None
        )

    assert response["statusCode"] == 404
    sign.assert_not_called()


def test_download_is_audit_logged(as_admin):
    with (
        patch.object(admin.s3_client, "head_object", return_value=_head()),
        patch.object(admin.s3_client, "generate_presigned_url", return_value="u"),
        patch.object(admin.logger, "info") as info,
    ):
        admin.handler(_event("/api/admin/download", query={"key": _KEY}), None)

    info.assert_called_once()
    assert info.call_args.args[0] == "admin_download"
    assert info.call_args.kwargs["key"] == _KEY
    assert info.call_args.kwargs["admin_email"] == "admin@example.gov.uk"
