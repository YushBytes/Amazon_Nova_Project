"""Phase 3.5: account uniqueness and video-ownership integrity.

References: `docs/SECURITY_IMPLEMENTATION_PLAN.md` items 33 (no
`users.username` unique index) and 34 (pre-Phase-3 uploads without ownership).

The delicate requirement is that creating a unique index on a collection that
already holds duplicates must **fail safely**: no account may be deleted,
merged, or renamed to make the migration succeed, and the diagnostic must not
leak the conflicting usernames.

All accounts and ids here are synthetic.
"""

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import app as app_module
import auth
import db_init
import video_repo

GOOD_PASSWORD = "a-sufficiently-long-password"


@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


def _alice():
    return {"Authorization": f"Bearer {auth.create_access_token('alice', 'client')}"}


# ---------------------------------------------------------------------------
# duplicate detection
# ---------------------------------------------------------------------------

async def test_no_duplicates_reported_for_a_clean_collection(fake_db):
    await fake_db["users"].insert_one({"username": "a1", "role": "client"})
    await fake_db["users"].insert_one({"username": "a2", "role": "client"})

    counts = await db_init.find_duplicate_username_counts(fake_db)
    assert counts == {"groups": 0, "extra_documents": 0}


async def test_duplicates_are_counted_accurately(fake_db):
    users = fake_db["users"]
    # Seed pre-existing bad data, bypassing index checks.
    users.insert_raw({"username": "dupe", "role": "client"})
    users.insert_raw({"username": "dupe", "role": "client"})
    users.insert_raw({"username": "dupe", "role": "agent"})
    users.insert_raw({"username": "other", "role": "client"})
    users.insert_raw({"username": "other", "role": "client"})
    users.insert_raw({"username": "unique", "role": "client"})

    counts = await db_init.find_duplicate_username_counts(fake_db)
    assert counts["groups"] == 2            # "dupe" and "other"
    assert counts["extra_documents"] == 3   # 2 extra for dupe, 1 for other


async def test_duplicate_report_never_exposes_usernames(fake_db, caplog):
    """The diagnostic must be safe to paste into a log or an issue."""
    users = fake_db["users"]
    users.insert_raw({"username": "sensitive-person", "role": "client"})
    users.insert_raw({"username": "sensitive-person", "role": "client"})

    with caplog.at_level("DEBUG"):
        counts = await db_init.find_duplicate_username_counts(fake_db)
        created = await db_init.ensure_user_indexes(fake_db, strict=False)

    assert counts["groups"] == 1
    assert created is False
    assert "sensitive-person" not in caplog.text
    assert "sensitive-person" not in str(counts)


# ---------------------------------------------------------------------------
# index creation is safe and repeatable
# ---------------------------------------------------------------------------

async def test_unique_index_created_on_a_clean_collection(fake_db):
    await fake_db["users"].insert_one({"username": "solo", "role": "client"})

    assert await db_init.ensure_user_indexes(fake_db) is True
    assert ("username", True) in fake_db["users"].indexes


async def test_index_creation_is_idempotent(fake_db):
    await fake_db["users"].insert_one({"username": "solo", "role": "client"})

    assert await db_init.ensure_user_indexes(fake_db) is True
    assert await db_init.ensure_user_indexes(fake_db) is True
    # Declared once, not duplicated.
    assert fake_db["users"].indexes.count(("username", True)) == 1


async def test_index_creation_is_skipped_when_duplicates_exist(fake_db):
    users = fake_db["users"]
    users.insert_raw({"username": "dupe", "role": "client"})
    users.insert_raw({"username": "dupe", "role": "client"})

    assert await db_init.ensure_user_indexes(users and fake_db) is False
    assert ("username", True) not in users.indexes


async def test_strict_mode_raises_for_duplicates(fake_db):
    users = fake_db["users"]
    users.insert_raw({"username": "dupe", "role": "client"})
    users.insert_raw({"username": "dupe", "role": "client"})

    with pytest.raises(db_init.IndexCreationBlocked) as excinfo:
        await db_init.ensure_user_indexes(fake_db, strict=True)

    message = str(excinfo.value)
    assert "1 username(s)" in message
    assert "dupe" not in message, "the blocked-index error leaked a username"


async def test_no_account_is_modified_or_removed_by_a_blocked_migration(fake_db):
    users = fake_db["users"]
    users.insert_raw({"username": "dupe", "role": "client"})
    users.insert_raw({"username": "dupe", "role": "agent"})
    users.insert_raw({"username": "keeper", "role": "client"})
    before = [dict(d) for d in users.docs]

    await db_init.ensure_user_indexes(fake_db, strict=False)

    assert users.docs == before, "a blocked migration altered account data"


async def test_initialize_database_is_safe_with_duplicates(fake_db):
    """Startup must not crash, and must still set up what it can."""
    users = fake_db["users"]
    users.insert_raw({"username": "dupe", "role": "client"})
    users.insert_raw({"username": "dupe", "role": "client"})

    results = await db_init.initialize_database(strict=False)

    assert results["user_index"] is False
    assert results["video_indexes"] is True
    assert ("video_id", True) in fake_db["videos"].indexes


async def test_initialize_database_on_a_clean_database(fake_db):
    results = await db_init.initialize_database(strict=False)
    assert results == {"video_indexes": True, "user_index": True}


# ---------------------------------------------------------------------------
# signup and the uniqueness race
# ---------------------------------------------------------------------------

def test_duplicate_signup_is_rejected_cleanly(client, fake_db):
    first = client.post(
        "/auth/signup", json={"username": "taken", "password": GOOD_PASSWORD}
    )
    assert first.status_code == 200

    second = client.post(
        "/auth/signup", json={"username": "taken", "password": GOOD_PASSWORD}
    )
    assert second.status_code == 400
    assert "already exists" in second.json()["detail"].lower()


async def test_signup_survives_a_duplicate_key_race(fake_db, monkeypatch):
    """The find_one check is not atomic; the index is the real guarantee.

    Simulates the losing side of a race: the existence check passes, then the
    insert is rejected by the unique index. That must become a clean 400, not
    an unhandled 500.
    """
    from fastapi import HTTPException

    from auth import SignupRequest, signup

    async def always_duplicate(_document):
        raise DuplicateKeyError("E11000 duplicate key error: username")

    monkeypatch.setattr(fake_db["users"], "insert_one", always_duplicate)

    with pytest.raises(HTTPException) as excinfo:
        await signup(SignupRequest(username="racer", password=GOOD_PASSWORD))

    assert excinfo.value.status_code == 400
    assert "already exists" in excinfo.value.detail.lower()


async def test_duplicate_key_race_does_not_log_credentials(fake_db, monkeypatch, caplog):
    from fastapi import HTTPException

    from auth import SignupRequest, signup

    async def always_duplicate(_document):
        raise DuplicateKeyError("E11000 duplicate key error: username")

    monkeypatch.setattr(fake_db["users"], "insert_one", always_duplicate)

    with caplog.at_level("DEBUG"):
        with pytest.raises(HTTPException):
            await signup(SignupRequest(username="racer2", password=GOOD_PASSWORD))

    assert GOOD_PASSWORD not in caplog.text
    assert "$argon2" not in caplog.text


@pytest.mark.parametrize("username", [" alice", "alice ", " alice ", "\talice"])
def test_new_usernames_with_surrounding_whitespace_are_rejected(
    client, fake_db, username
):
    """Prevents creating " alice" alongside "alice".

    Existing records are matched exactly and never rewritten, so this guards
    the future without touching stored data.
    """
    response = client.post(
        "/auth/signup", json={"username": username, "password": GOOD_PASSWORD}
    )
    assert response.status_code == 422


def test_ordinary_usernames_are_unaffected(client, fake_db):
    response = client.post(
        "/auth/signup", json={"username": "normal.user-1", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200


async def test_existing_untrimmed_username_can_still_log_in(fake_db):
    """A stored " legacy" account must remain reachable.

    This is why usernames are not normalised on read: trimming would make this
    account impossible to authenticate.
    """
    from auth import LoginRequest, login

    fake_db["users"].insert_raw(
        {
            "username": " legacy",
            "password": auth.hash_password(GOOD_PASSWORD),
            "role": "client",
        }
    )

    result = await login(LoginRequest(username=" legacy", password=GOOD_PASSWORD))
    assert result.access_token


# ---------------------------------------------------------------------------
# video ownership integrity
# ---------------------------------------------------------------------------

@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", target.resolve())
    return target.resolve()


def _upload(client, headers, filename="clip.mp4"):
    return client.post(
        "/upload-reel",
        files={"file": (filename, b"fake-video-bytes", "video/mp4")},
        headers=headers,
    )


def test_upload_persists_the_authenticated_owner(client, fake_db, upload_dir):
    import anyio

    body = _upload(client, _alice()).json()
    record = anyio.run(video_repo.get_video, body["video_id"])

    assert record["owner"] == "alice"
    # The owner came from the token, so it cannot be influenced by the request.
    assert record["video_id"] == body["video_id"]


def test_ownership_cannot_be_claimed_by_supplying_an_id(client, fake_db, upload_dir):
    """A second user must not be able to register themselves as the owner.

    There is no route that writes an ownership record for an arbitrary id, and
    uploading always mints a fresh server-generated id. This asserts both: the
    returned id differs, and the original record is untouched.
    """
    import anyio

    victim = _upload(client, _alice()).json()

    mallory = {
        "Authorization": f"Bearer {auth.create_access_token('mallory', 'client')}"
    }
    attacker = _upload(client, mallory).json()

    assert attacker["video_id"] != victim["video_id"]

    original = anyio.run(video_repo.get_video, victim["video_id"])
    assert original["owner"] == "alice", "ownership was reassigned"

    # And Mallory still cannot read it.
    assert (
        client.get(f"/violations/{victim['video_id']}", headers=mallory).status_code
        == 404
    )


def test_duplicate_video_id_cannot_create_a_second_owner(fake_db):
    """The unique index is the backstop against two owners for one id."""
    import anyio

    async def scenario():
        await video_repo.ensure_indexes()
        await video_repo.record_video(
            video_id="vid_abcd1234",
            owner="alice",
            stored_filename="vid_abcd1234.mp4",
            original_filename="a.mp4",
            extension="mp4",
            size_bytes=10,
        )
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
        return record["owner"]

    assert anyio.run(scenario) == "alice"


def test_failed_ownership_write_leaves_no_accessible_upload(
    client, fake_db, upload_dir, monkeypatch
):
    """An orphaned file would be unattributable and must not survive."""

    async def boom(**_kwargs):
        raise RuntimeError("mongo unavailable")

    monkeypatch.setattr(app_module.video_repo, "record_video", boom)

    response = _upload(client, _alice())
    assert response.status_code == 500
    assert not list(upload_dir.iterdir()), "an orphaned upload was left on disk"


def test_missing_ownership_metadata_denies_access(client, fake_db, upload_dir):
    """A pre-Phase-3 style video with no record must be denied, not allowed."""
    # A file exists on disk but no ownership record was ever written.
    (upload_dir / "vid_99999999.mp4").write_bytes(b"orphan")

    for path in ("/violations/vid_99999999", "/analyze/vid_99999999"):
        method = client.get if path.startswith("/violations") else client.post
        assert method(path, headers=_alice()).status_code == 404

    token = auth.create_media_token("alice", "vid_99999999")
    assert (
        client.get(f"/uploads/vid_99999999.mp4?access_token={token}").status_code
        == 404
    )


def test_ownership_is_enforced_on_every_video_route(client, fake_db, upload_dir):
    body = _upload(client, _alice()).json()
    mallory = {
        "Authorization": f"Bearer {auth.create_access_token('mallory', 'client')}"
    }

    assert client.get(f"/violations/{body['video_id']}", headers=mallory).status_code == 404
    assert client.post(f"/analyze/{body['video_id']}", headers=mallory).status_code == 404

    mallory_media = auth.create_media_token("mallory", body["video_id"])
    assert (
        client.get(f"/uploads/{body['filename']}?access_token={mallory_media}").status_code
        == 404
    )


def test_video_indexes_are_created_repeatably(fake_db):
    import anyio

    async def scenario():
        await video_repo.ensure_indexes()
        await video_repo.ensure_indexes()
        return fake_db["videos"].indexes

    indexes = anyio.run(scenario)
    assert indexes.count(("video_id", True)) == 1
    assert ("owner", False) in indexes
