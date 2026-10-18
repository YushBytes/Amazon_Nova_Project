"""Shared test configuration for the NovaFlow backend.

Two things happen here:

1. The backend directory is placed on ``sys.path`` so tests can import the
   application modules by their flat names (``import analyzer``), which is how
   the application itself imports them at runtime.

2. Dummy AWS/Mongo environment variables are set *before* any backend module is
   imported. Several backend modules build boto3 clients at module scope
   (``analyzer.py``, ``projects.py``, ``extractor.py``), so the environment has
   to look plausible at import time. Client construction performs no network
   I/O, and every test patches the client objects, so no real AWS call is ever
   made. The credentials below are obvious non-secrets used only to stop botocore
   from searching the developer's real credential chain.
"""

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Deterministic, non-secret test configuration. Set before backend imports.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("S3_BUCKET_NAME", "test-bucket")
os.environ.setdefault("DYNAMODB_TABLE_NAME", "TestFrames")
os.environ.setdefault("MONGODB_URL", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_DB_NAME", "novaflow_test")

import pytest  # noqa: E402  (import after sys.path/env setup, intentionally)


@pytest.fixture
def frame_jpeg_bytes() -> bytes:
    """A minimal JPEG-shaped byte string.

    The analyzer never decodes the image itself; it forwards the bytes to
    Bedrock (mocked in tests). Only the SOI/EOI markers matter for realism.
    """
    return b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
