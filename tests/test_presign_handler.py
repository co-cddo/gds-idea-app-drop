"""Unit tests for the presigned-POST Lambda handler."""

import json
from unittest.mock import patch

from backend_src.presign import handler as presign


def _alb_event(*, method="POST", body=None, is_base64=False):
    return {
        "httpMethod": method,
        "path": "/api/presign",
        "headers": {},
        "body": body,
        "isBase64Encoded": is_base64,
    }


def _fake_presigned_post(**kwargs):
    return {
        "url": "https://test-uploads-bucket.s3.amazonaws.com/",
        "fields": {"key": kwargs["Key"], "Content-Type": kwargs["Fields"]["Content-Type"]},
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


@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_returns_presigned_fields_for_valid_request(mock_generate):
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


@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_defaults_content_type_when_missing(mock_generate):
    mock_generate.side_effect = _fake_presigned_post

    body = json.dumps({"filename": "notes.txt"})
    response = presign.handler(_alb_event(body=body), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["fields"]["Content-Type"] == "application/octet-stream"


@patch("backend_src.presign.handler.s3_client.generate_presigned_post")
def test_returns_500_when_s3_call_fails(mock_generate):
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
