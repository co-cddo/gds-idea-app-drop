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
    {"filename": "report.pdf", "contentType": "application/pdf", "fileSize": 1234}

    Files larger than MULTIPART_THRESHOLD_BYTES get a multipart upload
    instead of a single presigned POST - see _handle_create_multipart.
    `fileSize` is required to pick between the two; a request without it
    always gets the simple/single-POST path.

    Also accepts:
      {"action": "report-error", ...} - best-effort client-side failure
        report, since real uploads go straight from the browser to S3 and
        are otherwise invisible server-side.
      {"action": "complete", "uploadId", "key", "parts": [{"partNumber", "eTag"}]}
      {"action": "abort", "uploadId", "key"}

Response (JSON body), single presigned POST:
    {"url": "...", "fields": {...}, "key": "uploads/2026/01/01/<uuid>/report.pdf"}

    The caller is expected to POST a multipart/form-data request built from
    `fields` (plus the file itself, as field "file") directly to `url`.

Response (JSON body), multipart:
    {"uploadId": "...", "key": "...", "partSize": 104857600, "totalParts": 25,
     "parts": [{"partNumber": 1, "url": "..."}, ...]}

    The caller PUTs each part's bytes to its `url`, collects the `ETag`
    response header from each, then calls back with {"action": "complete", ...}.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import uuid
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.config import Config
from cognito_auth.exceptions import (
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
)
from cognito_auth.lambda_auth import LambdaAuth

logger = Logger(service="drop-presign")
metrics = Metrics(namespace="drop", service="presign")

_AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")

# region_name + addressing_style="virtual" are both required here - without
# them, boto3 generates presigned POST URLs using the legacy global
# s3.amazonaws.com endpoint, which 307-redirects to the region-specific one
# for any bucket outside us-east-1 (ours is eu-west-2). Browsers don't carry
# CORS headers through that redirect cleanly, so the actual upload fails
# client-side with a CORS/NetworkError - the presign call itself still
# succeeds, which is what made this confusing to diagnose.
#
# signature_version="s3v4" is explicit for the same class of reason: it's
# the only version eu-west-2 actually supports, and generate_presigned_url
# (used for multipart parts, unlike generate_presigned_post) warns it may
# silently fall back to an unsigned/incompatible scheme without this set.
s3_client = boto3.client(
    "s3",
    region_name=_AWS_REGION,
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

BUCKET_NAME = os.environ["UPLOADS_BUCKET_NAME"]

# authoriser=None: the ALB's Cognito auth action has already gated who can
# reach this Lambda at all, so this is used purely to obtain a *verified*
# identity for attribution metadata - not to re-run authorisation checks.
_auth = LambdaAuth(authoriser=None, region=_AWS_REGION)

# S3 presigned POST forms support up to ~5GiB per file. This is a placeholder
# ceiling for the prototype - true resumable uploads (retrying only a failed
# part, which multipart already gets us most of the way to) are covered by
# MULTIPART_THRESHOLD_BYTES below; this is still the overall file-size cap.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024 * 1024)))

# How long a presigned POST URL/fields remain valid after being issued.
PRESIGN_EXPIRY_SECONDS = int(os.environ.get("PRESIGN_EXPIRY_SECONDS", str(15 * 60)))

# Files at or below this use a single presigned POST (unchanged, simplest
# path); above it, use S3 multipart upload instead - a single-shot POST of
# a multi-GB file over one TCP connection is both slow (no parallelism) and
# fragile (one 15-minute presign window for the whole transfer, no retry
# smaller than the entire file).
MULTIPART_THRESHOLD_BYTES = int(
    os.environ.get("MULTIPART_THRESHOLD_BYTES", str(100 * 1024 * 1024))
)

# 100MB parts -> max 50 parts for a 5GB file, comfortably under S3's
# 10,000-part limit with room to raise MAX_UPLOAD_BYTES later if needed.
PART_SIZE_BYTES = int(os.environ.get("PART_SIZE_BYTES", str(100 * 1024 * 1024)))

# Generous relative to PRESIGN_EXPIRY_SECONDS: all part URLs are signed
# up front, but a bounded-concurrency client won't start every part
# immediately, and parts may need to retry - this needs to comfortably
# outlast the whole transfer, not just one part's upload time.
MULTIPART_PART_EXPIRY_SECONDS = int(
    os.environ.get("MULTIPART_PART_EXPIRY_SECONDS", str(2 * 60 * 60))
)

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def handler(event, context):
    """Handle an ALB request for a presigned upload URL.

    Metrics are flushed here (not via the usual @metrics.log_metrics
    decorator) because that decorator - like @logger.inject_lambda_context -
    reads real attributes off `context`, which is `None` in most of this
    module's own unit tests.
    """
    try:
        return _handle(event)
    finally:
        metrics.flush_metrics(raise_on_empty_metrics=False)


def _handle(event: dict) -> dict:
    method = event.get("httpMethod", "GET")
    if method != "POST":
        return _response(405, {"error": "Method not allowed"})

    try:
        body = _parse_body(event)
    except (ValueError, TypeError) as exc:
        return _response(400, {"error": str(exc)})

    action = body.get("action")
    if action == "report-error":
        return _handle_report_error(body)
    if action == "complete":
        return _handle_complete_multipart(body)
    if action == "abort":
        return _handle_abort_multipart(body)

    filename = body.get("filename")
    if not filename or not isinstance(filename, str):
        return _response(400, {"error": "filename is required"})

    content_type = body.get("contentType") or _DEFAULT_CONTENT_TYPE
    if not isinstance(content_type, str):
        return _response(400, {"error": "contentType must be a string"})

    file_size = body.get("fileSize")
    if not isinstance(file_size, (int, float)):
        file_size = None

    key = _build_key(filename)
    claims = _get_uploader_claims(event)

    if file_size is not None and file_size > MULTIPART_THRESHOLD_BYTES:
        return _handle_create_multipart(
            key=key,
            filename=filename,
            content_type=content_type,
            file_size=file_size,
            claims=claims,
        )

    try:
        presigned = s3_client.generate_presigned_post(
            Bucket=BUCKET_NAME,
            Key=key,
            **_presigned_post_params(content_type, claims),
        )
    except Exception:
        logger.exception("failed to generate presigned post", key=key)
        return _response(500, {"error": "Failed to generate upload URL"})

    logger.info(
        "presign_issued",
        key=key,
        upload_filename=filename,
        content_type=content_type,
        file_size=file_size,
        uploader_email=claims.get("email"),
    )
    metrics.add_metric(name="PresignIssued", unit=MetricUnit.Count, value=1)
    if file_size is not None:
        metrics.add_metric(
            name="RequestedFileSize", unit=MetricUnit.Bytes, value=file_size
        )

    return _response(
        200,
        {"url": presigned["url"], "fields": presigned["fields"], "key": key},
    )


def _handle_create_multipart(
    *, key: str, filename: str, content_type: str, file_size: float, claims: dict
) -> dict:
    """Start a multipart upload and presign every part up front.

    All part URLs are signed now, in one Lambda invocation, rather than
    letting the client ask for them one at a time - simpler, and avoids
    N further round trips through the ALB during the upload itself.
    """
    total_parts = math.ceil(file_size / PART_SIZE_BYTES)

    try:
        created = s3_client.create_multipart_upload(
            Bucket=BUCKET_NAME,
            Key=key,
            ContentType=content_type,
            Metadata=_uploader_metadata(claims),
        )
        upload_id = created["UploadId"]

        parts = [
            {
                "partNumber": part_number,
                "url": s3_client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": BUCKET_NAME,
                        "Key": key,
                        "PartNumber": part_number,
                        "UploadId": upload_id,
                    },
                    ExpiresIn=MULTIPART_PART_EXPIRY_SECONDS,
                ),
            }
            for part_number in range(1, total_parts + 1)
        ]
    except Exception:
        logger.exception("failed to create multipart upload", key=key)
        return _response(500, {"error": "Failed to start multipart upload"})

    logger.info(
        "multipart_created",
        key=key,
        upload_id=upload_id,
        upload_filename=filename,
        content_type=content_type,
        file_size=file_size,
        total_parts=total_parts,
        uploader_email=claims.get("email"),
    )
    metrics.add_metric(name="MultipartCreated", unit=MetricUnit.Count, value=1)
    metrics.add_metric(name="RequestedFileSize", unit=MetricUnit.Bytes, value=file_size)

    return _response(
        200,
        {
            "uploadId": upload_id,
            "key": key,
            "partSize": PART_SIZE_BYTES,
            "totalParts": total_parts,
            "parts": parts,
        },
    )


def _handle_complete_multipart(body: dict) -> dict:
    upload_id = body.get("uploadId")
    key = body.get("key")
    parts = body.get("parts")
    if not upload_id or not key or not parts:
        return _response(400, {"error": "uploadId, key and parts are required"})

    try:
        s3_client.complete_multipart_upload(
            Bucket=BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": part["partNumber"], "ETag": part["eTag"]}
                    for part in parts
                ]
            },
        )
    except Exception:
        logger.exception(
            "failed to complete multipart upload", key=key, upload_id=upload_id
        )
        return _response(500, {"error": "Failed to complete upload"})

    logger.info(
        "multipart_completed", key=key, upload_id=upload_id, part_count=len(parts)
    )
    metrics.add_metric(name="MultipartCompleted", unit=MetricUnit.Count, value=1)
    return _response(200, {"ok": True})


def _handle_abort_multipart(body: dict) -> dict:
    upload_id = body.get("uploadId")
    key = body.get("key")
    if not upload_id or not key:
        return _response(400, {"error": "uploadId and key are required"})

    try:
        s3_client.abort_multipart_upload(
            Bucket=BUCKET_NAME, Key=key, UploadId=upload_id
        )
    except Exception:
        logger.exception(
            "failed to abort multipart upload", key=key, upload_id=upload_id
        )
        return _response(500, {"error": "Failed to abort upload"})

    logger.warning("multipart_aborted", key=key, upload_id=upload_id)
    metrics.add_metric(name="MultipartAborted", unit=MetricUnit.Count, value=1)
    return _response(200, {"ok": True})


def _handle_report_error(body: dict) -> dict:
    """Best-effort client-side upload failure report.

    Real uploads go straight from the browser to S3, so a failure there
    (network error, S3 rejection, expired presign, etc) is otherwise
    invisible server-side. The browser calls this on any upload failure -
    this is purely for observability, there's nothing to action here.
    """
    logger.warning(
        "upload_reported_failed",
        upload_filename=body.get("filename"),
        file_size=body.get("fileSize"),
        elapsed_ms=body.get("elapsedMs"),
        error=body.get("error"),
    )
    metrics.add_metric(name="UploadReportedFailed", unit=MetricUnit.Count, value=1)
    return _response(200, {"ok": True})


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
        logger.warning(f"could not verify uploader identity: {exc}")
        return {}

    return {
        "sub": user.sub,
        "email": user.email,
        "name": user.name,
        "given_name": user.given_name,
    }


def _uploader_metadata(claims: dict) -> dict:
    """Build the uploader-identity/upload-time metadata dict shared by both
    upload paths.

    Simple presigned POST turns this into x-amz-meta-* form fields/
    conditions (see _presigned_post_params); multipart passes it directly
    as create_multipart_upload's Metadata=, which S3 stores identically
    (surfaced the same way to backend_src/upload_logger's head_object call
    either way) - empty values are dropped in both cases, so a missing/
    unverifiable identity still produces a valid, just unattributed, upload.
    """
    metadata = {
        "uploaded-by-sub": claims.get("sub"),
        "uploaded-by-email": claims.get("email"),
        "uploaded-by-name": claims.get("name") or claims.get("given_name"),
        "uploaded-at": datetime.now(UTC).isoformat(),
    }
    return {key: value for key, value in metadata.items() if value}


def _presigned_post_params(content_type: str, claims: dict) -> dict:
    """Build the Fields/Conditions/ExpiresIn kwargs for generate_presigned_post."""
    fields = {"Content-Type": content_type}
    conditions = [
        {"Content-Type": content_type},
        ["content-length-range", 0, MAX_UPLOAD_BYTES],
    ]

    for meta_key, value in _uploader_metadata(claims).items():
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
