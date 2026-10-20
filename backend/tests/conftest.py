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

import io
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
# In-memory S3 (Phase 4.1)
# ---------------------------------------------------------------------------

class FakeS3Error(Exception):
    """Base for the induced failures the source-store tests need."""


def _client_error(code, operation):
    """Build a real botocore ClientError so production handlers are exercised.

    Constructing the genuine exception type matters: `source_store` branches on
    `exc.response["Error"]["Code"]`, and a stand-in would let that branch rot.
    """
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "induced by a test"}}, operation
    )


class FakeS3:
    """Minimal in-memory S3 covering the operations the backend performs.

    Objects are bytes in a dict keyed by (bucket, key). Range reads are
    honoured because the playback route forwards Range to S3 and returns 206
    from the response, and a fake that ignored ranges would make that
    untestable.

    Failures are induced by setting `fail_put`, `fail_get`, or `fail_delete`
    to a botocore error code, or to a BotoCoreError subclass for a transport
    failure.
    """

    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []
        self.deletes = []
        self.fail_put = None
        self.fail_get = None
        self.fail_delete = None

    # -- helpers for tests --------------------------------------------------

    def seed(self, bucket, key, data):
        self.objects[(bucket, key)] = bytes(data)

    def has(self, bucket, key):
        return (bucket, key) in self.objects

    def keys(self):
        return sorted(key for _, key in self.objects)

    # -- the boto3 surface --------------------------------------------------

    def _maybe_fail(self, setting, operation):
        if setting is None:
            return
        if isinstance(setting, type) and issubclass(setting, Exception):
            raise setting()
        if isinstance(setting, Exception):
            raise setting
        raise _client_error(setting, operation)

    def put_object(self, Bucket, Key, Body=None, **kwargs):
        self._maybe_fail(self.fail_put, "PutObject")
        data = Body.read() if hasattr(Body, "read") else (Body or b"")
        self.objects[(Bucket, Key)] = bytes(data)
        self.puts.append({"Bucket": Bucket, "Key": Key, **kwargs})
        return {"ETag": '"fake"'}

    def get_object(self, Bucket, Key, Range=None, **kwargs):
        self._maybe_fail(self.fail_get, "GetObject")
        self.gets.append({"Bucket": Bucket, "Key": Key, "Range": Range})
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")

        data = self.objects[(Bucket, Key)]
        total = len(data)
        result = {}

        if Range:
            start, _, end = Range.partition("=")[2].partition("-")
            first = int(start) if start else 0
            last = int(end) if end else total - 1
            if first >= total or first > last:
                raise _client_error("InvalidRange", "GetObject")
            last = min(last, total - 1)
            data = data[first : last + 1]
            result["ContentRange"] = f"bytes {first}-{last}/{total}"

        result["Body"] = io.BytesIO(data)
        result["ContentLength"] = len(data)
        return result

    def delete_object(self, Bucket, Key, **kwargs):
        self._maybe_fail(self.fail_delete, "DeleteObject")
        self.deletes.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture(autouse=True)
def fake_s3(monkeypatch):
    """Give every test an in-memory S3 for source-video storage.

    Autouse deliberately: `upload_source` is now on the upload path, so
    without this a test that uploads would attempt a real network call. Being
    autouse means no test in this suite can reach S3 by accident, which is a
    safety property as much as a convenience.

    Returns the FakeS3 so a test can seed objects, induce failures, or assert
    on the keys that were written.
    """
    import source_store

    store = FakeS3()
    monkeypatch.setattr(source_store, "_client", store)
    yield store
    monkeypatch.setattr(source_store, "_client", None, raising=False)


# ---------------------------------------------------------------------------
# In-memory DynamoDB frames table (Phase 4.1)
# ---------------------------------------------------------------------------

class FakeFramesTable:
    """In-memory stand-in for the DynamoDB frames table.

    Items are keyed (video_id, frame_number), which is the real table's key
    schema, so the idempotency property the pipeline relies on - re-running a
    frame overwrites rather than appends - holds here too.

    Only the operations the backend performs are implemented: put_item,
    update_item (SET expressions), query on the key condition, scan with a
    filter, and delete_item. Key conditions and filters are evaluated by
    inspecting the boto3 condition objects, so a query that works against the
    real table works here.

    Autouse via `fake_frames_table`, so no test can reach real DynamoDB.
    Tests that want to observe SDK calls still patch `analyzer.table`
    themselves, and their patch wins inside its context.
    """

    def __init__(self):
        self.items = {}
        self.deleted = []
        self.fail_query = None
        self.fail_delete = None

    # -- helpers for tests --------------------------------------------------

    def seed(self, item):
        self.items[(item["video_id"], int(item["frame_number"]))] = dict(item)

    def frame_numbers(self, video_id):
        return sorted(n for v, n in self.items if v == video_id)

    # -- condition evaluation ----------------------------------------------

    @staticmethod
    def _flatten(condition):
        """Expand a boto3 condition tree into (name, operator, values) triples."""
        from boto3.dynamodb.conditions import And, ConditionBase

        if isinstance(condition, And):
            out = []
            for part in condition.get_expression()["values"]:
                out.extend(FakeFramesTable._flatten(part))
            return out
        if isinstance(condition, ConditionBase):
            expression = condition.get_expression()
            values = list(expression["values"])
            name = getattr(values[0], "name", None)
            return [(name, expression["operator"], values[1:])]
        return []

    @classmethod
    def _satisfies(cls, item, condition):
        if condition is None:
            return True
        for name, operator, operands in cls._flatten(condition):
            actual = item.get(name)
            if operator == "=":
                if str(actual) != str(operands[0]):
                    return False
            elif operator in (">=", "gte"):
                if actual is None or int(actual) < int(operands[0]):
                    return False
            elif operator in ("<=", "lte"):
                if actual is None or int(actual) > int(operands[0]):
                    return False
            else:
                raise NotImplementedError(
                    f"FakeFramesTable does not implement the {operator!r} "
                    f"condition; add it here rather than changing the query "
                    f"under test"
                )
        return True

    # -- the boto3 resource surface ----------------------------------------

    def put_item(self, Item=None, **kwargs):
        item = dict(Item or {})
        self.items[(item["video_id"], int(item["frame_number"]))] = item
        return {}

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **kwargs):
        key = (Key["video_id"], int(Key["frame_number"]))
        item = self.items.setdefault(key, dict(Key))
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}

        body = UpdateExpression.strip()
        if body.upper().startswith("SET "):
            body = body[4:]
        for assignment in body.split(","):
            if "=" not in assignment:
                continue
            target, placeholder = assignment.split("=", 1)
            target = names.get(target.strip(), target.strip())
            item[target] = values[placeholder.strip()]
        return {}

    def query(self, KeyConditionExpression=None, **kwargs):
        if self.fail_query is not None:
            raise self.fail_query
        matched = [
            dict(item)
            for item in self.items.values()
            if self._satisfies(item, KeyConditionExpression)
        ]
        matched.sort(key=lambda i: int(i["frame_number"]))
        limit = kwargs.get("Limit")
        if limit:
            matched = matched[: int(limit)]
        return {"Items": matched}

    def scan(self, FilterExpression=None, **kwargs):
        matched = [
            dict(item)
            for item in self.items.values()
            if self._satisfies(item, FilterExpression)
        ]
        matched.sort(key=lambda i: int(i["frame_number"]))
        return {"Items": matched}

    def delete_item(self, Key=None, **kwargs):
        if self.fail_delete is not None:
            raise self.fail_delete
        key = (Key["video_id"], int(Key["frame_number"]))
        self.items.pop(key, None)
        self.deleted.append(key)
        return {}


@pytest.fixture(autouse=True)
def fake_frames_table(monkeypatch):
    """Replace the DynamoDB frames table everywhere it is bound."""
    table = FakeFramesTable()
    for module_name in ("analyzer", "extractor", "video_stitcher"):
        if module_name in sys.modules:
            monkeypatch.setattr(
                sys.modules[module_name], "table", table, raising=False
            )
    return table


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
            if key == "$or":
                # The lease filter is an $or over "unclaimed" / "no lease" /
                # "lease expired". Without this the claim query would match
                # nothing and every claim test would pass for the wrong
                # reason.
                if not any(self._matches(doc, clause) for clause in expected):
                    return False
                continue
            if key == "$and":
                if not all(self._matches(doc, clause) for clause in expected):
                    return False
                continue
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
                    elif op == "$lte":
                        if not (actual is not None and actual <= operand):
                            return False
                    elif op == "$gte":
                        if not (actual is not None and actual >= operand):
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

    async def delete_one(self, query):
        for index, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[index]

                class Result:
                    deleted_count = 1

                return Result()

        class Empty:
            deleted_count = 0

        return Empty()

    async def count_documents(self, query=None):
        query = query or {}
        return sum(1 for doc in self.docs if self._matches(doc, query))

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
