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

from backend_stack import DropBackendStack

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

# Backend resources (uploads bucket + presign Lambda) live in their own
# stack, but are NOT given their own ALB/Cognito client. Instead,
# attach_presign_route wires the presign Lambda into the frontend's
# *existing* ALB, on a dedicated /api/presign route that reuses the same
# Cognito auth action as the site itself. This keeps the whole app behind a
# single login/session - there is no second ALB and no second OAuth client.
backend_stack = DropBackendStack(
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
