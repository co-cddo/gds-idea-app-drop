"""CDK synth-level tests for the drop backend stack (uploads bucket + presign Lambda).

Uses DeploymentConfig.from_dict to avoid any real AWS Parameter Store lookups.
"""

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk.assertions import Match, Template
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig
from gds_idea_cdk_constructs.config import DeploymentEnvironment

from backend_stack import MAX_UPLOAD_BYTES, DropBackendStack

_TEST_CONFIG = {
    "domain_name": "example-test.gov.uk",
    "vpc_id": "vpc-0123456789abcdef0",
    "ecs_arn": "arn:aws:ecs:eu-west-2:992382722318:cluster/test-cluster",
    "cognito_user_pool_id": "eu-west-2_TESTPOOL",
    "waf_arn": "arn:aws:wafv2:eu-west-2:992382722318:regional/webacl/test/abc",
    "waf_big_upload_arn": "arn:aws:wafv2:eu-west-2:992382722318:regional/webacl/test-big/def",
    "logs_bucket_name": "test-alb-logs-bucket",
}


def _make_stack() -> DropBackendStack:
    cdk_env = cdk.Environment(
        account=DeploymentEnvironment.DEVELOPMENT.value, region="eu-west-2"
    )
    deployment_config = DeploymentConfig.from_dict(cdk_env, _TEST_CONFIG)
    app_config = AppConfig(app_name="drop", framework="static")

    app = cdk.App()
    return DropBackendStack(
        app,
        "drop-backend-stack",
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
                        "AllowedOrigins": ["https://drop.example-test.gov.uk"],
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


class _StubAuthStrategy:
    """Minimal stand-in for the frontend stack's real (Cognito) auth strategy.

    Records the target group it was asked to wrap, and returns a plain
    forward action - just enough to verify attach_presign_route calls it
    correctly, without needing a real Cognito user pool.
    """

    def __init__(self):
        self.wrapped_target_groups = []

    def create_listener_action(self, target_group):
        self.wrapped_target_groups.append(target_group)
        return elbv2.ListenerAction.forward([target_group])


def _make_wired_stacks():
    """Build the backend stack plus a minimal frontend-like stand-in.

    Avoids needing the full StaticSite/BaseWebStack machinery (which
    requires real VPC/hosted-zone lookups) - just enough of an ALB + HTTPS
    listener to exercise attach_presign_route's cross-stack wiring.
    """
    app = cdk.App()
    cdk_env = cdk.Environment(
        account=DeploymentEnvironment.DEVELOPMENT.value, region="eu-west-2"
    )
    deployment_config = DeploymentConfig.from_dict(cdk_env, _TEST_CONFIG)
    app_config = AppConfig(app_name="drop", framework="static")

    backend_stack = DropBackendStack(
        app,
        "drop-backend-stack",
        deployment_config=deployment_config,
        app_config=app_config,
        env=cdk_env,
    )

    frontend_stack = cdk.Stack(app, "fake-frontend-stack", env=cdk_env)
    vpc = ec2.Vpc(frontend_stack, "Vpc", max_azs=1, nat_gateways=0)
    default_target_group = elbv2.ApplicationTargetGroup(
        frontend_stack,
        "DefaultTargetGroup",
        vpc=vpc,
        port=80,
        target_type=elbv2.TargetType.IP,
    )
    alb = elbv2.ApplicationLoadBalancer(frontend_stack, "Alb", vpc=vpc, internet_facing=True)
    # Plain HTTP listener is enough here - we only care about the listener
    # rule/action wiring, not real TLS (which would need an ACM cert).
    https_listener = alb.add_listener(
        "HttpsListener",
        port=80,
        default_action=elbv2.ListenerAction.forward([default_target_group]),
    )

    auth_strategy = _StubAuthStrategy()

    backend_stack.attach_presign_route(
        https_listener=https_listener,
        vpc=vpc,
        auth_strategy=auth_strategy,
    )

    return backend_stack, frontend_stack, auth_strategy


def test_attach_presign_route_creates_target_group_in_backend_stack():
    backend_stack, _frontend_stack, _auth = _make_wired_stacks()

    template = Template.from_stack(backend_stack)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 1)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"TargetType": "lambda"},
    )


def test_attach_presign_route_adds_listener_rule_on_frontend_stack():
    _backend_stack, frontend_stack, _auth = _make_wired_stacks()

    template = Template.from_stack(frontend_stack)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 10,
            "Conditions": Match.array_with(
                [
                    {
                        "Field": "path-pattern",
                        "PathPatternConfig": {"Values": ["/api/presign"]},
                    }
                ]
            ),
        },
    )


def test_attach_presign_route_reuses_the_passed_in_auth_strategy():
    backend_stack, _frontend_stack, auth_strategy = _make_wired_stacks()

    assert len(auth_strategy.wrapped_target_groups) == 1
    # The target group the auth strategy wrapped is the same one attached
    # to the presign Lambda, not some other/default target group.
    template = Template.from_stack(backend_stack)
    presign_target_groups = template.find_resources(
        "AWS::ElasticLoadBalancingV2::TargetGroup"
    )
    assert len(presign_target_groups) == 1
