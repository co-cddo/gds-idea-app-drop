"""Shared test setup.

The presign Lambda handler reads UPLOADS_BUCKET_NAME at import time, so it
must be set before that module is first imported by any test.
"""

import os

os.environ.setdefault("UPLOADS_BUCKET_NAME", "test-uploads-bucket")
