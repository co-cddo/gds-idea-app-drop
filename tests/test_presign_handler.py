"""Unit tests for the presigned-POST Lambda handler."""

import json
from unittest.mock import patch

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
    response = presign.handler(_alb_event(body=json.dumps({"contentType": "text/plain"})), None)
    assert response["statusCode"] == 400
    assert "filename" in json.loads(response["body"])["error"].lower()


@patch("backend_src.presign.handler._auth.get_auth_user", side_effect=MissingTokenError("x"))
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


@patch("backend_src.presign.handler._auth.get_auth_user", side_effect=MissingTokenError("x"))
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_defaults_content_type_when_missing(mock_generate, _mock_auth):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps({"filename": "notes.txt"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["fields"]["Content-Type"] == "application/octet-stream"


@patch("backend_src.presign.handler._auth.get_auth_user", side_effect=MissingTokenError("x"))
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
        sub="abc-123", email="dev.user@example.gov.uk", given_name="Dev", family_name="User"
    )

    claims = presign._get_uploader_claims(_alb_event())

    assert claims["sub"] == "abc-123"
    assert claims["email"] == "dev.user@example.gov.uk"
    assert claims["given_name"] == "Dev"
    assert claims["name"] == "Dev User"


@patch("backend_src.presign.handler._auth.get_auth_user", side_effect=MissingTokenError("x"))
def test_get_uploader_claims_returns_empty_dict_when_tokens_missing(_mock_get_auth_user):
    assert presign._get_uploader_claims(_alb_event()) == {}


@patch("backend_src.presign.handler._auth.get_auth_user", side_effect=ExpiredTokenError("x"))
def test_get_uploader_claims_returns_empty_dict_when_token_expired(_mock_get_auth_user):
    assert presign._get_uploader_claims(_alb_event()) == {}


# --- Upload attribution metadata ---


@patch("backend_src.presign.handler._auth.get_auth_user")
@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_upload_is_tagged_with_verified_uploader_identity(mock_generate, mock_get_auth_user):
    mock_generate.side_effect = _fake_presigned_post
    mock_get_auth_user.return_value = User.create_mock(
        sub="abc-123", email="dev.user@example.gov.uk", given_name="Dev", family_name="User"
    )

    body = json.dumps({"filename": "report.pdf"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["fields"]["x-amz-meta-uploaded-by-sub"] == "abc-123"
    assert payload["fields"]["x-amz-meta-uploaded-by-email"] == "dev.user@example.gov.uk"
    assert payload["fields"]["x-amz-meta-uploaded-by-name"] == "Dev User"
    assert "x-amz-meta-uploaded-at" in payload["fields"]

    # Every metadata field must also appear as a matching presigned POST
    # condition, or S3 will reject the upload.
    _, kwargs = mock_generate.call_args
    conditions = kwargs["Conditions"]
    for meta_key in ("uploaded-by-sub", "uploaded-by-email", "uploaded-by-name", "uploaded-at"):
        field_name = f"x-amz-meta-{meta_key}"
        assert {field_name: payload["fields"][field_name]} in conditions


@patch("backend_src.presign.handler._auth.get_auth_user", side_effect=MissingTokenError("x"))
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
