#!/usr/bin/env python3
"""Benchmark the drop app's upload path outside a browser.

Invokes the real deployed presign Lambda *directly* (bypassing the ALB and
Cognito login flow entirely - that hop is already known to be fast/small,
it's not what this measures) with a synthetic ALB event, then performs the
actual upload against S3 using whatever the Lambda returns - a single
presigned POST for small files, or a set of per-part presigned URLs for
large ones (once multipart support lands). Reports wall-clock time and
throughput, so we have a repeatable, real number instead of one manual
browser test.

This is deliberately a standalone script, not a pytest test: it hits real
AWS resources (a real Lambda invocation, real S3 PUT/POST traffic, real
storage - cleaned up afterwards) and can take many minutes for large sizes.
It must never run automatically in CI.

Usage:
    uv run python scripts/benchmark_upload.py --size 2.5GB
    uv run python scripts/benchmark_upload.py --size 500MB --stack-name drop-backend-stack
    uv run python scripts/benchmark_upload.py --size 100MB --function-name arn:aws:lambda:... \\
        --bucket-name my-uploads-bucket

Requires AWS credentials (e.g. via --profile, or already exported) with:
    - lambda:InvokeFunction on the presign Lambda
    - s3:HeadObject / s3:DeleteObject on the uploads bucket
    - cloudformation:DescribeStacks (only if resolving via --stack-name)

Refuses to run against anything that looks like a prod resource unless
--allow-prod is passed explicitly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import boto3
import requests

DEFAULT_STACK_NAME = "drop-backend-stack"
DEFAULT_REGION = "eu-west-2"
DEFAULT_CONCURRENCY = 6

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMG]?B)?\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
}


def parse_size(text: str) -> int:
    """Parse a human size string ("2.5GB", "500MB", "1024") into bytes."""
    match = _SIZE_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(
            f"Could not parse size '{text}' - expected e.g. '2.5GB', '500MB', '1024'"
        )
    value, unit = match.groups()
    unit = (unit or "B").upper()
    return int(float(value) * _SIZE_MULTIPLIERS[unit])


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} GB"


class SyntheticFile:
    """A read()-able synthetic byte source of a given size.

    Behaves like a file object for `requests`' multipart encoder, without
    ever holding the whole payload in memory at once - only one chunk at a
    time is materialised.

    Implements __len__ deliberately: without it, `requests` can't determine
    Content-Length upfront for a raw `data=<file-like>` PUT (as opposed to
    a `files=` multipart body, which it reads fully into memory first) and
    falls back to `Transfer-Encoding: chunked` - which S3 rejects outright
    for presigned uploads with a 501 NotImplemented. Discovered by actually
    running this against real S3 part URLs, not just unit tests.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._remaining = size

    def __len__(self) -> int:
        return self._size

    def read(self, n: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        chunk = self._remaining if n is None or n < 0 else min(n, self._remaining)
        self._remaining -= chunk
        return b"\0" * chunk


@dataclass
class PartResult:
    part_number: int
    seconds: float
    bytes_sent: int
    etag: str | None = None
    error: str | None = None


@dataclass
class BenchmarkResult:
    key: str
    total_bytes: int
    multipart: bool
    total_seconds: float = 0.0
    presign_seconds: float = 0.0
    complete_seconds: float = 0.0
    parts: list[PartResult] = field(default_factory=list)
    verified: bool = False

    @property
    def throughput_mbps(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return (self.total_bytes / (1024 * 1024)) / self.total_seconds

    def report(self) -> str:
        lines = [
            "",
            "=== Upload benchmark result ===",
            f"Key:              {self.key}",
            f"Size:             {format_size(self.total_bytes)} ({self.total_bytes} bytes)",
            f"Mode:             {'multipart' if self.multipart else 'simple presigned POST'}",
            f"Presign call:     {self.presign_seconds:.2f}s",
            f"Upload transfer:  {self.total_seconds:.2f}s",
            f"Throughput:       {self.throughput_mbps:.2f} MB/s",
        ]
        if self.multipart:
            lines.append(f"Complete call:    {self.complete_seconds:.2f}s")
            ok = [p for p in self.parts if p.error is None]
            failed = [p for p in self.parts if p.error is not None]
            if ok:
                durations = sorted(p.seconds for p in ok)
                lines.append(
                    f"Parts:            {len(ok)} ok, {len(failed)} failed "
                    f"(min {durations[0]:.2f}s / max {durations[-1]:.2f}s / "
                    f"avg {sum(durations) / len(durations):.2f}s)"
                )
            for p in failed:
                lines.append(f"  part {p.part_number} FAILED: {p.error}")
        lines.append(f"Verified in S3:   {'yes' if self.verified else 'NO'}")
        lines.append("================================")
        return "\n".join(lines)


def resolve_targets(
    *,
    stack_name: str | None,
    function_name: str | None,
    bucket_name: str | None,
    region: str,
    session: boto3.Session,
) -> tuple[str, str]:
    """Return (function_arn_or_name, bucket_name), resolving via CloudFormation if needed."""
    if function_name and bucket_name:
        return function_name, bucket_name

    cfn = session.client("cloudformation", region_name=region)
    stack_name = stack_name or DEFAULT_STACK_NAME
    print(f"Resolving Lambda/bucket from CloudFormation stack '{stack_name}'...")
    outputs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])
    values = {o["OutputKey"]: o["OutputValue"] for o in outputs}

    resolved_function = function_name or values.get("PresignLambdaArn")
    resolved_bucket = bucket_name or values.get("UploadsBucketName")

    if not resolved_function or not resolved_bucket:
        raise SystemExit(
            f"Could not resolve PresignLambdaArn/UploadsBucketName from stack "
            f"'{stack_name}' outputs: {values}. Pass --function-name and "
            f"--bucket-name explicitly instead."
        )
    return resolved_function, resolved_bucket


def guard_against_prod(*names: str, allow_prod: bool) -> None:
    if allow_prod:
        return
    for name in names:
        if "prod" in name.lower():
            raise SystemExit(
                f"Refusing to run: '{name}' looks like a production resource. "
                f"Re-run with --allow-prod if you really mean this."
            )


def alb_event(body: dict) -> dict:
    """Build a synthetic ALB Lambda-target event, matching the shape the real
    ALB sends (see tests/test_presign_handler.py::_alb_event). No auth headers
    are included - the handler treats a missing/invalid identity as
    unattributed rather than an error, which is fine for a benchmark."""
    return {
        "httpMethod": "POST",
        "path": "/api/presign",
        "headers": {},
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def invoke_presign_lambda(lambda_client, function_name: str, body: dict) -> dict:
    response = lambda_client.invoke(
        FunctionName=function_name,
        Payload=json.dumps(alb_event(body)).encode("utf-8"),
    )
    if response.get("FunctionError"):
        raise SystemExit(
            f"Presign Lambda invocation failed: {response['Payload'].read()!r}"
        )

    payload = json.loads(response["Payload"].read())
    status_code = payload.get("statusCode")
    if status_code != 200:
        raise SystemExit(
            f"Presign Lambda returned {status_code}: {payload.get('body')}"
        )

    return json.loads(payload["body"])


def run_simple_upload(
    *, presigned: dict, size_bytes: int, filename: str, content_type: str
) -> BenchmarkResult:
    """Single presigned-POST upload (today's only path, and the path used for
    small files even after multipart support lands)."""
    fields = presigned["fields"]
    key = presigned["key"]
    url = presigned["url"]

    # The "file" field must be last in the multipart body - S3 requires it.
    files = [(k, (None, v)) for k, v in fields.items()]
    files.append(("file", (filename, SyntheticFile(size_bytes), content_type)))

    result = BenchmarkResult(key=key, total_bytes=size_bytes, multipart=False)

    start = time.monotonic()
    response = requests.post(url, files=files, timeout=None)
    result.total_seconds = time.monotonic() - start

    if response.status_code not in (200, 201, 204):
        raise SystemExit(
            f"Upload failed: status={response.status_code} body={response.text[:2000]}"
        )

    return result


def _upload_one_part(
    session: requests.Session, part: dict, size_bytes: int
) -> PartResult:
    start = time.monotonic()
    try:
        response = session.put(
            part["url"], data=SyntheticFile(size_bytes), timeout=None
        )
        elapsed = time.monotonic() - start
        if not response.ok:
            return PartResult(
                part_number=part["partNumber"],
                seconds=elapsed,
                bytes_sent=size_bytes,
                error=f"status={response.status_code} body={response.text[:500]}",
            )
        etag = response.headers.get("ETag", "").strip('"')
        return PartResult(
            part_number=part["partNumber"],
            seconds=elapsed,
            bytes_sent=size_bytes,
            etag=etag,
        )
    except requests.RequestException as exc:
        return PartResult(
            part_number=part["partNumber"],
            seconds=time.monotonic() - start,
            bytes_sent=size_bytes,
            error=str(exc),
        )


def run_multipart_upload(
    *,
    presigned: dict,
    size_bytes: int,
    concurrency: int,
    lambda_client,
    function_name: str,
) -> BenchmarkResult:
    """Multipart upload path (available once the presign Lambda supports it -
    the response shape below is the agreed contract for that feature)."""
    key = presigned["key"]
    upload_id = presigned["uploadId"]
    part_size = presigned["partSize"]
    parts_meta = presigned["parts"]

    result = BenchmarkResult(key=key, total_bytes=size_bytes, multipart=True)

    start = time.monotonic()
    with (
        requests.Session() as session,
        ThreadPoolExecutor(max_workers=concurrency) as pool,
    ):
        futures = []
        for part in parts_meta:
            part_number = part["partNumber"]
            is_last = part_number == len(parts_meta)
            this_part_size = (
                size_bytes - part_size * (len(parts_meta) - 1) if is_last else part_size
            )
            futures.append(pool.submit(_upload_one_part, session, part, this_part_size))

        for future in as_completed(futures):
            result.parts.append(future.result())

    result.total_seconds = time.monotonic() - start
    result.parts.sort(key=lambda p: p.part_number)

    failed = [p for p in result.parts if p.error is not None]
    if failed:
        print(f"{len(failed)} part(s) failed:")
        for p in failed:
            print(f"  part {p.part_number}: {p.error}")
        print("Attempting to abort the multipart upload.")
        invoke_presign_lambda(
            lambda_client,
            function_name,
            {"action": "abort", "uploadId": upload_id, "key": key},
        )
        raise SystemExit(
            "Multipart upload had failed parts; aborted. See part errors above."
        )

    complete_start = time.monotonic()
    invoke_presign_lambda(
        lambda_client,
        function_name,
        {
            "action": "complete",
            "uploadId": upload_id,
            "key": key,
            "parts": [
                {"partNumber": p.part_number, "eTag": p.etag} for p in result.parts
            ],
        },
    )
    result.complete_seconds = time.monotonic() - complete_start

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--size",
        type=parse_size,
        required=True,
        help="File size to simulate, e.g. 2.5GB, 500MB",
    )
    parser.add_argument(
        "--stack-name",
        default=None,
        help=f"CloudFormation stack to resolve resources from (default: {DEFAULT_STACK_NAME})",
    )
    parser.add_argument(
        "--function-name",
        default=None,
        help="Presign Lambda name/ARN (skips CloudFormation lookup if given with --bucket-name)",
    )
    parser.add_argument(
        "--bucket-name",
        default=None,
        help="Uploads bucket name (skips CloudFormation lookup if given with --function-name)",
    )
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None, help="AWS profile to use")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Parallel part uploads for multipart",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Don't delete the test object from S3 afterwards",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="Allow running against a resource whose name contains 'prod'",
    )
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    lambda_client = session.client("lambda")
    s3_client = session.client("s3")

    function_name, bucket_name = resolve_targets(
        stack_name=args.stack_name,
        function_name=args.function_name,
        bucket_name=args.bucket_name,
        region=args.region,
        session=session,
    )
    guard_against_prod(
        function_name, bucket_name, args.stack_name or "", allow_prod=args.allow_prod
    )

    filename = f"benchmark-{uuid.uuid4().hex[:8]}.bin"
    content_type = "application/octet-stream"

    print(
        f"Requesting presign for {format_size(args.size)} ({args.size} bytes) as '{filename}'..."
    )
    presign_start = time.monotonic()
    presigned = invoke_presign_lambda(
        lambda_client,
        function_name,
        {"filename": filename, "contentType": content_type, "fileSize": args.size},
    )
    presign_seconds = time.monotonic() - presign_start

    if "uploadId" in presigned:
        print(f"Lambda selected multipart mode: {len(presigned['parts'])} part(s).")
        result = run_multipart_upload(
            presigned=presigned,
            size_bytes=args.size,
            concurrency=args.concurrency,
            lambda_client=lambda_client,
            function_name=function_name,
        )
    else:
        print("Lambda selected simple presigned-POST mode.")
        result = run_simple_upload(
            presigned=presigned,
            size_bytes=args.size,
            filename=filename,
            content_type=content_type,
        )

    result.presign_seconds = presign_seconds

    print("Verifying object landed in S3...")
    try:
        head = s3_client.head_object(Bucket=bucket_name, Key=result.key)
        result.verified = head["ContentLength"] == args.size
    except Exception as exc:  # noqa: BLE001 - report and continue to cleanup
        print(f"Could not verify object: {exc}")

    if not args.keep:
        print("Cleaning up test object...")
        s3_client.delete_object(Bucket=bucket_name, Key=result.key)
    else:
        print(f"Leaving test object in place: s3://{bucket_name}/{result.key}")

    print(result.report())
    return 0 if result.verified else 1


if __name__ == "__main__":
    sys.exit(main())
