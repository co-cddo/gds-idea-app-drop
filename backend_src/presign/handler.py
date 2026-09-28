"""ALB Lambda target: issues S3 presigned POST URLs for direct browser uploads.

This function sits behind the frontend's ALB on a dedicated listener rule
(`/api/presign`), reusing the same Cognito authentication action as the
static site itself (wired up in `app.py`). By the time a request reaches
this handler, the ALB has already enforced authentication - unauthenticated
requests never get this far.

Each uploaded object is tagged with the uploader's identity and upload time
as S3 object metadata (x-amz-meta-uploaded-by-*, x-amz-meta-uploaded-at).
Identity is obtained via `cognito-auth`'s `LambdaAuth`, which verifies the
ALB's `x-amzn-oidc-data` JWT signature against AWS's published ALB public
key (not just decoded/trusted) - see `_get_uploader_claims`. This doesn't
affect the S3 key itself (already collision-free via a UUID - see
`_build_key`), it's purely for attribution - this attribution is expected to
matter later (e.g. a "your uploads" page), so it's verified properly rather
than just decoded.

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
from botocore.config import Config
from cognito_auth.exceptions import (
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
)
from cognito_auth.lambda_auth import LambdaAuth

_AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")

# region_name + addressing_style="virtual" are both required here - without
# them, boto3 generates presigned POST URLs using the legacy global
# s3.amazonaws.com endpoint, which 307-redirects to the region-specific one
# for any bucket outside us-east-1 (ours is eu-west-2). Browsers don't carry
# CORS headers through that redirect cleanly, so the actual upload fails
# client-side with a CORS/NetworkError - the presign call itself still
# succeeds, which is what made this confusing to diagnose.
s3_client = boto3.client(
    "s3",
    region_name=_AWS_REGION,
    config=Config(s3={"addressing_style": "virtual"}),
)

BUCKET_NAME = os.environ["UPLOADS_BUCKET_NAME"]

# authoriser=None: the ALB's Cognito auth action has already gated who can
# reach this Lambda at all, so this is used purely to obtain a *verified*
# identity for attribution metadata - not to re-run authorisation checks.
_auth = LambdaAuth(authoriser=None, region=_AWS_REGION)

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
    claims = _get_uploader_claims(event)

    try:
        presigned = s3_client.generate_presigned_post(
            Bucket=BUCKET_NAME,
            Key=key,
            **_presigned_post_params(content_type, claims),
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
    """Build a collision-resistant, path-safe S3 key for an uploaded file."""
    safe_name = _SAFE_FILENAME_RE.sub("_", filename).strip("._") or "file"
    date_prefix = datetime.now(UTC).strftime("%Y/%m/%d")
    return f"uploads/{date_prefix}/{uuid.uuid4()}/{safe_name}"


def _get_uploader_claims(event: dict) -> dict:
    """Get the verified uploader's identity claims, via cognito-auth.

    Verifies the ALB's x-amzn-oidc-data JWT signature against AWS's
    published ALB public key (ES256) - this is the same mechanism the
    platform's own /.auth/user endpoint relies on, not a hand-rolled decode.

    Attribution is best-effort: returns {} if the tokens are missing,
    invalid, or expired, and callers must not let that block the upload
    itself - a stale/misconfigured session shouldn't prevent someone from
    sending a file, it just means this particular upload goes unattributed.
    """
    try:
        user = _auth.get_auth_user(event)
    except (MissingTokenError, InvalidTokenError, ExpiredTokenError) as exc:
        print(f"WARNING: could not verify uploader identity: {exc}")
        return {}

    return {
        "sub": user.sub,
        "email": user.email,
        "name": user.name,
        "given_name": user.given_name,
    }


def _presigned_post_params(content_type: str, claims: dict) -> dict:
    """Build the Fields/Conditions/ExpiresIn kwargs for generate_presigned_post.

    Tags the object with the uploader's identity (from `claims`, see
    _get_uploader_claims) and the upload time as S3 object metadata.
    Identity fields are only included if actually present in the claims -
    a missing/unverifiable identity still produces a valid upload, just
    without attribution metadata.
    """
    fields = {"Content-Type": content_type}
    conditions = [
        {"Content-Type": content_type},
        ["content-length-range", 0, MAX_UPLOAD_BYTES],
    ]

    metadata = {
        "uploaded-by-sub": claims.get("sub"),
        "uploaded-by-email": claims.get("email"),
        "uploaded-by-name": claims.get("name") or claims.get("given_name"),
        "uploaded-at": datetime.now(UTC).isoformat(),
    }
    for meta_key, value in metadata.items():
        if not value:
            continue
        field_name = f"x-amz-meta-{meta_key}"
        fields[field_name] = value
        conditions.append({field_name: value})

    return {
        "Fields": fields,
        "Conditions": conditions,
        "ExpiresIn": PRESIGN_EXPIRY_SECONDS,
    }


def _response(status_code: int, body_dict: dict) -> dict:
    """Build an ALB-compatible Lambda target response."""
    return {
        "statusCode": status_code,
        "statusDescription": f"{status_code} {'OK' if status_code == 200 else 'Error'}",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body_dict),
        "isBase64Encoded": False,
    }
