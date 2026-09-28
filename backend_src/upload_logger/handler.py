"""S3 ObjectCreated handler - confirms an upload actually completed.

Uploads go straight from the browser to S3 via a presigned POST/PUT (see
backend_src/presign/handler.py) - the presign Lambda only ever learns that a
URL was *issued*, never that it was *used*. This closes that gap: it fires
whenever an object actually lands under uploads/, fetches the
uploader-identity metadata already attached to the object (set by the
presign Lambda), and logs one confirmation record.

Deliberately dependency-free (stdlib + boto3 only, both already present in
the Lambda runtime) - this function has nothing else to do, so it doesn't
need the presign Lambda's local-pip bundling machinery.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

import boto3

logger = logging.getLogger("drop-upload-logger")
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")


def handler(event, context):
    for record in event.get("Records", []):
        _log_one(record)


def _log_one(record: dict) -> None:
    s3_info = record.get("s3", {})
    bucket = s3_info.get("bucket", {}).get("name")
    raw_key = s3_info.get("object", {}).get("key")
    size = s3_info.get("object", {}).get("size")

    if not bucket or not raw_key:
        logger.warning(
            json.dumps(
                {"event": "upload_confirm_skipped", "reason": "missing bucket/key"}
            )
        )
        return

    # S3 event notification keys are URL-encoded (e.g. spaces as '+').
    key = urllib.parse.unquote_plus(raw_key)

    metadata = {}
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        metadata = head.get("Metadata", {})
    except Exception as exc:  # noqa: BLE001 - log and still record what we know
        logger.warning(
            json.dumps(
                {"event": "upload_confirm_head_failed", "key": key, "error": str(exc)}
            )
        )

    logger.info(
        json.dumps(
            {
                "event": "upload_confirmed",
                "key": key,
                "size": size,
                "uploaded_by_email": metadata.get("uploaded-by-email"),
                "uploaded_by_name": metadata.get("uploaded-by-name"),
                "uploaded_at": metadata.get("uploaded-at"),
            }
        )
    )
