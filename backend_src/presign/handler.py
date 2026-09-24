"""ALB Lambda target: issues S3 presigned POST URLs for direct browser uploads.

This function sits behind the frontend's ALB on a dedicated listener rule
(`/api/presign`), reusing the same Cognito authentication action as the
static site itself (wired up in `app.py`). By the time a request reaches
this handler, the ALB has already enforced authentication - unauthenticated
requests never get this far.

Request (JSON body):
    {"filename": "report.pdf", "contentType": "application/pdf"}

Response (JSON body):
    {"url": "...", "fields": {...}, "key": "uploads/2026/01/01/<uuid>/report.pdf"}

The caller is expected to POST a multipart/form-data request built from
`fields` (plus the file itself, as field "file") directly to `url`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from datetime import UTC, datetime

import boto3

s3_client = boto3.client("s3")

BUCKET_NAME = os.environ["UPLOADS_BUCKET_NAME"]

# S3 presigned POST forms support up to ~5GiB per file. This is a placeholder
# ceiling for the prototype - multipart/resumable uploads for larger files
# are a separate piece of future work.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024 * 1024)))

# How long a presigned POST URL/fields remain valid after being issued.
PRESIGN_EXPIRY_SECONDS = int(os.environ.get("PRESIGN_EXPIRY_SECONDS", str(15 * 60)))

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def handler(event, context):
    """Handle an ALB request for a presigned upload URL."""
    method = event.get("httpMethod", "GET")
    if method != "POST":
        return _response(405, {"error": "Method not allowed"})

    try:
        body = _parse_body(event)
    except (ValueError, TypeError) as exc:
        return _response(400, {"error": str(exc)})

    filename = body.get("filename")
    if not filename or not isinstance(filename, str):
        return _response(400, {"error": "filename is required"})

    content_type = body.get("contentType") or _DEFAULT_CONTENT_TYPE
    if not isinstance(content_type, str):
        return _response(400, {"error": "contentType must be a string"})

    key = _build_key(filename)

    try:
        presigned = s3_client.generate_presigned_post(
            Bucket=BUCKET_NAME,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 0, MAX_UPLOAD_BYTES],
            ],
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - report and return 500
        print(f"ERROR: failed to generate presigned post for key={key}: {exc}")
        return _response(500, {"error": "Failed to generate upload URL"})

    return _response(
        200,
        {"url": presigned["url"], "fields": presigned["fields"], "key": key},
    )


def _parse_body(event: dict) -> dict:
    """Extract and JSON-decode the ALB event body."""
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if not raw_body:
        raise ValueError("Request body is required")

    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("Request body must be valid JSON") from exc

    if not isinstance(parsed, dict):
        raise TypeError("Request body must be a JSON object")

    return parsed


def _build_key(filename: str) -> str:
    """Build a collision-resistant, path-safe S3 key for an uploaded file.

    Note: this does not currently attribute uploads to a specific user.
    Decoding the ALB OIDC header to tag uploads by user is a reasonable
    future enhancement, deferred alongside notifications.
    """
    safe_name = _SAFE_FILENAME_RE.sub("_", filename).strip("._") or "file"
    date_prefix = datetime.now(UTC).strftime("%Y/%m/%d")
    return f"uploads/{date_prefix}/{uuid.uuid4()}/{safe_name}"


def _response(status_code: int, body_dict: dict) -> dict:
    """Build an ALB-compatible Lambda target response."""
    return {
        "statusCode": status_code,
        "statusDescription": f"{status_code} {'OK' if status_code == 200 else 'Error'}",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body_dict),
        "isBase64Encoded": False,
    }
