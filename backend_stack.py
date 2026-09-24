"""Backend stack for yeet: S3 uploads bucket + presigned-POST Lambda.

This stack deliberately does NOT provision its own ALB, Cognito client, or
compute platform - it is plain, minimal infrastructure. `attach_presign_route`
wires the presign Lambda into the *existing* ALB created by the `StaticSite`
(frontend) stack, via a Lambda target group and a listener rule for
`/api/presign` that reuses the frontend's existing Cognito authentication
action.

This keeps the whole app behind a single ALB and a single login/session -
there is no second Cognito client and no second sign-in flow. See app.py
for where `attach_presign_route` is called, once both stacks exist.
"""

from __future__ import annotations

from typing import Protocol

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from constructs import Construct
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig


class _AuthStrategyLike(Protocol):
    """Shape of gds_idea_cdk_constructs' internal IAuthStrategy we rely on.

    Not importing the real (underscore-private) IAuthStrategy type to avoid
    coupling to the library's internals more than we already do by reading
    `frontend_stack._auth_strategy` - this Protocol just documents the one
    method we call on it.
    """

    def create_listener_action(
        self, target_group: elbv2.IApplicationTargetGroup
    ) -> elbv2.ListenerAction: ...

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

    def attach_presign_route(
        self,
        *,
        https_listener: elbv2.ApplicationListener,
        vpc: ec2.IVpc,
        auth_strategy: _AuthStrategyLike,
        path_pattern: str = "/api/presign",
        priority: int = 10,
    ) -> None:
        """Attach the presign Lambda to an existing (frontend) ALB listener.

        This deliberately reuses the frontend stack's own resources rather
        than creating any of its own ALB/Cognito client:

        - `https_listener` / `vpc`: the frontend StaticSite stack's existing
          ALB listener and VPC (`frontend_stack.https_listener`,
          `frontend_stack.vpc`).
        - `auth_strategy`: the frontend stack's live auth strategy object
          (`frontend_stack._auth_strategy`), so the new rule is protected by
          the *exact same* Cognito user pool/client/session cookie as the
          rest of the site - no second ALB, no second login.

        Call this once both this backend stack and the frontend stack have
        been constructed, e.g. from app.py::

            backend_stack.attach_presign_route(
                https_listener=frontend_stack.https_listener,
                vpc=frontend_stack.vpc,
                auth_strategy=frontend_stack._auth_strategy,
            )

        The target group is created in *this* (backend) stack; the listener
        rule is created under the listener's own (frontend) stack. CDK
        resolves the cross-stack reference between them automatically.
        """
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "PresignTargetGroup",
            vpc=vpc,
            target_type=elbv2.TargetType.LAMBDA,
            targets=[elbv2_targets.LambdaTarget(self.presign_lambda)],
        )

        https_listener.add_action(
            "PresignRoute",
            priority=priority,
            conditions=[elbv2.ListenerCondition.path_patterns([path_pattern])],
            action=auth_strategy.create_listener_action(target_group),
        )
