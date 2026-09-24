#!/usr/bin/env python3
import os

import aws_cdk as cdk
from aws_cdk import Duration, Tags
from aws_cdk import aws_events as events
from gds_idea_cdk_constructs import AppConfig, DeploymentConfig
from gds_idea_cdk_constructs.static_site import (
    AuthType,
    StaticSite,
    StaticSiteProperties,
)

from backend_stack import YeetBackendStack

app = cdk.App()
cdk_env = cdk.Environment(
    account=os.environ["CDK_DEFAULT_ACCOUNT"],
    region=os.environ["CDK_DEFAULT_REGION"],
)

app_config = AppConfig.from_pyproject()
dep_config = DeploymentConfig(cdk_env)

stack_tags = {
    "Environment": dep_config.environment.friendly_name,
    "ManagedBy": "cdk",
    "Repository": "co-cddo/gds-idea-app-yeet",
    "AppName": app_config.app_name,
    "Owner": "David Gillespie",
}

for key, value in stack_tags.items():
    Tags.of(app).add(key, value)
    Tags.of(app).add(key, value, include_resource_types=["aws:cdk:stack"])


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

# Backend resources (uploads bucket + presign Lambda) live in their own
# stack, but are NOT given their own ALB/Cognito client. Instead,
# attach_presign_route wires the presign Lambda into the frontend's
# *existing* ALB, on a dedicated /api/presign route that reuses the same
# Cognito auth action as the site itself. This keeps the whole app behind a
# single login/session - there is no second ALB and no second OAuth client.
backend_stack = YeetBackendStack(
    app,
    f"{app_config.app_name}-backend-stack",
    deployment_config=dep_config,
    app_config=app_config,
    env=cdk_env,
)

backend_stack.attach_presign_route(
    https_listener=stack.https_listener,
    vpc=stack.vpc,
    auth_strategy=stack._auth_strategy,
)

app.synth()
