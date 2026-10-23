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

import base64
import importlib
import io
import os
import struct
import sys
import zlib
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
def _reset_aws_client_memo():
    """Drop aws_clients' memoized clients around every test.

    Phase 6 made clients process-wide and memoized. Without this, a test that
    injects a fake through `aws_clients.get_client` could leak it into the next
    test, or a real client built before a patch took effect could be reused
    afterwards - either way the test would pass or fail for the wrong reason.
    """
    import aws_clients

    aws_clients.reset()
    yield
    aws_clients.reset()


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

    # `source_store` lazily builds its client, but `analyzer`, `extractor` and
    # `video_stitcher` each bind one at MODULE SCOPE (see the note in
    # CLAUDE.md §8 about boto3 clients built at import time). Patching only
    # `source_store` left those three pointing at a real boto3 client, so the
    # safety property claimed above did not actually hold for them.
    #
    # It went unnoticed because the only tests that reach `extractor`'s S3 put
    # are the real-FFmpeg extraction tests, which skipped on every host that
    # had no FFmpeg. Phase 9.1 installed FFmpeg, they ran, and they attempted
    # a real `PutObject` against AWS - failing on InvalidAccessKeyId rather
    # than being mocked. Binding the fake here is what makes the fixture's
    # promise true.
    # Imported rather than only patched-if-already-present: whether these
    # modules happen to be in sys.modules depends on which test files were
    # selected, and a safety property that holds only for a full-suite run is
    # not a safety property. Importing them performs no I/O - the boto3 client
    # is constructed, not connected.
    for module_name in ("analyzer", "extractor", "video_stitcher"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "s3_client", store, raising=False)

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
        # Items read per Query page. None means "everything in one page".
        # Set it to force the multi-page path a real table takes at 1 MB.
        self.page_size = None
        # Call records, so a test can assert HOW the table was read - which is
        # the point of the Phase 6 scan-to-query change, not just the result.
        self.query_calls = []
        self.scan_calls = []

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

    def get_item(self, Key=None, **kwargs):
        """Read one frame row by its full key.

        Phase 8: the remediation routes address a single finding by
        (video_id, frame_number), so a point read is the correct and cheapest
        call. Mirrors the real service in the way that matters - a miss
        returns a response with NO "Item" key rather than raising.
        """
        key = (Key["video_id"], int(Key["frame_number"]))
        item = self.items.get(key)
        return {"Item": dict(item)} if item is not None else {}

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

    def query(self, KeyConditionExpression=None, FilterExpression=None, **kwargs):
        """Query, with the paging semantics the real service has.

        Phase 6 moved `check_pending_frames` from Scan to Query, so this has
        to model the parts of Query that change the caller's correctness:

        * `FilterExpression` is applied AFTER the key condition, exactly as
          DynamoDB does - which is why a filtered page can come back empty and
          still carry a `LastEvaluatedKey`. A fake that ignored the filter
          would make the new query look correct whatever it asked for.
        * `Limit` bounds items READ, before filtering, and a truncated read
          returns `LastEvaluatedKey`. Code that stops at the first empty page
          silently loses frames; `page_size` below lets a test force that.
        * `ExclusiveStartKey` resumes after a given key.
        """
        if self.fail_query is not None:
            raise self.fail_query

        ordered = sorted(self.items.values(), key=lambda i: int(i["frame_number"]))
        ordered = [i for i in ordered if self._satisfies(i, KeyConditionExpression)]

        start = kwargs.get("ExclusiveStartKey")
        if start:
            after = int(start["frame_number"])
            ordered = [i for i in ordered if int(i["frame_number"]) > after]

        # Items read, which is what Limit and page_size bound.
        budget = kwargs.get("Limit") or self.page_size
        truncated = False
        if budget and len(ordered) > int(budget):
            ordered = ordered[: int(budget)]
            truncated = True

        self.query_calls.append(dict(kwargs, KeyConditionExpression=KeyConditionExpression))

        page = [dict(i) for i in ordered if self._satisfies(i, FilterExpression)]
        response = {"Items": page}
        if truncated and ordered:
            last = ordered[-1]
            response["LastEvaluatedKey"] = {
                "video_id": last["video_id"],
                "frame_number": last["frame_number"],
            }
        return response

    def scan(self, FilterExpression=None, **kwargs):
        self.scan_calls.append(dict(kwargs, FilterExpression=FilterExpression))
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
# Replacement-image fixtures
# ---------------------------------------------------------------------------

def real_png(width: int = 16, height: int = 16, rgb=(220, 40, 40)) -> bytes:
    """A genuinely decodable PNG, built with the standard library alone.

    Phase 9.1. Until now every test that attached a replacement image used
    ``b"\\x89PNG\\r\\n\\x1a\\n" + b"\\x00" * N`` - the PNG magic number followed
    by padding. That is not an image: nothing can decode it. It satisfied the
    old validation, which only sniffed the first eight bytes, so the whole
    remediation workflow was exercised with a file that could never have been
    reconstructed from.

    Worse, that exact shape is the payload that makes FFmpeg **block** when it
    is given as a ``-loop 1`` overlay input, so the fixture was the hang. The
    asset layer now probes for decodability and rejects it, which is correct -
    and which is why a valid-path test needs a real image here.

    Deliberately stdlib-only (``zlib`` + ``struct``): no Pillow, no committed
    binary fixture, and no dependency on FFmpeg being installed to *build* the
    bytes. 16x16 clears ``remediation_assets.MIN_ASSET_PIXELS`` and the whole
    file is ~79 bytes.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # 8-bit truecolour (colour type 2), no interlacing.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Each scanline is a filter byte (0 = None) followed by RGB triples.
    raw = (b"\x00" + bytes(rgb) * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# A real 16x16 baseline JPEG, 224 bytes, produced once with
#   ffmpeg -f lavfi -i color=c=red:size=16x16 -frames:v 1 -q:v 31 real.jpg
# and embedded so the fixture needs neither FFmpeg nor Pillow at test time.
# A JPEG cannot be hand-assembled from zlib the way a PNG can - it needs
# quantisation and Huffman tables - so this is a literal rather than a builder.
_REAL_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAPTGF2YzYzLjEuMTAxAP/bAEMACD4+ST5JVVVV"
    "VVVVZF1kaGhoZGRkZGhoaHBwcIODg3BwcGhocHB8fIODj5OPh4eDh5OTm5uburqystnZ"
    "4P/////EAEwAAQEAAAAAAAAAAAAAAAAAAAAGAQEBAAAAAAAAAAAAAAAAAAAGBxABAAAA"
    "AAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIABAAEAMBIgACEQADEQD/"
    "2gAMAwEAAhEDEQA/AIsATX9//9k="
)


def real_jpeg() -> bytes:
    """A genuinely decodable JPEG. See `real_png` for why this matters."""
    return base64.b64decode(_REAL_JPEG_B64)


@pytest.fixture
def replacement_png() -> bytes:
    """A real, decodable replacement image for the happy path."""
    return real_png()


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
        # Set to an exception to make the readiness ping fail, which is how a
        # test simulates an unreachable database without touching the network.
        self.fail_command = None

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name)
        return self.collections[name]

    async def command(self, name, *args, **kwargs):
        """The admin-command surface, which the readiness probe uses to ping.

        Mirrors motor: `command("ping")` returns `{"ok": 1}` when the server
        answers, and raises otherwise. Without this the probe would fail with
        AttributeError and the "ready" test would pass for the wrong reason -
        reporting not-ready because the fake was incomplete rather than
        because a dependency was down.
        """
        if self.fail_command is not None:
            raise self.fail_command
        if name == "ping":
            return {"ok": 1}
        return {"ok": 1, "command": name}


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

    # Every module that does `from db import get_database`, which binds its
    # own reference. Phase 8 added remediation_repo; a module missing from
    # this list silently reaches the real MongoDB and the test fails with a
    # connection timeout rather than an assertion.
    for module_name in ("auth", "video_repo", "db_init", "remediation_repo"):
        if module_name in sys.modules:
            monkeypatch.setattr(
                sys.modules[module_name], "get_database", _get_database, raising=False
            )

    return database
