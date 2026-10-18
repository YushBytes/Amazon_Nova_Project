"""Phase 3: token validation, password hashing, and legacy migration.

Audit references: docs/SECURITY_AUDIT.md findings S1 (signing key), S2
(unsalted SHA-256 passwords), S6 (token validation).

All accounts here are synthetic. No real password, hash, or credential appears
in this file, and no test prints a stored hash.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt

import auth
import config

TEST_PASSWORD = "correct-horse-battery-staple"
WRONG_PASSWORD = "not-the-right-password"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_new_hashes_use_argon2id():
    stored = auth.hash_password(TEST_PASSWORD)
    assert stored.startswith("$argon2id$"), "passwords must use Argon2id"
    assert auth.is_argon2_hash(stored)


def test_hash_is_salted_so_identical_passwords_differ():
    """The old SHA-256 scheme produced identical hashes for identical inputs,
    which made precomputed-table attacks trivial."""
    first = auth.hash_password(TEST_PASSWORD)
    second = auth.hash_password(TEST_PASSWORD)
    assert first != second


def test_password_is_not_recoverable_from_the_hash():
    stored = auth.hash_password(TEST_PASSWORD)
    assert TEST_PASSWORD not in stored


def test_sha256_of_the_password_is_not_the_stored_value():
    """Guards against a regression back to the unsalted digest."""
    stored = auth.hash_password(TEST_PASSWORD)
    legacy = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    assert stored != legacy


def test_correct_password_verifies():
    stored = auth.hash_password(TEST_PASSWORD)
    result = auth.check_password(TEST_PASSWORD, stored)
    assert result.ok is True
    assert result.was_legacy is False
    assert result.needs_rehash is False


def test_wrong_password_is_rejected():
    stored = auth.hash_password(TEST_PASSWORD)
    assert auth.check_password(WRONG_PASSWORD, stored).ok is False


# ---------------------------------------------------------------------------
# Legacy hash recognition
# ---------------------------------------------------------------------------

def test_legacy_sha256_hash_is_recognised_and_verifies():
    legacy = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    assert auth.is_legacy_hash(legacy)

    result = auth.check_password(TEST_PASSWORD, legacy)
    assert result.ok is True
    assert result.was_legacy is True
    assert result.needs_rehash is True, "a verified legacy hash must be upgraded"


def test_legacy_hash_with_wrong_password_is_rejected():
    legacy = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    result = auth.check_password(WRONG_PASSWORD, legacy)
    assert result.ok is False
    assert result.needs_rehash is False


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "",
        "   ",
        "not-a-hash",
        "abc123",
        "A" * 64,  # uppercase hex: not the legacy format
        "0" * 63,  # too short
        "0" * 65,  # too long
        "$2b$12$abcdefghijklmnopqrstuv",  # a bcrypt hash, not ours
        12345,
        {"$ne": None},  # a NoSQL-injection shaped value
    ],
)
def test_malformed_stored_hashes_never_authenticate(malformed):
    """An unrecognised stored value must be refused, not guessed at.

    This is the control that stops an empty or attacker-influenced password
    field from being treated as a legacy record.
    """
    assert auth.is_legacy_hash(malformed) is False
    assert auth.check_password(TEST_PASSWORD, malformed).ok is False
    assert auth.check_password("", malformed).ok is False


# ---------------------------------------------------------------------------
# Token creation and validation
# ---------------------------------------------------------------------------

async def test_valid_token_is_accepted():
    token = auth.create_access_token("alice", "client")
    claims = await auth.decode_token(token)
    assert claims["sub"] == "alice"
    assert claims["role"] == "client"
    assert claims["scope"] == auth.SCOPE_ACCESS


async def test_token_signed_with_a_different_key_is_rejected():
    settings = config.get_settings()
    forged = jwt.encode(
        {
            "sub": "attacker",
            "role": "agent",
            "scope": auth.SCOPE_ACCESS,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "a-completely-different-signing-key-0123456789",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(HTTPException) as excinfo:
        await auth.decode_token(forged)
    assert excinfo.value.status_code == 401


async def test_expired_token_is_rejected():
    settings = config.get_settings()
    expired = jwt.encode(
        {
            "sub": "alice",
            "role": "client",
            "scope": auth.SCOPE_ACCESS,
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(HTTPException) as excinfo:
        await auth.decode_token(expired)
    assert excinfo.value.status_code == 401


@pytest.mark.parametrize(
    "malformed",
    ["", "abc", "a.b", "a.b.c", "not.a.jwt", "...", "Bearer sometoken"],
)
async def test_malformed_tokens_are_rejected(malformed):
    with pytest.raises(HTTPException) as excinfo:
        await auth.decode_token(malformed)
    assert excinfo.value.status_code == 401


async def test_token_without_exp_is_rejected():
    """A token that never expires cannot be revoked by waiting."""
    settings = config.get_settings()
    no_exp = jwt.encode(
        {"sub": "alice", "role": "client", "scope": auth.SCOPE_ACCESS},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(HTTPException) as excinfo:
        await auth.decode_token(no_exp)
    assert excinfo.value.status_code == 401


@pytest.mark.parametrize("bad_sub", [None, "", "   ", 123, [], {}])
async def test_token_with_missing_or_invalid_subject_is_rejected(bad_sub):
    settings = config.get_settings()
    claims = {
        "role": "client",
        "scope": auth.SCOPE_ACCESS,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if bad_sub is not None:
        claims["sub"] = bad_sub
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(HTTPException):
        await auth.decode_token(token)


@pytest.mark.parametrize(
    "bad_role", [None, "", "admin", "superuser", "root", "AGENT", 1, [], {"a": 1}]
)
async def test_token_with_an_unknown_role_is_rejected(bad_role):
    """A forged or unexpected role claim must not be honoured.

    "admin"/"root" are not in the role model; accepting them could grant
    access that no authorization rule anticipated.
    """
    settings = config.get_settings()
    claims = {
        "sub": "alice",
        "scope": auth.SCOPE_ACCESS,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if bad_role is not None:
        claims["role"] = bad_role
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(HTTPException):
        await auth.decode_token(token)


async def test_unsigned_none_algorithm_token_is_rejected():
    """The classic alg=none bypass must not work.

    python-jose will not produce such a token, so it is assembled by hand.
    """
    import base64
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64(
        json.dumps(
            {
                "sub": "attacker",
                "role": "agent",
                "scope": auth.SCOPE_ACCESS,
                "exp": 9999999999,
            }
        ).encode()
    )
    forged = f"{header}.{body}."

    with pytest.raises(HTTPException) as excinfo:
        await auth.decode_token(forged)
    assert excinfo.value.status_code == 401


async def test_hs512_token_is_rejected_when_hs256_is_configured():
    """Only the configured algorithm may validate."""
    settings = config.get_settings()
    other = jwt.encode(
        {
            "sub": "alice",
            "role": "client",
            "scope": auth.SCOPE_ACCESS,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        settings.jwt_secret,
        algorithm="HS512",
    )
    assert settings.jwt_algorithm == "HS256"
    with pytest.raises(HTTPException):
        await auth.decode_token(other)


def test_cannot_mint_a_token_for_an_unknown_role():
    with pytest.raises(ValueError):
        auth.create_access_token("alice", "admin")


async def test_authentication_errors_are_uniform():
    """Expired, forged, and malformed tokens must look identical to a client,
    so responses cannot be used to probe why a token failed."""
    settings = config.get_settings()
    expired = jwt.encode(
        {
            "sub": "a",
            "role": "client",
            "scope": auth.SCOPE_ACCESS,
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    forged = jwt.encode(
        {
            "sub": "a",
            "role": "client",
            "scope": auth.SCOPE_ACCESS,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "another-key-entirely-0123456789abcdefghij",
        algorithm=settings.jwt_algorithm,
    )

    details = []
    for token in (expired, forged, "garbage"):
        with pytest.raises(HTTPException) as excinfo:
            await auth.decode_token(token)
        details.append((excinfo.value.status_code, excinfo.value.detail))

    assert len(set(details)) == 1, f"responses differ and leak the cause: {details}"


# ---------------------------------------------------------------------------
# Media tokens
# ---------------------------------------------------------------------------

def test_media_token_is_scoped_to_one_video():
    token = auth.create_media_token("alice", "vid_abc12345")
    claims = auth.decode_media_token(token)
    assert claims["vid"] == "vid_abc12345"
    assert claims["scope"] == auth.SCOPE_MEDIA


async def test_media_token_cannot_be_used_as_a_session_token():
    """Scope separation: a media token must not authenticate API routes."""
    token = auth.create_media_token("alice", "vid_abc12345")
    with pytest.raises(HTTPException):
        await auth.decode_token(token)


def test_session_token_cannot_be_used_as_a_media_token():
    token = auth.create_access_token("alice", "client")
    with pytest.raises(HTTPException):
        auth.decode_media_token(token)


# ---------------------------------------------------------------------------
# Login, signup, and transparent migration (in-memory database)
# ---------------------------------------------------------------------------

async def test_signup_then_login_round_trip(fake_db):
    from auth import AuthRequest, login, signup

    created = await signup(AuthRequest(username="newuser", password=TEST_PASSWORD))
    assert created.role == config.DEFAULT_SIGNUP_ROLE

    stored = await fake_db["users"].find_one({"username": "newuser"})
    assert stored["password"].startswith("$argon2id$")

    logged_in = await login(AuthRequest(username="newuser", password=TEST_PASSWORD))
    assert logged_in.role == config.DEFAULT_SIGNUP_ROLE
    assert logged_in.access_token


async def test_login_with_wrong_password_is_rejected(fake_db):
    from auth import AuthRequest, LoginRequest, login, signup

    await signup(AuthRequest(username="user1", password=TEST_PASSWORD))
    with pytest.raises(HTTPException) as excinfo:
        await login(LoginRequest(username="user1", password=WRONG_PASSWORD))
    assert excinfo.value.status_code == 401


async def test_unknown_user_and_wrong_password_look_identical(fake_db):
    """Login must not allow username enumeration."""
    from auth import AuthRequest, login, signup

    await signup(AuthRequest(username="known", password=TEST_PASSWORD))

    with pytest.raises(HTTPException) as wrong_pw:
        await login(AuthRequest(username="known", password=WRONG_PASSWORD))
    with pytest.raises(HTTPException) as no_user:
        await login(AuthRequest(username="nobody", password=TEST_PASSWORD))

    assert wrong_pw.value.status_code == no_user.value.status_code
    assert wrong_pw.value.detail == no_user.value.detail


async def test_legacy_account_is_migrated_on_successful_login(fake_db):
    """A pre-existing SHA-256 record must keep working and be upgraded."""
    from auth import AuthRequest, login

    legacy_hash = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    await fake_db["users"].insert_one(
        {"username": "legacy", "password": legacy_hash, "role": "agent"}
    )

    result = await login(AuthRequest(username="legacy", password=TEST_PASSWORD))
    # The existing privileged role is preserved, not downgraded.
    assert result.role == "agent"

    stored = await fake_db["users"].find_one({"username": "legacy"})
    assert stored["password"].startswith("$argon2id$"), "hash was not upgraded"
    assert stored["password_algo"] == "argon2id"
    assert "password_migrated_at" in stored


async def test_failed_login_leaves_a_legacy_record_untouched(fake_db):
    from auth import AuthRequest, login

    legacy_hash = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    await fake_db["users"].insert_one(
        {"username": "legacy2", "password": legacy_hash, "role": "client"}
    )

    with pytest.raises(HTTPException):
        await login(AuthRequest(username="legacy2", password=WRONG_PASSWORD))

    stored = await fake_db["users"].find_one({"username": "legacy2"})
    assert stored["password"] == legacy_hash, "a failed login must not rewrite the hash"


async def test_repeated_logins_after_migration_still_work(fake_db):
    """Migration must be idempotent from the user's point of view."""
    from auth import AuthRequest, login

    legacy_hash = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    await fake_db["users"].insert_one(
        {"username": "legacy3", "password": legacy_hash, "role": "client"}
    )

    for _ in range(3):
        result = await login(AuthRequest(username="legacy3", password=TEST_PASSWORD))
        assert result.access_token

    stored = await fake_db["users"].find_one({"username": "legacy3"})
    assert stored["password"].startswith("$argon2id$")
    # Still rejects the wrong password after migration.
    with pytest.raises(HTTPException):
        await login(AuthRequest(username="legacy3", password=WRONG_PASSWORD))


async def test_account_with_unknown_stored_role_cannot_log_in(fake_db):
    """A corrupt role must not become a token claim."""
    from auth import AuthRequest, login

    await fake_db["users"].insert_one(
        {
            "username": "weird",
            "password": auth.hash_password(TEST_PASSWORD),
            "role": "superadmin",
        }
    )
    with pytest.raises(HTTPException) as excinfo:
        await login(AuthRequest(username="weird", password=TEST_PASSWORD))
    assert excinfo.value.status_code == 401


async def test_account_with_malformed_stored_hash_cannot_log_in(fake_db):
    """An empty or corrupt stored hash must never authenticate."""
    from auth import LoginRequest, login

    for username, stored in [
        ("broken1", ""),
        ("broken2", "not-a-hash"),
        ("broken3", None),
    ]:
        await fake_db["users"].insert_one(
            {"username": username, "password": stored, "role": "client"}
        )
        with pytest.raises(HTTPException) as excinfo:
            await login(LoginRequest(username=username, password=TEST_PASSWORD))
        assert excinfo.value.status_code == 401


async def test_login_accepts_a_short_legacy_password(fake_db):
    """Length policy applies to new passwords only.

    An account created before the policy may have a short password; enforcing
    the signup minimum on login would lock that user out permanently.
    """
    from auth import LoginRequest, login

    short = "abc"
    legacy_hash = hashlib.sha256(short.encode()).hexdigest()
    await fake_db["users"].insert_one(
        {"username": "shortpw", "password": legacy_hash, "role": "client"}
    )

    result = await login(LoginRequest(username="shortpw", password=short))
    assert result.access_token
    stored = await fake_db["users"].find_one({"username": "shortpw"})
    assert stored["password"].startswith("$argon2id$")


async def test_signup_enforces_the_password_length_policy(fake_db):
    """New accounts must meet the minimum."""
    import pydantic

    from auth import SignupRequest

    with pytest.raises(pydantic.ValidationError):
        SignupRequest(username="newbie", password="short")


# ---------------------------------------------------------------------------
# Logging must not leak credentials
# ---------------------------------------------------------------------------

async def test_login_does_not_log_the_password_or_hash(fake_db, caplog):
    from auth import AuthRequest, login, signup

    with caplog.at_level("DEBUG"):
        await signup(AuthRequest(username="quiet", password=TEST_PASSWORD))
        await login(AuthRequest(username="quiet", password=TEST_PASSWORD))

    text = caplog.text
    assert TEST_PASSWORD not in text
    assert "$argon2id$" not in text


async def test_migration_logging_does_not_include_the_hash(fake_db, caplog):
    from auth import AuthRequest, login

    legacy_hash = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    await fake_db["users"].insert_one(
        {"username": "legacy4", "password": legacy_hash, "role": "client"}
    )

    with caplog.at_level("DEBUG"):
        await login(AuthRequest(username="legacy4", password=TEST_PASSWORD))

    assert legacy_hash not in caplog.text
    assert TEST_PASSWORD not in caplog.text
