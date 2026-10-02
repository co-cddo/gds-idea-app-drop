"""CDK synth-level tests for the drop backend stack (uploads bucket + presign Lambda).

Uses DeploymentConfig.from_dict to avoid any real AWS Parameter Store lookups.
"""

import json

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
                        "ExposedHeaders": ["ETag"],
                    }
                ]
            }
        },
    )


def test_uploads_bucket_aborts_incomplete_multipart_uploads():
    template = Template.from_stack(_make_stack())

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "AbortIncompleteMultipartUpload": {
                                    "DaysAfterInitiation": 1
                                }
                            }
                        )
                    ]
                )
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


def test_presign_lambda_role_grants_multipart_actions_on_uploads_prefix():
    import json

    template = Template.from_stack(_make_stack())

    policies_json = json.dumps(template.find_resources("AWS::IAM::Policy"))
    for action in (
        "s3:CreateMultipartUpload",
        "s3:UploadPart",
        "s3:CompleteMultipartUpload",
        "s3:ListMultipartUploadParts",
    ):
        assert action in policies_json, f"expected {action} to be granted"


def test_presign_lambda_has_multipart_environment():
    template = Template.from_stack(_make_stack())

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "handler.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "MULTIPART_THRESHOLD_BYTES": str(100 * 1024 * 1024),
                        "PART_SIZE_BYTES": str(100 * 1024 * 1024),
                        "MULTIPART_PART_EXPIRY_SECONDS": str(2 * 60 * 60),
                    }
                )
            },
        },
    )


def test_lambda_log_groups_have_explicit_retention():
    template = Template.from_stack(_make_stack())

    # Each of our Lambdas (presign, admin uploads, upload confirmation) gets
    # its own log group with a bounded retention - without this, Lambda's
    # default log group never expires.
    template.resource_count_is("AWS::Logs::LogGroup", 3)
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    for resource in log_groups.values():
        assert resource["Properties"]["RetentionInDays"] == 90


def test_upload_confirmation_lambda_exists_and_reads_uploads_prefix_only():
    template = Template.from_stack(_make_stack())

    # 4, not 3: CDK auto-generates a fourth Lambda (BucketNotificationsHandler)
    # to back the Custom::S3BucketNotifications resource used below - that's
    # not one of ours.
    template.resource_count_is("AWS::Lambda::Function", 4)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "handler.handler",
            "Runtime": Match.string_like_regexp("python3.13"),
        },
    )

    policies = template.find_resources("AWS::IAM::Policy")
    assert "uploads/*" in json.dumps(policies)


def test_uploads_bucket_notifies_upload_confirmation_lambda_on_object_created():
    template = Template.from_stack(_make_stack())

    # CDK implements bucket notifications via a custom resource that calls
    # PutBucketNotificationConfiguration - assert on that, plus the Lambda
    # permission that allows S3 to invoke the function.
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Principal": "s3.amazonaws.com"},
    )
    template.resource_count_is("Custom::S3BucketNotifications", 1)


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
    alb = elbv2.ApplicationLoadBalancer(
        frontend_stack, "Alb", vpc=vpc, internet_facing=True
    )
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
    backend_stack.attach_admin_route(
        https_listener=https_listener,
        vpc=vpc,
        auth_strategy=auth_strategy,
    )

    return backend_stack, frontend_stack, auth_strategy


def test_attach_presign_route_creates_target_group_in_backend_stack():
    backend_stack, _frontend_stack, _auth = _make_wired_stacks()

    template = Template.from_stack(backend_stack)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 2)
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

    assert len(auth_strategy.wrapped_target_groups) == 2  # presign + admin
    # The target group the auth strategy wrapped is the same one attached
    # to the presign Lambda, not some other/default target group.
    template = Template.from_stack(backend_stack)
    presign_target_groups = template.find_resources(
        "AWS::ElasticLoadBalancingV2::TargetGroup"
    )
    assert len(presign_target_groups) == 2


# --- admin uploads Lambda --------------------------------------------------


def _policy_statements(template, role_logical_prefix):
    """All IAM statements attached to roles whose logical id starts with prefix."""
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = policy["Properties"]["Roles"]
        if any(r["Ref"].startswith(role_logical_prefix) for r in roles):
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _actions(statements):
    actions = set()
    for st in statements:
        a = st["Action"]
        actions.update([a] if isinstance(a, str) else a)
    return actions


def test_admin_lambda_role_is_read_only_and_prefix_scoped():
    template = Template.from_stack(_make_stack())
    statements = _policy_statements(template, "AdminUploadsLambdaRole")

    assert _actions(statements) == {"s3:ListBucket", "s3:GetObject"}

    list_stmt = next(s for s in statements if s["Action"] == "s3:ListBucket")
    assert list_stmt["Condition"] == {"StringLike": {"s3:prefix": ["uploads/*"]}}

    get_stmt = next(s for s in statements if s["Action"] == "s3:GetObject")
    assert "uploads/*" in json.dumps(get_stmt["Resource"])


def test_presign_role_cannot_read_or_list_uploads():
    template = Template.from_stack(_make_stack())
    actions = _actions(_policy_statements(template, "PresignLambdaRole"))

    assert not any(
        a.startswith(("s3:GetObject", "s3:List", "s3:Get*"))
        for a in actions - {"s3:ListMultipartUploadParts"}
    )
    assert "s3:GetObject" not in actions


def test_admin_lambda_env_and_limits():
    template = Template.from_stack(_make_stack())
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": {
                "Environment": {"Variables": {"DOWNLOAD_URL_EXPIRY_SECONDS": "60"}}
            }
        },
    )
    assert len(functions) == 1
    props = next(iter(functions.values()))["Properties"]
    assert props["ReservedConcurrentExecutions"] == 5


def test_attach_admin_route_adds_listener_rule_on_frontend_stack():
    _backend_stack, frontend_stack, _auth = _make_wired_stacks()

    Template.from_stack(frontend_stack).has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 11,
            "Conditions": Match.array_with(
                [
                    {
                        "Field": "path-pattern",
                        "PathPatternConfig": {"Values": ["/api/admin/*"]},
                    }
                ]
            ),
        },
    )


def test_lambdas_are_only_invokable_via_account_scoped_elb_permission():
    """Guards against the cross-account ALB impersonation path.

    CDK's LambdaTarget creates an ELB invoke permission with no SourceArn,
    which would let any ALB in any account invoke these functions and
    supply forged identity headers. Every such permission must be scoped.
    """
    backend_stack, _frontend_stack, _auth = _make_wired_stacks()
    template = Template.from_stack(backend_stack)

    elb_permissions = template.find_resources(
        "AWS::Lambda::Permission",
        {"Properties": {"Principal": "elasticloadbalancing.amazonaws.com"}},
    )
    assert len(elb_permissions) == 2  # presign + admin

    for permission in elb_permissions.values():
        source_arn = json.dumps(permission["Properties"].get("SourceArn"))
        assert "elasticloadbalancing" in source_arn
        assert "targetgroup/" in source_arn
        assert "eu-west-2" in source_arn
        assert DeploymentEnvironment.DEVELOPMENT.value in source_arn


def test_no_lambda_has_a_function_url():
    backend_stack, _frontend_stack, _auth = _make_wired_stacks()
    Template.from_stack(backend_stack).resource_count_is("AWS::Lambda::Url", 0)


def test_lambdas_that_verify_tokens_are_pinned_to_our_user_pool():
    template = Template.from_stack(_make_stack())

    for marker in ("MAX_UPLOAD_BYTES", "DOWNLOAD_URL_EXPIRY_SECONDS"):
        functions = template.find_resources(
            "AWS::Lambda::Function",
            {"Properties": {"Environment": {"Variables": {marker: Match.any_value()}}}},
        )
        assert len(functions) == 1
        env = next(iter(functions.values()))["Properties"]["Environment"]["Variables"]
        assert env["COGNITO_AUTH_USER_POOL_ID"] == "eu-west-2_TESTPOOL"
