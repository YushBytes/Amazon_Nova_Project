"""OPTIONAL integration tests against a real MongoDB server.

Phase 3.5. The rest of the suite uses an in-memory substitute, which exercises
application logic but not real driver behaviour, real unique-index enforcement,
or real duplicate-key errors. These tests close that gap when a disposable
database is available.

HOW TO RUN

    set MONGODB_TEST_URL=mongodb://localhost:27017/novaflow_pytest
    pytest -m requires_mongodb

Every test here SKIPS unless `MONGODB_TEST_URL` is set and the server is
reachable. Nothing in the default `pytest` run touches a database.

SAFETY RULES enforced by `_assert_safe_test_database` below. The database name
must positively identify itself as a test database (it has to contain "test"),
must not be a well-known real name, and the URL must point at a loopback or
explicitly test-labelled host unless the caller sets
`MONGODB_TEST_ALLOW_REMOTE=true`. Collections are dropped during setup, so a
mistake here would destroy real data; the guard refuses rather than guesses.
Only the collections this module creates are touched.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.requires_mongodb

TEST_URL_VAR = "MONGODB_TEST_URL"
ALLOW_REMOTE_VAR = "MONGODB_TEST_ALLOW_REMOTE"

# Database names that must never be used as a test target, even if someone
# appends "test" to them.
_FORBIDDEN_DB_NAMES = {"novaflow", "admin", "local", "config", "production", "prod"}

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "mongo", "mongodb")

# Collections this module owns. Nothing else is read or written.
_OWNED_COLLECTIONS = ("users", "videos")


class UnsafeTestDatabase(RuntimeError):
    """Raised when the configured database cannot be confirmed test-only."""


def _parse_db_name(url: str) -> str:
    """Extract the database name from a MongoDB connection string."""
    without_scheme = url.split("://", 1)[-1]
    path = without_scheme.split("/", 1)[1] if "/" in without_scheme else ""
    return path.split("?", 1)[0]


def _assert_safe_test_database(url: str) -> str:
    """Refuse anything not positively identifiable as a disposable test DB."""
    db_name = _parse_db_name(url)

    if not db_name:
        raise UnsafeTestDatabase(
            f"{TEST_URL_VAR} must include an explicit database name, e.g. "
            "mongodb://localhost:27017/novaflow_pytest"
        )
    if db_name.lower() in _FORBIDDEN_DB_NAMES:
        raise UnsafeTestDatabase(
            f"Refusing to use database {db_name!r}: it is a reserved or "
            "production-looking name."
        )
    if "test" not in db_name.lower():
        raise UnsafeTestDatabase(
            f"Refusing to use database {db_name!r}: the name must contain "
            "'test' so a disposable database cannot be confused with a real "
            "one. Setup drops collections."
        )

    host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    hostname = host.split(":")[0].lower()
    allow_remote = (os.getenv(ALLOW_REMOTE_VAR) or "").strip().lower() == "true"
    if hostname not in _LOCAL_HOSTS and not allow_remote:
        raise UnsafeTestDatabase(
            f"Refusing to use host {hostname!r}: not a loopback address. Set "
            f"{ALLOW_REMOTE_VAR}=true only for a database you know is "
            "disposable."
        )

    return db_name


@pytest.fixture(scope="module")
def mongo_url() -> str:
    url = os.getenv(TEST_URL_VAR)
    if not url:
        pytest.skip(
            f"{TEST_URL_VAR} is not set; real-MongoDB integration tests skipped"
        )
    # A misconfiguration must fail loudly, never be skipped past.
    _assert_safe_test_database(url)
    return url


@pytest.fixture(scope="module")
def motor_db(mongo_url):
    """A verified-disposable database handle, cleaned up afterwards."""
    motor = pytest.importorskip("motor.motor_asyncio")
    import anyio

    db_name = _parse_db_name(mongo_url)

    async def connect():
        client = motor.AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=2000)
        try:
            await client.admin.command("ping")
        except Exception as exc:  # pragma: no cover - environment dependent
            client.close()
            pytest.skip(f"MongoDB at {TEST_URL_VAR} is not reachable: {type(exc).__name__}")
        return client

    client = anyio.run(connect)
    database = client[db_name]

    async def clean():
        # Only the collections this module owns.
        for name in _OWNED_COLLECTIONS:
            await database[name].drop()

    anyio.run(clean)
    yield database
    anyio.run(clean)
    client.close()


@pytest.fixture
def patched_db(motor_db, monkeypatch):
    """Point the application's get_database at the real test database."""
    import sys

    import db as db_module

    async def _get_database():
        return motor_db

    monkeypatch.setattr(db_module, "get_database", _get_database)
    for module_name in ("auth", "video_repo", "db_init"):
        if module_name in sys.modules:
            monkeypatch.setattr(
                sys.modules[module_name], "get_database", _get_database, raising=False
            )
    return motor_db


# ---------------------------------------------------------------------------
# the safety guard itself (runs without a database)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "mongodb://localhost:27017/novaflow",          # real-looking name
        "mongodb://localhost:27017/admin",             # reserved
        "mongodb://localhost:27017/",                  # no database name
        "mongodb://localhost:27017",                   # no database name
        "mongodb://localhost:27017/production_db",     # no "test" marker
        "mongodb://db.example.com:27017/novaflow_test",  # remote without opt-in
    ],
)
def test_guard_refuses_unsafe_targets(url, monkeypatch):
    """The guard must refuse rather than guess; setup drops collections."""
    monkeypatch.delenv(ALLOW_REMOTE_VAR, raising=False)
    with pytest.raises(UnsafeTestDatabase):
        _assert_safe_test_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "mongodb://localhost:27017/novaflow_test",
        "mongodb://127.0.0.1:27017/pytest_novaflow",
        "mongodb://mongo:27017/novaflow_test?retryWrites=false",
    ],
)
def test_guard_accepts_clearly_disposable_targets(url, monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_VAR, raising=False)
    assert "test" in _assert_safe_test_database(url).lower()


def test_guard_allows_remote_only_with_explicit_opt_in(monkeypatch):
    url = "mongodb://db.example.com:27017/novaflow_test"
    monkeypatch.delenv(ALLOW_REMOTE_VAR, raising=False)
    with pytest.raises(UnsafeTestDatabase):
        _assert_safe_test_database(url)

    monkeypatch.setenv(ALLOW_REMOTE_VAR, "true")
    assert _assert_safe_test_database(url) == "novaflow_test"


# ---------------------------------------------------------------------------
# unique index behaviour
# ---------------------------------------------------------------------------

async def test_username_unique_index_is_enforced_by_the_server(patched_db):
    """The real index, not the fake's emulation of it."""
    from pymongo.errors import DuplicateKeyError

    import db_init

    await patched_db["users"].insert_one({"username": "solo", "role": "client"})
    assert await db_init.ensure_user_indexes(patched_db) is True

    with pytest.raises(DuplicateKeyError):
        await patched_db["users"].insert_one({"username": "solo", "role": "client"})


async def test_index_creation_is_idempotent_against_a_real_server(patched_db):
    import db_init

    assert await db_init.ensure_user_indexes(patched_db) is True
    assert await db_init.ensure_user_indexes(patched_db) is True


async def test_real_duplicates_block_index_creation_without_data_loss(patched_db):
    """The important migration-safety property, verified for real."""
    import db_init

    users = patched_db["users"]
    await users.insert_one({"username": "dupe", "role": "client"})
    await users.insert_one({"username": "dupe", "role": "agent"})
    before = await users.count_documents({})

    created = await db_init.ensure_user_indexes(patched_db, strict=False)
    assert created is False

    with pytest.raises(db_init.IndexCreationBlocked) as excinfo:
        await db_init.ensure_user_indexes(patched_db, strict=True)
    assert "dupe" not in str(excinfo.value)

    # Nothing was deleted or merged to make the migration succeed.
    assert await users.count_documents({}) == before


async def test_duplicate_detection_uses_real_aggregation(patched_db):
    import db_init

    users = patched_db["users"]
    await users.insert_many(
        [
            {"username": "a", "role": "client"},
            {"username": "a", "role": "client"},
            {"username": "a", "role": "client"},
            {"username": "b", "role": "client"},
            {"username": "b", "role": "client"},
            {"username": "c", "role": "client"},
        ]
    )

    counts = await db_init.find_duplicate_username_counts(patched_db)
    assert counts == {"groups": 2, "extra_documents": 3}


async def test_video_indexes_against_a_real_server(patched_db):
    from pymongo.errors import DuplicateKeyError

    import video_repo

    await video_repo.ensure_indexes()
    await video_repo.record_video(
        video_id="vid_abcd1234",
        owner="alice",
        stored_filename="vid_abcd1234.mp4",
        original_filename="a.mp4",
        extension="mp4",
        size_bytes=10,
    )

    # One id cannot acquire a second owner.
    with pytest.raises(DuplicateKeyError):
        await video_repo.record_video(
            video_id="vid_abcd1234",
            owner="mallory",
            stored_filename="vid_abcd1234.mp4",
            original_filename="b.mp4",
            extension="mp4",
            size_bytes=10,
        )

    record = await video_repo.get_video("vid_abcd1234")
    assert record["owner"] == "alice"


# ---------------------------------------------------------------------------
# signup and login against a real server
# ---------------------------------------------------------------------------

async def test_duplicate_signup_against_a_real_index(patched_db):
    """With the real unique index in place, the race path returns 400."""
    from fastapi import HTTPException

    import db_init
    from auth import SignupRequest, signup

    await db_init.ensure_user_indexes(patched_db)

    unique = f"user_{uuid.uuid4().hex[:8]}"
    first = await signup(SignupRequest(username=unique, password="a-good-password"))
    assert first.role == "client"

    with pytest.raises(HTTPException) as excinfo:
        await signup(SignupRequest(username=unique, password="a-good-password"))
    assert excinfo.value.status_code == 400


async def test_legacy_password_migration_against_a_real_server(patched_db):
    """The conditional update is the part the fake cannot really verify."""
    import hashlib

    from auth import LoginRequest, login

    password = "a-good-password"
    legacy = hashlib.sha256(password.encode()).hexdigest()
    username = f"legacy_{uuid.uuid4().hex[:8]}"

    await patched_db["users"].insert_one(
        {"username": username, "password": legacy, "role": "agent"}
    )

    result = await login(LoginRequest(username=username, password=password))
    assert result.role == "agent"

    stored = await patched_db["users"].find_one({"username": username})
    assert stored["password"].startswith("$argon2id$")
    assert stored["password_algo"] == "argon2id"


async def test_failed_login_does_not_migrate_against_a_real_server(patched_db):
    import hashlib

    from fastapi import HTTPException

    from auth import LoginRequest, login

    password = "a-good-password"
    legacy = hashlib.sha256(password.encode()).hexdigest()
    username = f"legacy_{uuid.uuid4().hex[:8]}"

    await patched_db["users"].insert_one(
        {"username": username, "password": legacy, "role": "client"}
    )

    with pytest.raises(HTTPException):
        await login(LoginRequest(username=username, password="wrong-password-x"))

    stored = await patched_db["users"].find_one({"username": username})
    assert stored["password"] == legacy


async def test_repeated_logins_are_idempotent_against_a_real_server(patched_db):
    """The conditional update must tolerate being retried."""
    import hashlib

    from auth import LoginRequest, login

    password = "a-good-password"
    legacy = hashlib.sha256(password.encode()).hexdigest()
    username = f"legacy_{uuid.uuid4().hex[:8]}"

    await patched_db["users"].insert_one(
        {"username": username, "password": legacy, "role": "client"}
    )

    hashes = []
    for _ in range(3):
        assert await login(LoginRequest(username=username, password=password))
        stored = await patched_db["users"].find_one({"username": username})
        hashes.append(stored["password"])

    assert all(h.startswith("$argon2id$") for h in hashes)
    # After the first migration the hash is stable (no needless rewrites).
    assert hashes[1] == hashes[2]


async def test_concurrent_logins_do_not_corrupt_the_record(patched_db):
    """Two simultaneous migrations must leave exactly one valid hash."""
    import asyncio
    import hashlib

    from auth import LoginRequest, check_password, login

    password = "a-good-password"
    legacy = hashlib.sha256(password.encode()).hexdigest()
    username = f"legacy_{uuid.uuid4().hex[:8]}"

    await patched_db["users"].insert_one(
        {"username": username, "password": legacy, "role": "client"}
    )

    results = await asyncio.gather(
        *(login(LoginRequest(username=username, password=password)) for _ in range(5)),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results), results

    stored = await patched_db["users"].find_one({"username": username})
    assert check_password(password, stored["password"]).ok is True

    # Exactly one account record still exists.
    assert await patched_db["users"].count_documents({"username": username}) == 1


# ---------------------------------------------------------------------------
# ownership against a real server
# ---------------------------------------------------------------------------

async def test_ownership_lookup_and_denial_against_a_real_server(patched_db):
    import video_repo

    await video_repo.ensure_indexes()
    await video_repo.record_video(
        video_id="vid_own11111",
        owner="alice",
        stored_filename="vid_own11111.mp4",
        original_filename="a.mp4",
        extension="mp4",
        size_bytes=10,
    )

    assert await video_repo.is_owner("vid_own11111", "alice") is True
    assert await video_repo.is_owner("vid_own11111", "mallory") is False
    # A video with no record must deny, not default to allow.
    assert await video_repo.is_owner("vid_missing0", "alice") is False
    with pytest.raises(video_repo.OwnershipUnknown):
        await video_repo.get_video("vid_missing0")


async def test_owner_listing_is_scoped_against_a_real_server(patched_db):
    import video_repo

    await video_repo.ensure_indexes()
    for index, owner in enumerate(["alice", "alice", "bob"]):
        await video_repo.record_video(
            video_id=f"vid_scope{index:04d}",
            owner=owner,
            stored_filename=f"vid_scope{index:04d}.mp4",
            original_filename="a.mp4",
            extension="mp4",
            size_bytes=10,
        )

    alice = await video_repo.list_videos_for_owner("alice")
    bob = await video_repo.list_videos_for_owner("bob")

    assert {record["owner"] for record in alice} == {"alice"}
    assert len(alice) == 2
    assert len(bob) == 1


async def test_initialize_database_against_a_real_server(patched_db):
    import db_init

    results = await db_init.initialize_database(strict=False)
    assert results == {"video_indexes": True, "user_index": True}

    # Repeatable.
    assert await db_init.initialize_database(strict=False) == {
        "video_indexes": True,
        "user_index": True,
    }


# ---------------------------------------------------------------------------
# Phase 4.1: claims, leases, and recovery against a real server
# ---------------------------------------------------------------------------
#
# These matter more than most integration tests in this file. The fencing
# guarantee rests on `update_one` applying at most one update for a filter
# that includes the expected state and the claim id, and on `$or` over a
# missing field behaving the way the in-memory substitute assumes. Both are
# server behaviours, not application logic, so the fake could agree with the
# application and both could be wrong.

async def _seed_queued(video_id, owner="alice"):
    """Register a video and leave it queued, as upload + submit does."""
    import video_repo
    from job_state import JobState

    await video_repo.record_video(
        video_id=video_id,
        owner=owner,
        stored_filename=f"{video_id}.mp4",
        original_filename="clip.mp4",
        extension="mp4",
        size_bytes=1024,
        s3_bucket="test-bucket",
        s3_key=f"sources/{video_id}/source.mp4",
    )
    await video_repo.transition(video_id, JobState.QUEUED, expected=JobState.UPLOADED)
    return video_id


async def _expire_lease(database, video_id, minutes=10):
    await database["videos"].update_one(
        {"video_id": video_id},
        {"$set": {
            "lease_expires_at": datetime.now(timezone.utc) - timedelta(minutes=minutes),
            "updated_at": datetime.now(timezone.utc) - timedelta(minutes=minutes),
        }},
    )


async def test_the_source_reference_round_trips_through_the_driver(patched_db):
    import source_store
    import video_repo

    await _seed_queued("vid_real0001")
    record = await video_repo.get_video("vid_real0001")

    assert record["s3_bucket"] == "test-bucket"
    assert record["s3_key"] == "sources/vid_real0001/source.mp4"
    assert source_store.has_durable_source(record) is True


async def test_only_one_claim_wins_against_a_real_server(patched_db):
    """The real conditional update, not the fake's emulation of it."""
    import video_repo
    from job_state import InvalidTransition, JobState

    await _seed_queued("vid_real0002")

    _record, claim_id = await video_repo.claim(
        "vid_real0002", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    assert claim_id

    with pytest.raises(InvalidTransition):
        await video_repo.claim(
            "vid_real0002", JobState.EXTRACTING, expected=JobState.QUEUED
        )


async def test_a_fenced_write_is_refused_by_the_real_server(patched_db):
    import video_repo
    from job_state import JobState
    from video_repo import ClaimLost

    await _seed_queued("vid_real0003")
    await video_repo.claim(
        "vid_real0003", JobState.EXTRACTING, expected=JobState.QUEUED
    )

    with pytest.raises(ClaimLost):
        await video_repo.transition(
            "vid_real0003",
            JobState.ANALYZING,
            expected=JobState.EXTRACTING,
            claim_id="a-claim-that-never-held-this-job",
        )

    record = await video_repo.get_video("vid_real0003")
    assert record["status"] == JobState.EXTRACTING.value


async def test_heartbeat_extends_a_real_lease(patched_db):
    import video_repo
    from job_state import JobState

    await _seed_queued("vid_real0004")
    _record, claim_id = await video_repo.claim(
        "vid_real0004", JobState.EXTRACTING, expected=JobState.QUEUED
    )

    # Pull the lease back so the extension is unambiguously observable.
    await _expire_lease(patched_db, "vid_real0004", minutes=5)
    await video_repo.heartbeat("vid_real0004", claim_id)

    after = (await video_repo.get_video("vid_real0004"))["lease_expires_at"]
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    assert after > datetime.now(timezone.utc)


async def test_a_fenced_heartbeat_is_refused_by_the_real_server(patched_db):
    import video_repo
    from job_state import JobState
    from video_repo import ClaimLost

    await _seed_queued("vid_real0005")
    await video_repo.claim(
        "vid_real0005", JobState.EXTRACTING, expected=JobState.QUEUED
    )

    with pytest.raises(ClaimLost):
        await video_repo.heartbeat("vid_real0005", "not-this-workers-claim")


async def test_an_expired_lease_is_found_and_reclaimed(patched_db):
    import video_repo
    from job_state import JobState

    await _seed_queued("vid_real0006")
    await video_repo.claim(
        "vid_real0006", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    await _expire_lease(patched_db, "vid_real0006")

    abandoned = await video_repo.find_expired_leases()
    assert "vid_real0006" in [r["video_id"] for r in abandoned]

    updated = await video_repo.reclaim(
        next(r for r in abandoned if r["video_id"] == "vid_real0006")
    )
    assert updated["status"] == JobState.QUEUED.value
    assert updated["claim_id"] is None
    assert updated["recovered_count"] == 1


async def test_a_live_lease_is_not_reported_abandoned_by_the_real_server(patched_db):
    import video_repo
    from job_state import JobState

    await _seed_queued("vid_real0007")
    await video_repo.claim(
        "vid_real0007", JobState.EXTRACTING, expected=JobState.QUEUED
    )

    abandoned = await video_repo.find_expired_leases()

    assert "vid_real0007" not in [r["video_id"] for r in abandoned]


async def test_a_double_reclaim_is_refused_by_the_real_server(patched_db):
    """Two schedulers reading the same record must not both consume an attempt."""
    import video_repo
    from job_state import JobState

    await _seed_queued("vid_real0008")
    await video_repo.claim(
        "vid_real0008", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    await _expire_lease(patched_db, "vid_real0008")

    snapshot = await video_repo.get_video("vid_real0008")
    first = await video_repo.reclaim(snapshot)
    second = await video_repo.reclaim(snapshot)

    assert first is not None
    assert second is None
    assert (await video_repo.get_video("vid_real0008"))["attempt"] == 2


async def test_a_legacy_record_with_no_lease_fields_is_claimable(patched_db):
    """`{"claim_id": None}` must match a document where the field is absent.

    The lease filter depends on that server behaviour. A pre-4.1 record has no
    claim_id at all, and if the filter failed to match it, the record could
    never be claimed again - permanently stuck, by the very mechanism added to
    rescue stuck jobs.
    """
    import video_repo
    from job_state import JobState

    await patched_db["videos"].insert_one({
        "video_id": "vid_legacy99",
        "owner": "alice",
        "stored_filename": "vid_legacy99.mp4",
        "status": "queued",
        "attempt": 1,
    })

    record, claim_id = await video_repo.claim(
        "vid_legacy99", JobState.EXTRACTING, expected=JobState.QUEUED
    )

    assert record["status"] == JobState.EXTRACTING.value
    assert record["claim_id"] == claim_id


async def test_recovery_sweep_is_idempotent_against_a_real_server(patched_db):
    import pipeline
    import video_repo
    from job_state import JobState

    await _seed_queued("vid_real0009")
    await video_repo.claim(
        "vid_real0009", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    await _expire_lease(patched_db, "vid_real0009")

    first = await pipeline.recover_abandoned_jobs(dispatch=None)
    second = await pipeline.recover_abandoned_jobs(dispatch=None)

    assert first["requeued"] >= 1
    assert second["requeued"] == 0
    assert (await video_repo.get_video("vid_real0009"))["attempt"] == 2


async def test_reclaim_fails_a_job_past_its_attempt_budget(patched_db):
    import video_repo
    from job_state import MAX_ATTEMPTS, JobState

    await _seed_queued("vid_real0010")
    await video_repo.claim(
        "vid_real0010", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    await patched_db["videos"].update_one(
        {"video_id": "vid_real0010"}, {"$set": {"attempt": MAX_ATTEMPTS}}
    )
    await _expire_lease(patched_db, "vid_real0010")

    updated = await video_repo.reclaim(await video_repo.get_video("vid_real0010"))

    assert updated["status"] == JobState.FAILED.value
    assert "interrupted" in updated["error_summary"].lower()


async def test_delete_video_removes_only_the_named_record(patched_db):
    """The compensating cleanup must not take anything else with it."""
    import video_repo

    await _seed_queued("vid_real0011", owner="alice")
    await _seed_queued("vid_real0012", owner="bob")

    assert await video_repo.delete_video("vid_real0011") is True

    with pytest.raises(video_repo.OwnershipUnknown):
        await video_repo.get_video("vid_real0011")
    assert (await video_repo.get_video("vid_real0012"))["owner"] == "bob"


async def test_the_status_index_exists_for_the_recovery_sweep(patched_db):
    """The sweep queries on status; without the index it is a collection scan."""
    import video_repo

    await video_repo.ensure_indexes()
    information = await patched_db["videos"].index_information()

    indexed = {
        tuple(field for field, _direction in spec["key"])
        for spec in information.values()
    }
    assert ("status",) in indexed
    assert ("video_id",) in indexed
