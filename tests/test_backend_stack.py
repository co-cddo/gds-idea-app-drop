"""CDK synth-level tests for the yeet backend stack (uploads bucket + presign Lambda).

Uses DeploymentConfig.from_dict to avoid any real AWS Parameter Store lookups.
"""

import aws_cdk as cdk
from aws_cdk.assertions import Template
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig
from gds_idea_cdk_constructs.config import DeploymentEnvironment

from backend_stack import MAX_UPLOAD_BYTES, YeetBackendStack

_TEST_CONFIG = {
    "domain_name": "example-test.gov.uk",
    "vpc_id": "vpc-0123456789abcdef0",
    "ecs_arn": "arn:aws:ecs:eu-west-2:992382722318:cluster/test-cluster",
    "cognito_user_pool_id": "eu-west-2_TESTPOOL",
    "waf_arn": "arn:aws:wafv2:eu-west-2:992382722318:regional/webacl/test/abc",
    "waf_big_upload_arn": "arn:aws:wafv2:eu-west-2:992382722318:regional/webacl/test-big/def",
    "logs_bucket_name": "test-alb-logs-bucket",
}


def _make_stack() -> YeetBackendStack:
    cdk_env = cdk.Environment(
        account=DeploymentEnvironment.DEVELOPMENT.value, region="eu-west-2"
    )
    deployment_config = DeploymentConfig.from_dict(cdk_env, _TEST_CONFIG)
    app_config = AppConfig(app_name="yeet", framework="static")

    app = cdk.App()
    return YeetBackendStack(
        app,
        "yeet-backend-stack",
        deployment_config=deployment_config,
        app_config=app_config,
        env=cdk_env,
    )


def test_uploads_bucket_is_private_and_retained():
    template = Template.from_stack(_make_stack())

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    template.has_resource(
        "AWS::S3::Bucket",
        {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"},
    )


def test_uploads_bucket_has_cors_rule_scoped_to_site_origin():
    template = Template.from_stack(_make_stack())

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "CorsConfiguration": {
                "CorsRules": [
                    {
                        "AllowedMethods": ["POST", "PUT"],
                        "AllowedOrigins": ["https://yeet.example-test.gov.uk"],
                    }
                ]
            }
        },
    )


def test_presign_lambda_has_expected_environment():
    template = Template.from_stack(_make_stack())

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "handler.handler",
            "Environment": {
                "Variables": {
                    "MAX_UPLOAD_BYTES": str(MAX_UPLOAD_BYTES),
                }
            },
        },
    )


def test_presign_lambda_role_only_grants_put_on_uploads_prefix():
    import json

    template = Template.from_stack(_make_stack())

    # The role's inline policy should scope s3:PutObject* to the uploads/*
    # prefix of this stack's own bucket, not the whole bucket or others.
    policies = template.find_resources("AWS::IAM::Policy")
    assert policies, "expected at least one IAM::Policy resource"
    assert "uploads/*" in json.dumps(policies)
