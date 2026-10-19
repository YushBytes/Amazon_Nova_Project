"""Shared test configuration for the NovaFlow backend.

Three things happen here:

1. The backend directory is placed on ``sys.path`` so tests can import the
   application modules by their flat names (``import analyzer``), which is how
   the application itself imports them at runtime.

2. Dummy AWS/Mongo environment variables and a synthetic JWT signing key are
   set *before* any backend module is imported. Several backend modules build
   boto3 clients at module scope (``analyzer.py``, ``projects.py``,
   ``extractor.py``) and ``app.py`` resolves security settings at import time,
   so the environment has to look plausible. Client construction performs no
   network I/O, and every test patches the client objects, so no real AWS call
   is ever made. The values below are obvious non-secrets used only to stop
   botocore from searching the developer's real credential chain and to give
   the token tests a deterministic key.

3. An in-memory MongoDB substitute is provided, so authentication, signup,
   and video-ownership behaviour can be tested without a running database.
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

# Synthetic signing key for the test process only. Long enough to satisfy the
# production validator and not present in config.KNOWN_INSECURE_SECRETS, so the
# token tests exercise the real validation path.
os.environ.setdefault(
    "JWT_SECRET_KEY", "pytest-only-synthetic-signing-key-0123456789abcdef"
)
os.environ.setdefault("NOVAFLOW_ENV", "development")

import pytest  # noqa: E402  (import after sys.path/env setup, intentionally)


# ---------------------------------------------------------------------------
# Rate limiter isolation
# ---------------------------------------------------------------------------

def _find_rate_limiter(application):
    """Locate the live RateLimitMiddleware instance in an app's stack."""
    from rate_limit import RateLimitMiddleware

    if getattr(application, "middleware_stack", None) is None:
        # Starlette builds this on first request; build it now so the
        # instance exists and can be reset before the test runs.
        application.middleware_stack = application.build_middleware_stack()

    node = application.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    return None


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Give every test a fresh rate-limit budget.

    The limiter is real in tests (it is a security control, so disabling it
    would hide regressions), but its counters live for the life of the
    process. Without this reset, a test that logs in repeatedly would throttle
    whichever test happened to run next.
    """
    import app as app_module

    limiter = _find_rate_limiter(app_module.app)
    if limiter is not None:
        limiter.reset()
    yield
    if limiter is not None:
        limiter.reset()


# ---------------------------------------------------------------------------
# Frames / analyzer fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def frame_jpeg_bytes() -> bytes:
    """A minimal JPEG-shaped byte string.

    The analyzer never decodes the image itself; it forwards the bytes to
    Bedrock (mocked in tests). Only the SOI/EOI markers matter for realism.
    """
    return b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"


# ---------------------------------------------------------------------------
# In-memory MongoDB substitute
# ---------------------------------------------------------------------------

class FakeCursor:
    """Supports the ``find(...).sort(...).limit(...)`` async-iteration chain."""

    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: d.get(field) or 0, reverse=direction == -1)
        return self

    def limit(self, count):
        self._docs = self._docs[:count]
        return self

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc

        return gen()


class FakeCollection:
    """Minimal async collection covering the operations the backend uses.

    Deliberately simple: documents are plain dicts in a list. It implements
    just enough of the motor API (``find_one``, ``insert_one``,
    ``update_one``, ``find``, ``create_index``) for the authentication and
    ownership code paths.
    """

    def __init__(self, name):
        self.name = name
        self.docs = []
        self.indexes = []
        self._counter = 0

    def _matches(self, doc, query):
        """Match a document against a query.

        Supports plain equality plus the handful of operators the backend
        actually uses, so a query that works against the real driver also
        works here.
        """
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict):
                for op, operand in expected.items():
                    if op == "$in":
                        if actual not in operand:
                            return False
                    elif op == "$nin":
                        if actual in operand:
                            return False
                    elif op == "$ne":
                        if actual == operand:
                            return False
                    elif op == "$exists":
                        if (key in doc) is not bool(operand):
                            return False
                    elif op == "$gt":
                        if not (actual is not None and actual > operand):
                            return False
                    elif op == "$lt":
                        if not (actual is not None and actual < operand):
                            return False
                    else:
                        raise NotImplementedError(
                            f"FakeCollection does not implement {op!r}; add it "
                            f"here rather than changing the query under test"
                        )
            elif actual != expected:
                return False
        return True

    async def find_one(self, query):
        for doc in self.docs:
            if self._matches(doc, query):
                return dict(doc)
        return None

    async def insert_one(self, document):
        from pymongo.errors import DuplicateKeyError

        self._counter += 1
        stored = dict(document)
        stored.setdefault("_id", f"{self.name}-{self._counter}")
        # Honour every unique index that has been declared, and raise the same
        # exception type the real driver raises, so application code that
        # catches DuplicateKeyError is genuinely exercised.
        for field, unique in self.indexes:
            if not unique:
                continue
            value = stored.get(field)
            if any(d.get(field) == value for d in self.docs):
                raise DuplicateKeyError(f"E11000 duplicate key error: {field}")
        self.docs.append(stored)

        class Result:
            inserted_id = stored["_id"]

        return Result()

    async def update_one(self, query, update):
        matched = modified = 0
        for doc in self.docs:
            if self._matches(doc, query):
                matched = 1
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                modified = 1
                break

        class Result:
            matched_count = matched
            modified_count = modified

        return Result()

    def find(self, query=None):
        query = query or {}
        return FakeCursor(
            [dict(doc) for doc in self.docs if self._matches(doc, query)]
        )

    async def create_index(self, field, unique=False):
        """Declare an index.

        Mirrors the real driver in the way that matters here: creating a
        unique index on a collection that already holds duplicates fails.
        """
        from pymongo.errors import DuplicateKeyError

        if unique:
            seen = set()
            for doc in self.docs:
                value = doc.get(field)
                if value in seen:
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error: {field} already duplicated"
                    )
                seen.add(value)
        if (field, unique) not in self.indexes:
            self.indexes.append((field, unique))
        return field

    def insert_raw(self, document):
        """Insert bypassing index checks, to seed pre-existing bad data."""
        self._counter += 1
        stored = dict(document)
        stored.setdefault("_id", f"{self.name}-{self._counter}")
        self.docs.append(stored)
        return stored


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name)
        return self.collections[name]


@pytest.fixture
def fake_db(monkeypatch):
    """Patch ``db.get_database`` so every consumer sees one in-memory database.

    Patched on the ``db`` module *and* on the modules that imported the
    function by name, because ``from db import get_database`` binds a separate
    reference.
    """
    import db as db_module

    database = FakeDatabase()

    async def _get_database():
        return database

    monkeypatch.setattr(db_module, "get_database", _get_database)

    for module_name in ("auth", "video_repo", "db_init"):
        if module_name in sys.modules:
            monkeypatch.setattr(
                sys.modules[module_name], "get_database", _get_database, raising=False
            )

    return database
