#!/usr/bin/env python3
import os

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_events as events
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig, IdeaTags
from gds_idea_cdk_constructs.static_site import (
    AuthType,
    StaticSite,
    StaticSiteProperties,
)

from backend_stack import DropBackendStack, add_api_routes

app = cdk.App()
cdk_env = cdk.Environment(
    account=os.environ["CDK_DEFAULT_ACCOUNT"],
    region=os.environ["CDK_DEFAULT_REGION"],
)

app_config = AppConfig.from_pyproject()
dep_config = DeploymentConfig(cdk_env)

IdeaTags(
    environment=dep_config.environment,
    app_name=app_config.app_name,
    repository="gds-idea-app-drop",
    owners=["David Gillespie"],
).apply(app)

stack = StaticSite(
    app,
    deployment_config=dep_config,
    app_config=app_config,
    authentication=AuthType.INTERNAL_ACCESS,
    docker_context_path="site_src",
    dockerfile_path="Dockerfile",
    static_site_props=StaticSiteProperties(
        build_command="npx @11ty/eleventy",
        build_schedule=events.Schedule.rate(Duration.hours(6)),
    ),
)

# Backend resources (uploads bucket + Lambdas) live in their own stack, but
# are NOT given their own ALB/Cognito client. Instead, add_lambda_route wires
# each Lambda into the frontend's *existing* ALB, behind the same Cognito auth
# action as the site itself, so the whole app shares one login/session.
#
# The target groups and listener rules are created in the backend stack (the
# first argument), so the dependency only points backend -> frontend. The
# library also scopes each Lambda's ELB invoke permission to this account and
# sets COGNITO_AUTH_USER_POOL_ID / _CLIENT_IDS / _ALB_ARNS on it, so
# cognito-auth only trusts this app's user pool, app client and ALB.
backend_stack = DropBackendStack(
    app,
    f"{app_config.app_name}-backend-stack",
    deployment_config=dep_config,
    app_config=app_config,
    env=cdk_env,
)

add_api_routes(stack, backend_stack)

app.synth()
