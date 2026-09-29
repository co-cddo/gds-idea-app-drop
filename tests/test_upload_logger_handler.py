"""Unit tests for the upload-confirmation Lambda (S3 ObjectCreated handler)."""

import json
from unittest.mock import patch

from backend_src.upload_logger import handler as upload_logger


def _s3_event(
    *, bucket="test-uploads-bucket", key="uploads/2026/01/01/abc/report.pdf", size=1234
):
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key, "size": size},
                }
            }
        ]
    }


@patch("backend_src.upload_logger.handler.s3_client.head_object")
def test_logs_confirmation_with_uploader_metadata(mock_head_object, caplog):
    mock_head_object.return_value = {
        "Metadata": {
            "uploaded-by-email": "dev.user@example.gov.uk",
            "uploaded-by-name": "Dev User",
            "uploaded-at": "2026-01-01T00:00:00+00:00",
        }
    }

    with caplog.at_level("INFO", logger="drop-upload-logger"):
        upload_logger.handler(_s3_event(), None)

    record = next(
        r for r in caplog.records if '"event": "upload_confirmed"' in r.message
    )
    payload = json.loads(record.message)
    assert payload["key"] == "uploads/2026/01/01/abc/report.pdf"
    assert payload["size"] == 1234
    assert payload["uploaded_by_email"] == "dev.user@example.gov.uk"
    assert payload["uploaded_by_name"] == "Dev User"


@patch("backend_src.upload_logger.handler.s3_client.head_object")
def test_logs_confirmation_even_without_metadata(mock_head_object, caplog):
    """An unattributed upload (no verified identity) is still confirmed."""
    mock_head_object.return_value = {"Metadata": {}}

    with caplog.at_level("INFO", logger="drop-upload-logger"):
        upload_logger.handler(_s3_event(), None)

    record = next(
        r for r in caplog.records if '"event": "upload_confirmed"' in r.message
    )
    payload = json.loads(record.message)
    assert payload["uploaded_by_email"] is None


@patch(
    "backend_src.upload_logger.handler.s3_client.head_object",
    side_effect=Exception("boom"),
)
def test_logs_confirmation_even_when_head_object_fails(mock_head_object, caplog):
    """A failed metadata lookup shouldn't stop us recording the confirmation."""
    with caplog.at_level("INFO", logger="drop-upload-logger"):
        upload_logger.handler(_s3_event(), None)

    messages = [r.message for r in caplog.records]
    assert any('"event": "upload_confirm_head_failed"' in m for m in messages)
    assert any('"event": "upload_confirmed"' in m for m in messages)


def test_url_decodes_object_key():
    """S3 event notification keys are URL-encoded (e.g. spaces as '+')."""
    event = _s3_event(key="uploads/2026/01/01/abc/my+report.pdf")

    with patch("backend_src.upload_logger.handler.s3_client.head_object") as mock_head:
        mock_head.return_value = {"Metadata": {}}
        upload_logger.handler(event, None)

    mock_head.assert_called_once_with(
        Bucket="test-uploads-bucket", Key="uploads/2026/01/01/abc/my report.pdf"
    )


def test_skips_record_with_missing_key():
    event = {
        "Records": [{"s3": {"bucket": {"name": "test-uploads-bucket"}, "object": {}}}]
    }
    # Should not raise.
    upload_logger.handler(event, None)
