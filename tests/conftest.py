"""Shared test setup.

The presign Lambda handler reads UPLOADS_BUCKET_NAME at import time, so it
must be set before that module is first imported by any test.
"""

import os

# Force-set (not setdefault) fake credentials for every test run. Every real
# S3 call in backend_src is meant to be mocked - but if a test ever forgets
# to (as happened during development: an unmocked create_multipart_upload
# call actually succeeded against a real bucket, because this machine had
# live AWS credentials ambiently available), this guarantees it fails loudly
# with an auth error instead of silently hitting real AWS. Env vars take
# priority over profile/SSO credentials in boto3's resolution chain, so this
# overrides whatever's ambiently active for the whole test process.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "eu-west-2"

os.environ.setdefault("UPLOADS_BUCKET_NAME", "test-uploads-bucket")
