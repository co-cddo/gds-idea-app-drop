"""Backend stack for drop: S3 uploads bucket + Lambdas behind the site's ALB.

This stack deliberately does NOT provision its own ALB, Cognito client, or
compute platform - it is plain, minimal infrastructure. app.py wires the
presign and admin-uploads Lambdas into the *existing* ALB created by the
`StaticSite` (frontend) stack with `StaticSite.add_lambda_route`, which:

- creates each route's target group and listener rule in THIS stack (on the
  frontend's listener), behind the frontend's existing Cognito authentication
  action - so the whole app shares one ALB and one login/session;
- scopes each Lambda's ELB invoke permission to this account's target groups;
- sets the COGNITO_AUTH_* env vars that make `cognito-auth` trust only this
  app's user pool, app client and ALB.

Because everything is created here, the dependency only points from this
stack to the frontend stack, never back.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

import aws_cdk as cdk
import jsii
from aws_cdk import BundlingOptions, CfnOutput, Duration, ILocalBundling, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from constructs import Construct
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig
from gds_idea_cdk_constructs.static_site import StaticSite

logger = logging.getLogger(__name__)


@jsii.implements(ILocalBundling)
class _LocalPipBundling:
    """Local (no-Docker) bundling using uv pip, for Linux-platform wheels.

    Mirrors the exact pattern gds_idea_cdk_constructs' own StaticSite
    construct already uses successfully for its ServeLambda's dependencies.
    `uv pip install --python-platform` downloads the already-built
    manylinux wheel directly from PyPI (no compilation, no container
    needed) - this sidesteps the permission/cross-device-rename issues
    that Docker-based bundling hit with pip installing into a bind-mounted
    /asset-output on macOS Docker Desktop.
    """

    def __init__(self, source_path: str) -> None:
        self._source_path = source_path

    def try_bundle(self, output_dir: str, *, image, **kwargs) -> bool:
        try:
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--no-installer-metadata",
                    "--no-compile-bytecode",
                    "--python-platform",
                    "x86_64-manylinux2014",
                    "--python",
                    "3.13",
                    "--extra-index-url",
                    "https://co-cddo.github.io/gds-idea-pypi/simple/",
                    "-r",
                    f"{self._source_path}/requirements.txt",
                    "--target",
                    output_dir,
                    "--quiet",
                ],
                check=True,
                capture_output=True,
            )
            shutil.copy(f"{self._source_path}/handler.py", output_dir)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning(f"Local uv bundling failed: {exc}")
            return False


# S3 presigned POST forms support up to ~5GiB per file. This is a placeholder
# ceiling for the prototype - multipart/resumable uploads for larger files
# are a separate piece of future work, tracked separately from this build.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB

# How long a presigned POST URL/fields remain valid after being issued.
PRESIGN_EXPIRY_SECONDS = 15 * 60  # 15 minutes

# Files above this use S3 multipart upload instead of a single presigned
# POST - see backend_src/presign/handler.py's module docstring for why.
MULTIPART_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MiB

# 100MB parts -> max 50 parts for a 5GB file, well under S3's 10,000-part
# limit, with room to raise MAX_UPLOAD_BYTES later if needed.
PART_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB

# Generous relative to PRESIGN_EXPIRY_SECONDS - covers a bounded-concurrency
# client working through many parts plus retries, not just one part's
# upload time.
MULTIPART_PART_EXPIRY_SECONDS = 2 * 60 * 60  # 2 hours

# Admin download links are bearer credentials; keep them short-lived.
DOWNLOAD_URL_EXPIRY_SECONDS = 60

# Applied to every Lambda log group in this stack.
LOG_RETENTION = logs.RetentionDays.THREE_MONTHS


class DropBackendStack(cdk.Stack):
    """Owns the uploads bucket and the presign Lambda. No ALB, no auth of its own."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_config: DeploymentConfig,
        app_config: AppConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.deployment_config = deployment_config
        self.app_config = app_config

        # Must match the frontend StaticSite stack's subdomain (alb_domain_name).
        self.site_origin = (
            f"https://{app_config.app_name}.{deployment_config.domain_name}"
        )

        self.uploads_bucket = self._create_uploads_bucket()
        self.presign_lambda = self._create_presign_lambda()
        self.admin_uploads_lambda = self._create_admin_uploads_lambda()
        self.upload_confirmation_lambda = self._create_upload_confirmation_lambda()

        self._create_outputs()

    def _create_uploads_bucket(self) -> s3.Bucket:
        """Private bucket that receives civil servants' uploaded files.

        RETAIN is used deliberately: unlike the frontend's content bucket
        (rebuildable static assets, safe to DESTROY), this bucket holds real
        uploaded files and must survive stack teardown/recreation.
        """
        return s3.Bucket(
            self,
            "UploadsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST, s3.HttpMethods.PUT],
                    allowed_origins=[self.site_origin],
                    allowed_headers=["*"],
                    # ETag isn't one of the browser's CORS-safelisted
                    # response headers - without this, the multipart
                    # upload flow can't read a part's ETag after PUTting
                    # it (needed to complete the upload), and
                    # response.headers.get('ETag') silently returns null.
                    exposed_headers=["ETag"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                # Multipart uploads that never get completed or explicitly
                # aborted (abandoned tab, crashed browser, etc) otherwise
                # sit there costing storage forever.
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(1)
                ),
            ],
        )

    def _create_presign_lambda(self) -> _lambda.Function:
        """Lambda that generates presigned POST URLs. Own dedicated role.

        Deliberately not reusing the frontend stack's shared task_role - this
        function only ever needs s3:Put* on the uploads/ prefix of its own
        bucket, nothing else.

        The handler depends on `cognito-auth`, which pulls in pydantic-core -
        a compiled dependency needing Linux-platform wheels. Bundled via
        `_LocalPipBundling` (uv, no Docker) rather than cross-compiling or
        running pip inside a container - see that class's docstring.
        """
        role = iam.Role(
            self,
            "PresignLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        self.uploads_bucket.grant_put(role, "uploads/*")
        # grant_put covers PutObject*/Abort* (so AbortMultipartUpload is
        # already included) - these four are distinct IAM actions not
        # covered by that wildcard, needed for the multipart upload path.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:CreateMultipartUpload",
                    "s3:UploadPart",
                    "s3:CompleteMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                resources=[self.uploads_bucket.arn_for_objects("uploads/*")],
            )
        )

        presign_source_path = "backend_src/presign"

        presign_log_group = logs.LogGroup(
            self,
            "PresignLambdaLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        return _lambda.Function(
            self,
            "PresignLambda",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=_lambda.Code.from_asset(
                presign_source_path,
                bundling=BundlingOptions(
                    image=_lambda.Runtime.PYTHON_3_13.bundling_image,
                    local=_LocalPipBundling(presign_source_path),
                    # Only runs if local (uv) bundling fails - deliberately
                    # not a real Docker fallback (matches the platform's own
                    # precedent): better to fail loudly and fix the local
                    # path than silently mask a bundling problem.
                    command=["bash", "-c", "echo 'Local uv bundling failed' && exit 1"],
                ),
            ),
            role=role,
            # 10s was enough when this only ever did local presign signing;
            # create_multipart_upload is a real network call to S3, so this
            # has some margin now for that plus a cold start.
            timeout=Duration.seconds(15),
            memory_size=256,
            log_group=presign_log_group,
            environment={
                "UPLOADS_BUCKET_NAME": self.uploads_bucket.bucket_name,
                "MAX_UPLOAD_BYTES": str(MAX_UPLOAD_BYTES),
                "PRESIGN_EXPIRY_SECONDS": str(PRESIGN_EXPIRY_SECONDS),
                "MULTIPART_THRESHOLD_BYTES": str(MULTIPART_THRESHOLD_BYTES),
                "PART_SIZE_BYTES": str(PART_SIZE_BYTES),
                "MULTIPART_PART_EXPIRY_SECONDS": str(MULTIPART_PART_EXPIRY_SECONDS),
            },
        )

    def _create_admin_uploads_lambda(self) -> _lambda.Function:
        """Lambda behind /api/admin/*: lists uploads and issues download URLs.

        Read-only, and the only principal in this stack that can read uploaded
        content via the web. Own dedicated role, scoped to the uploads/ prefix
        - deliberately NOT sharing the presign role (write-only) or the
        frontend task role. Who may call it (the `gds-idea` group) is enforced
        in the handler; see backend_src/admin_uploads/handler.py.
        """
        role = iam.Role(
            self,
            "AdminUploadsLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[self.uploads_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": ["uploads/*"]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[self.uploads_bucket.arn_for_objects("uploads/*")],
            )
        )

        source_path = "backend_src/admin_uploads"

        log_group = logs.LogGroup(
            self,
            "AdminUploadsLambdaLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        return _lambda.Function(
            self,
            "AdminUploadsLambda",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=_lambda.Code.from_asset(
                source_path,
                bundling=BundlingOptions(
                    image=_lambda.Runtime.PYTHON_3_13.bundling_image,
                    local=_LocalPipBundling(source_path),
                    command=["bash", "-c", "echo 'Local uv bundling failed' && exit 1"],
                ),
            ),
            role=role,
            # Listing lists every key and then heads one page of objects.
            timeout=Duration.seconds(30),
            memory_size=256,
            # Caps cost/blast radius if an admin session is abused.
            reserved_concurrent_executions=5,
            log_group=log_group,
            environment={
                "UPLOADS_BUCKET_NAME": self.uploads_bucket.bucket_name,
                "DOWNLOAD_URL_EXPIRY_SECONDS": str(DOWNLOAD_URL_EXPIRY_SECONDS),
            },
        )

    def _create_upload_confirmation_lambda(self) -> _lambda.Function:
        """Fires on every object created under uploads/.

        This is the only server-side confirmation that an upload (for which
        the presign Lambda only ever issues a URL) actually completed - the
        transfer itself goes straight from the browser to S3. See
        backend_src/upload_logger/handler.py.
        """
        role = iam.Role(
            self,
            "UploadConfirmationLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        self.uploads_bucket.grant_read(role, "uploads/*")

        log_group = logs.LogGroup(
            self,
            "UploadConfirmationLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        function = _lambda.Function(
            self,
            "UploadConfirmationLambda",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.handler",
            # Stdlib + boto3 only (both already in the runtime) - unlike
            # PresignLambda, this has no third-party deps, so no bundling
            # config is needed at all.
            code=_lambda.Code.from_asset("backend_src/upload_logger"),
            role=role,
            timeout=Duration.seconds(10),
            memory_size=128,
            log_group=log_group,
        )

        self.uploads_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(function),
            s3.NotificationKeyFilter(prefix="uploads/"),
        )

        return function

    def _create_outputs(self) -> None:
        CfnOutput(
            self,
            "UploadsBucketName",
            value=self.uploads_bucket.bucket_name,
            description="S3 bucket receiving uploaded files",
        )
        CfnOutput(
            self,
            "PresignLambdaArn",
            value=self.presign_lambda.function_arn,
            description="ARN of the presigned-POST Lambda",
        )
        CfnOutput(
            self,
            "AdminUploadsLambdaArn",
            value=self.admin_uploads_lambda.function_arn,
            description="ARN of the admin uploads list/download Lambda",
        )
        CfnOutput(
            self,
            "UploadConfirmationLambdaArn",
            value=self.upload_confirmation_lambda.function_arn,
            description="ARN of the Lambda that logs confirmed uploads",
        )


def add_api_routes(frontend: StaticSite, backend: DropBackendStack) -> None:
    """Route the backend Lambdas through the frontend's ALB.

    Shared by app.py and the tests so they cannot drift apart. See the module
    docstring for what `add_lambda_route` does.

    The route ids ("Presign", "AdminUploads") give the target groups the
    logical ids PresignTargetGroup / AdminUploadsTargetGroup. The presign one
    already has that id, so CloudFormation keeps it rather than replacing it.
    """
    frontend.add_lambda_route(
        backend,
        "Presign",
        function=backend.presign_lambda,
        path_patterns=["/api/presign"],
        priority=10,
    )

    # Authorisation (gds-idea group) for this one is enforced in the Lambda.
    frontend.add_lambda_route(
        backend,
        "AdminUploads",
        function=backend.admin_uploads_lambda,
        path_patterns=["/api/admin/*"],
        priority=11,
    )
