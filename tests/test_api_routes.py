"""Tests for how the backend Lambdas are wired into the frontend's ALB.

Uses the real StaticSite (not a stand-in) so the Cognito client, ALB and
listener are the real ones - the COGNITO_AUTH_* pins only mean something if
they resolve to those resources.
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig
from gds_idea_cdk_constructs.config import DeploymentEnvironment
from gds_idea_cdk_constructs.static_site import (
    AuthType,
    StaticSite,
    StaticSiteProperties,
)

from backend_stack import DropBackendStack, add_api_routes

_ACCOUNT = DeploymentEnvironment.DEVELOPMENT.value
_REGION = "eu-west-2"
_CONFIG = {
    "domain_name": "example-test.gov.uk",
    "vpc_id": "vpc-0123456789abcdef0",
    "ecs_arn": f"arn:aws:ecs:{_REGION}:{_ACCOUNT}:cluster/test-cluster",
    "cognito_user_pool_id": "eu-west-2_TESTPOOL",
    "waf_arn": f"arn:aws:wafv2:{_REGION}:{_ACCOUNT}:regional/webacl/test/abc",
    "waf_big_upload_arn": (
        f"arn:aws:wafv2:{_REGION}:{_ACCOUNT}:regional/webacl/test-big/def"
    ),
    "logs_bucket_name": "test-alb-logs-bucket",
}
_PIN_VARS = {
    "COGNITO_AUTH_USER_POOL_ID",
    "COGNITO_AUTH_CLIENT_IDS",
    "COGNITO_AUTH_ALB_ARNS",
}


def _lookup_context() -> dict:
    """Context that satisfies StaticSite's VPC / hosted zone lookups offline."""
    vpc_id = _CONFIG["vpc_id"]
    domain = _CONFIG["domain_name"]
    return {
        f"availability-zones:account={_ACCOUNT}:region={_REGION}": [
            f"{_REGION}a",
            f"{_REGION}b",
        ],
        (
            f"vpc-provider:account={_ACCOUNT}:filter:vpc-id={vpc_id}:"
            f"region={_REGION}:returnAsymmetricSubnets=true"
        ): {
            "vpcId": vpc_id,
            "vpcCidrBlock": "10.0.0.0/16",
            "availabilityZones": [f"{_REGION}a", f"{_REGION}b"],
            "subnetGroups": [
                {
                    "name": "Public",
                    "type": "Public",
                    "subnets": [
                        {
                            "subnetId": f"subnet-test{i}",
                            "cidr": f"10.0.{i}.0/24",
                            "availabilityZone": f"{_REGION}{az}",
                            "routeTableId": f"rtb-test{i}",
                        }
                        for i, az in enumerate("ab")
                    ],
                }
            ],
        },
        f"hosted-zone:account={_ACCOUNT}:domainName={domain}:region={_REGION}": {
            "Id": "/hostedzone/ZTESTHOSTEDZONE",
            "Name": f"{domain}.",
        },
    }


@pytest.fixture
def stacks():
    """(frontend StaticSite, DropBackendStack) wired exactly as app.py does."""
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    app = cdk.App(context=_lookup_context())
    dep_config = DeploymentConfig.from_dict(env, _CONFIG)
    app_config = AppConfig(app_name="drop", framework="static")

    frontend = StaticSite(
        app,
        deployment_config=dep_config,
        app_config=app_config,
        authentication=AuthType.INTERNAL_ACCESS,
        docker_context_path="tests/fixtures/static_site",
        dockerfile_path="Dockerfile",
        static_site_props=StaticSiteProperties(build_command="true"),
    )
    backend = DropBackendStack(
        app,
        "drop-backend-stack",
        deployment_config=dep_config,
        app_config=app_config,
        env=env,
    )
    add_api_routes(frontend, backend)
    return app, frontend, backend


def _pattern(path: str) -> dict:
    return {"Field": "path-pattern", "PathPatternConfig": {"Values": [path]}}


def test_routes_are_created_in_the_backend_stack_behind_cognito_auth(stacks):
    _app, frontend, backend = stacks
    backend_template = Template.from_stack(backend)

    for priority, path in ((10, "/api/presign"), (11, "/api/admin/*")):
        backend_template.has_resource_properties(
            "AWS::ElasticLoadBalancingV2::ListenerRule",
            {
                "Priority": priority,
                "Conditions": Match.array_with([_pattern(path)]),
                # Same authentication action as the rest of the site.
                "Actions": Match.array_with(
                    [Match.object_like({"Type": "authenticate-cognito"})]
                ),
            },
        )

    # The frontend owns the listener but none of the API rules.
    Template.from_stack(frontend).resource_count_is(
        "AWS::ElasticLoadBalancingV2::ListenerRule", 0
    )


def test_dependency_only_points_from_backend_to_frontend(stacks):
    app, frontend, backend = stacks

    app.synth()  # raises on a cyclic cross-stack reference

    assert frontend in backend.dependencies
    assert backend not in frontend.dependencies


def test_target_group_logical_ids_are_stable(stacks):
    """Changing these would make CloudFormation replace the live target group."""
    _app, _frontend, backend = stacks
    target_groups = Template.from_stack(backend).find_resources(
        "AWS::ElasticLoadBalancingV2::TargetGroup"
    )

    prefixes = sorted(name[:-8] for name in target_groups)  # strip 8-char hash
    assert prefixes == ["AdminUploadsTargetGroup", "PresignTargetGroup"]


def test_elb_invoke_permissions_are_scoped_to_this_accounts_target_groups(stacks):
    """CDK's default leaves the Lambdas invokable by any ALB in any account."""
    _app, _frontend, backend = stacks
    permissions = Template.from_stack(backend).find_resources(
        "AWS::Lambda::Permission",
        {"Properties": {"Principal": "elasticloadbalancing.amazonaws.com"}},
    )

    assert len(permissions) == 2
    for permission in permissions.values():
        source_arn = json.dumps(permission["Properties"]["SourceArn"])
        assert f"elasticloadbalancing:{_REGION}:{_ACCOUNT}:targetgroup/*" in source_arn


@pytest.mark.parametrize("marker", ["MAX_UPLOAD_BYTES", "DOWNLOAD_URL_EXPIRY_SECONDS"])
def test_token_verifying_lambdas_are_pinned_to_our_pool_client_and_alb(stacks, marker):
    """cognito-auth must only trust this app's pool, app client and ALB."""
    _app, _frontend, backend = stacks
    functions = Template.from_stack(backend).find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Environment": {"Variables": {marker: Match.any_value()}}}},
    )
    assert len(functions) == 1
    env = next(iter(functions.values()))["Properties"]["Environment"]["Variables"]

    assert _PIN_VARS <= set(env)
    assert env["COGNITO_AUTH_USER_POOL_ID"] == "eu-west-2_TESTPOOL"
    # The client and ALB belong to the frontend stack: these are imports of
    # its exports, not literals, so they can't drift from the real resources.
    assert "Fn::ImportValue" in json.dumps(env["COGNITO_AUTH_CLIENT_IDS"])
    assert "Fn::ImportValue" in json.dumps(env["COGNITO_AUTH_ALB_ARNS"])


def test_pins_resolve_to_the_frontends_client_and_load_balancer(stacks):
    _app, frontend, backend = stacks
    frontend_template = Template.from_stack(frontend).to_json()
    exports = {
        output["Export"]["Name"]: output["Value"]
        for output in frontend_template["Outputs"].values()
        if "Export" in output
    }
    resources = frontend_template["Resources"]

    def exported_type(import_name: str) -> str:
        value = exports[import_name]
        return resources[value["Ref"] if "Ref" in value else value["Fn::GetAtt"][0]][
            "Type"
        ]

    backend_json = Template.from_stack(backend).to_json()
    lambdas = [
        r
        for r in backend_json["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
        and "COGNITO_AUTH_CLIENT_IDS"
        in r["Properties"].get("Environment", {}).get("Variables", {})
    ]
    assert len(lambdas) == 2

    for function in lambdas:
        env = function["Properties"]["Environment"]["Variables"]
        client = env["COGNITO_AUTH_CLIENT_IDS"]["Fn::ImportValue"]
        alb = env["COGNITO_AUTH_ALB_ARNS"]["Fn::ImportValue"]
        assert exported_type(client) == "AWS::Cognito::UserPoolClient"
        assert exported_type(alb) == "AWS::ElasticLoadBalancingV2::LoadBalancer"


def test_no_lambda_has_a_function_url(stacks):
    _app, _frontend, backend = stacks
    Template.from_stack(backend).resource_count_is("AWS::Lambda::Url", 0)
