"""Backend stack for yeet: S3 uploads bucket + presigned-POST Lambda.

This stack deliberately does NOT provision its own ALB, Cognito client, or
compute platform - it is plain, minimal infrastructure. It is designed to be
wired into the *existing* ALB created by the `StaticSite` (frontend) stack in
`app.py`, via a Lambda target group and a listener rule for `/api/presign`
that reuses the frontend's existing Cognito authentication action.

This keeps the whole app behind a single ALB and a single login/session -
there is no second Cognito client and no second sign-in flow. See app.py
for the wiring (added in a separate stage).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from constructs import Construct
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig

# S3 presigned POST forms support up to ~5GiB per file. This is a placeholder
# ceiling for the prototype - multipart/resumable uploads for larger files
# are a separate piece of future work, tracked separately from this build.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB

# How long a presigned POST URL/fields remain valid after being issued.
PRESIGN_EXPIRY_SECONDS = 15 * 60  # 15 minutes


class YeetBackendStack(cdk.Stack):
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
        self.site_origin = f"https://{app_config.app_name}.{deployment_config.domain_name}"

        self.uploads_bucket = self._create_uploads_bucket()
        self.presign_lambda = self._create_presign_lambda()

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
                    max_age=3000,
                )
            ],
        )

    def _create_presign_lambda(self) -> _lambda.Function:
        """Lambda that generates presigned POST URLs. Own dedicated role.

        Deliberately not reusing the frontend stack's shared task_role - this
        function only ever needs s3:Put* on the uploads/ prefix of its own
        bucket, nothing else.
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

        return _lambda.Function(
            self,
            "PresignLambda",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=_lambda.Code.from_asset("backend_src/presign"),
            role=role,
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "UPLOADS_BUCKET_NAME": self.uploads_bucket.bucket_name,
                "MAX_UPLOAD_BYTES": str(MAX_UPLOAD_BYTES),
                "PRESIGN_EXPIRY_SECONDS": str(PRESIGN_EXPIRY_SECONDS),
            },
        )

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
