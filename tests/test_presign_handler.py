"""Unit tests for the presigned-POST Lambda handler."""

import json
from unittest.mock import patch

import pytest
from cognito_auth.exceptions import ExpiredTokenError, MissingTokenError
from cognito_auth.user import User

from backend_src.presign import handler as presign


def _alb_event(*, method="POST", body=None, is_base64=False, headers=None):
    return {
        "httpMethod": method,
        "path": "/api/presign",
        "headers": headers or {},
        "body": body,
        "isBase64Encoded": is_base64,
    }


def _fake_presigned_post(**kwargs):
    return {
        "url": "https://test-uploads-bucket.s3.amazonaws.com/",
        "fields": {"key": kwargs["Key"], **kwargs["Fields"]},
    }


def test_rejects_non_post_methods():
    response = presign.handler(_alb_event(method="GET"), None)
    assert response["statusCode"] == 405


def test_rejects_missing_body():
    response = presign.handler(_alb_event(body=None), None)
    assert response["statusCode"] == 400
    assert "body" in json.loads(response["body"])["error"].lower()


def test_rejects_invalid_json_body():
    response = presign.handler(_alb_event(body="not json"), None)
    assert response["statusCode"] == 400


def test_rejects_missing_filename():
    response = presign.handler(
        _alb_event(body=json.dumps({"contentType": "text/plain"})), None
    )
    assert response["statusCode"] == 400
    assert "filename" in json.loads(response["body"])["error"].lower()


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_returns_presigned_fields_for_valid_request(mock_generate, _mock_auth):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps({"filename": "report.pdf", "contentType": "application/pdf"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["url"].startswith("https://")
    assert payload["fields"]["Content-Type"] == "application/pdf"
    assert payload["key"].startswith("uploads/")
    assert payload["key"].endswith("report.pdf")

    # Confirm the content-length-range condition was applied.
    _, kwargs = mock_generate.call_args
    conditions = kwargs["Conditions"]
    assert ["content-length-range", 0, presign.MAX_UPLOAD_BYTES] in conditions


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_defaults_content_type_when_missing(mock_generate, _mock_auth):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps({"filename": "notes.txt"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["fields"]["Content-Type"] == "application/octet-stream"


def test_s3_client_configured_for_regional_virtual_hosted_urls():
    """Regression test for a real production failure.

    Without region_name + addressing_style="virtual", boto3 generates
    presigned POST URLs using the legacy global s3.amazonaws.com endpoint,
    which 307-redirects to the region-specific endpoint for any bucket
    outside us-east-1 (ours is eu-west-2). Browsers don't carry CORS
    headers through that redirect, so the actual upload fails client-side
    with a CORS/NetworkError - even though the presign call itself
    succeeds (which is what made this confusing to diagnose from server
    logs alone).
    """
    assert presign.s3_client.meta.region_name == "eu-west-2"
    assert presign.s3_client.meta.config.s3["addressing_style"] == "virtual"


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_returns_500_when_s3_call_fails(mock_generate, _mock_auth):
    mock_generate.side_effect = Exception("boom")

    body = json.dumps({"filename": "report.pdf"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 500


def test_build_key_sanitizes_unsafe_filename_characters():
    key = presign._build_key("../../etc/passwd; rm -rf .txt")
    assert ".." not in key
    assert "/" not in key.rsplit("/", 1)[-1]  # sanitized leaf has no path separators
    assert key.startswith("uploads/")


def test_build_key_is_unique_per_call():
    key_a = presign._build_key("same-name.txt")
    key_b = presign._build_key("same-name.txt")
    assert key_a != key_b


# --- Verified uploader identity (via cognito-auth) ---


@patch("backend_src.presign.handler._auth.get_auth_user")
def test_get_uploader_claims_returns_identity_from_verified_user(mock_get_auth_user):
    mock_get_auth_user.return_value = User.create_mock(
        sub="abc-123",
        email="dev.user@example.gov.uk",
        given_name="Dev",
        family_name="User",
    )

    claims = presign._get_uploader_claims(_alb_event())

    assert claims["sub"] == "abc-123"
    assert claims["email"] == "dev.user@example.gov.uk"
    assert claims["given_name"] == "Dev"
    assert claims["name"] == "Dev User"


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
def test_get_uploader_claims_returns_empty_dict_when_tokens_missing(
    _mock_get_auth_user,
):
    assert presign._get_uploader_claims(_alb_event()) == {}


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=ExpiredTokenError("x"),
)
def test_get_uploader_claims_returns_empty_dict_when_token_expired(_mock_get_auth_user):
    assert presign._get_uploader_claims(_alb_event()) == {}


# --- Upload attribution metadata ---


@patch("backend_src.presign.handler._auth.get_auth_user")
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_upload_is_tagged_with_verified_uploader_identity(
    mock_generate, mock_get_auth_user
):
    mock_generate.side_effect = _fake_presigned_post
    mock_get_auth_user.return_value = User.create_mock(
        sub="abc-123",
        email="dev.user@example.gov.uk",
        given_name="Dev",
        family_name="User",
    )

    body = json.dumps({"filename": "report.pdf"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["fields"]["x-amz-meta-uploaded-by-sub"] == "abc-123"
    assert (
        payload["fields"]["x-amz-meta-uploaded-by-email"] == "dev.user@example.gov.uk"
    )
    assert payload["fields"]["x-amz-meta-uploaded-by-name"] == "Dev User"
    assert "x-amz-meta-uploaded-at" in payload["fields"]

    # Every metadata field must also appear as a matching presigned POST
    # condition, or S3 will reject the upload.
    _, kwargs = mock_generate.call_args
    conditions = kwargs["Conditions"]
    for meta_key in (
        "uploaded-by-sub",
        "uploaded-by-email",
        "uploaded-by-name",
        "uploaded-at",
    ):
        field_name = f"x-amz-meta-{meta_key}"
        assert {field_name: payload["fields"][field_name]} in conditions


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_upload_still_succeeds_without_identity_metadata_when_unverifiable(
    mock_generate, _mock_get_auth_user
):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps({"filename": "report.pdf"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert "x-amz-meta-uploaded-by-sub" not in payload["fields"]
    assert "x-amz-meta-uploaded-by-email" not in payload["fields"]
    # Timestamp is always recorded, even without an identity to attribute it to.
    assert "x-amz-meta-uploaded-at" in payload["fields"]


def test_presigned_post_params_falls_back_from_name_to_given_name():
    params = presign._presigned_post_params("text/plain", {"given_name": "Dev"})
    assert params["Fields"]["x-amz-meta-uploaded-by-name"] == "Dev"


def test_presigned_post_params_prefers_name_over_given_name():
    params = presign._presigned_post_params(
        "text/plain", {"name": "Dev User", "given_name": "Dev"}
    )
    assert params["Fields"]["x-amz-meta-uploaded-by-name"] == "Dev User"


# --- Optional fileSize (logging/metrics only, doesn't affect presign) ---


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_accepts_optional_file_size(mock_generate, _mock_auth):
    mock_generate.side_effect = _fake_presigned_post

    # Below MULTIPART_THRESHOLD_BYTES - stays on the simple path this test
    # actually mocks. Multipart routing itself is covered separately below.
    body = json.dumps({"filename": "report.pdf", "fileSize": 1000})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_ignores_non_numeric_file_size(mock_generate, _mock_auth):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps({"filename": "report.pdf", "fileSize": "not-a-number"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200


# --- Client-side failure reporting ("action": "report-error") ---


def test_report_error_returns_ok_without_touching_s3():
    body = json.dumps(
        {
            "action": "report-error",
            "filename": "big.csv",
            "fileSize": 2684763697,
            "elapsedMs": 12345,
            "error": "NetworkError when attempting to fetch resource",
        }
    )
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}


def test_report_error_logs_structured_fields(caplog):
    body = json.dumps(
        {
            "action": "report-error",
            "filename": "big.csv",
            "fileSize": 2684763697,
            "elapsedMs": 12345,
            "error": "boom",
        }
    )
    with caplog.at_level("WARNING", logger="drop-presign"):
        presign.handler(_alb_event(body=body), None)

    record = next(r for r in caplog.records if r.message == "upload_reported_failed")
    assert record.upload_filename == "big.csv"
    assert record.file_size == 2684763697
    assert record.elapsed_ms == 12345
    assert record.error == "boom"


def test_report_error_does_not_require_filename():
    """report-error is best-effort observability - never itself a hard failure."""
    body = json.dumps({"action": "report-error", "error": "boom"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200


# --- Multipart upload (files over MULTIPART_THRESHOLD_BYTES) ---


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_url")
@patch("backend_src.presign.handler.s3_client.create_multipart_upload")
def test_large_file_returns_multipart_shape(mock_create, mock_url, _mock_auth):
    mock_create.return_value = {"UploadId": "upload-123"}
    mock_url.side_effect = lambda op, Params, ExpiresIn: (
        f"https://s3.example/part-{Params['PartNumber']}"
    )

    file_size = presign.MULTIPART_THRESHOLD_BYTES + 1
    body = json.dumps({"filename": "big.csv", "fileSize": file_size})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["uploadId"] == "upload-123"
    assert payload["partSize"] == presign.PART_SIZE_BYTES
    assert payload["key"].startswith("uploads/")
    assert payload["totalParts"] == len(payload["parts"])
    assert payload["parts"][0] == {"partNumber": 1, "url": "https://s3.example/part-1"}


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_url")
@patch("backend_src.presign.handler.s3_client.create_multipart_upload")
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_small_file_does_not_use_multipart(
    mock_generate, mock_create, mock_url, _mock_auth
):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps(
        {"filename": "small.csv", "fileSize": presign.MULTIPART_THRESHOLD_BYTES}
    )
    response = presign.handler(_alb_event(body=body), None)

    # At/below the threshold - takes the ordinary presigned-POST path.
    mock_create.assert_not_called()
    mock_url.assert_not_called()
    assert response["statusCode"] == 200
    assert "uploadId" not in json.loads(response["body"])


@pytest.mark.parametrize(
    ("file_size", "part_size", "expected_parts"),
    [
        (250 * 1024 * 1024, 100 * 1024 * 1024, 3),  # 100 + 100 + 50
        (200 * 1024 * 1024, 100 * 1024 * 1024, 2),  # exact multiple
        (150 * 1024 * 1024, 100 * 1024 * 1024, 2),  # small remainder part
    ],
)
@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_url")
@patch("backend_src.presign.handler.s3_client.create_multipart_upload")
def test_part_count_matches_file_size_division(
    mock_create, mock_url, _mock_auth, file_size, part_size, expected_parts
):
    mock_create.return_value = {"UploadId": "upload-123"}
    mock_url.return_value = "https://s3.example/part"

    with patch("backend_src.presign.handler.PART_SIZE_BYTES", part_size):
        body = json.dumps({"filename": "f.bin", "fileSize": file_size})
        response = presign.handler(_alb_event(body=body), None)

    payload = json.loads(response["body"])
    assert payload["totalParts"] == expected_parts
    assert len(payload["parts"]) == expected_parts


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
)
@patch("backend_src.presign.handler.s3_client.generate_presigned_url")
@patch("backend_src.presign.handler.s3_client.create_multipart_upload")
def test_multipart_create_tags_uploader_metadata(
    mock_create, mock_url, mock_get_auth_user
):
    mock_create.return_value = {"UploadId": "upload-123"}
    mock_url.return_value = "https://s3.example/part"
    mock_get_auth_user.return_value = User.create_mock(
        sub="abc-123",
        email="dev.user@example.gov.uk",
        given_name="Dev",
        family_name="User",
    )

    file_size = presign.MULTIPART_THRESHOLD_BYTES + 1
    body = json.dumps({"filename": "big.csv", "fileSize": file_size})
    presign.handler(_alb_event(body=body), None)

    metadata = mock_create.call_args.kwargs["Metadata"]
    assert metadata["uploaded-by-sub"] == "abc-123"
    assert metadata["uploaded-by-email"] == "dev.user@example.gov.uk"
    assert metadata["uploaded-by-name"] == "Dev User"
    assert "uploaded-at" in metadata


@patch(
    "backend_src.presign.handler._auth.get_auth_user",
    side_effect=MissingTokenError("x"),
)
@patch(
    "backend_src.presign.handler.s3_client.create_multipart_upload",
    side_effect=Exception("boom"),
)
def test_multipart_create_returns_500_on_s3_failure(_mock_create, _mock_auth):
    file_size = presign.MULTIPART_THRESHOLD_BYTES + 1
    body = json.dumps({"filename": "big.csv", "fileSize": file_size})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 500


def test_complete_multipart_calls_s3_with_correct_parts():
    with patch(
        "backend_src.presign.handler.s3_client.complete_multipart_upload"
    ) as mock_complete:
        body = json.dumps(
            {
                "action": "complete",
                "uploadId": "upload-123",
                "key": "uploads/x/y.csv",
                "parts": [
                    {"partNumber": 1, "eTag": "etag-1"},
                    {"partNumber": 2, "eTag": "etag-2"},
                ],
            }
        )
        response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}
    mock_complete.assert_called_once_with(
        Bucket=presign.BUCKET_NAME,
        Key="uploads/x/y.csv",
        UploadId="upload-123",
        MultipartUpload={
            "Parts": [
                {"PartNumber": 1, "ETag": "etag-1"},
                {"PartNumber": 2, "ETag": "etag-2"},
            ]
        },
    )


@pytest.mark.parametrize(
    "body",
    [
        {"action": "complete", "key": "x", "parts": [{"partNumber": 1, "eTag": "e"}]},
        {
            "action": "complete",
            "uploadId": "u",
            "parts": [{"partNumber": 1, "eTag": "e"}],
        },
        {"action": "complete", "uploadId": "u", "key": "x", "parts": []},
    ],
)
def test_complete_multipart_requires_upload_id_key_and_parts(body):
    response = presign.handler(_alb_event(body=json.dumps(body)), None)
    assert response["statusCode"] == 400


def test_complete_multipart_returns_500_on_s3_failure():
    with patch(
        "backend_src.presign.handler.s3_client.complete_multipart_upload",
        side_effect=Exception("boom"),
    ):
        body = json.dumps(
            {
                "action": "complete",
                "uploadId": "upload-123",
                "key": "uploads/x/y.csv",
                "parts": [{"partNumber": 1, "eTag": "etag-1"}],
            }
        )
        response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 500


def test_abort_multipart_calls_s3():
    with patch(
        "backend_src.presign.handler.s3_client.abort_multipart_upload"
    ) as mock_abort:
        body = json.dumps(
            {"action": "abort", "uploadId": "upload-123", "key": "uploads/x/y.csv"}
        )
        response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    mock_abort.assert_called_once_with(
        Bucket=presign.BUCKET_NAME, Key="uploads/x/y.csv", UploadId="upload-123"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"action": "abort", "key": "x"},
        {"action": "abort", "uploadId": "u"},
    ],
)
def test_abort_multipart_requires_upload_id_and_key(body):
    response = presign.handler(_alb_event(body=json.dumps(body)), None)
    assert response["statusCode"] == 400


def test_abort_multipart_returns_500_on_s3_failure():
    with patch(
        "backend_src.presign.handler.s3_client.abort_multipart_upload",
        side_effect=Exception("boom"),
    ):
        body = json.dumps(
            {"action": "abort", "uploadId": "upload-123", "key": "uploads/x/y.csv"}
        )
        response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 500
