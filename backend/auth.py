"""Authentication, password storage, and authorization dependencies.

Phase 3 security hardening. Changes from the previous implementation:

  * The HMAC signing key is read from the environment (config.py) instead of
    being hardcoded. Production refuses to start without one.
  * Passwords are stored with Argon2id (argon2-cffi) instead of unsalted
    SHA-256. Legacy SHA-256 records are migrated transparently on the next
    successful login.
  * Public registration always assigns the least-privileged role server-side;
    a client-supplied role is ignored.
  * Token decoding restricts the algorithm, requires an expiry, and validates
    the subject and role claims against the known role model.
  * The auth routes are an APIRouter included under /auth rather than a mounted
    sub-application. External URLs are unchanged, but the routes now appear in
    the parent OpenAPI schema, share one CORS policy, and no longer expose a
    second /auth/docs surface.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError

from config import DEFAULT_SIGNUP_ROLE, VALID_ROLES, get_settings
from db import get_database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

# argon2-cffi defaults are Argon2id with time_cost=3, memory_cost=64 MiB,
# parallelism=4, which is a reasonable interactive-login profile. Parameters
# live in one place so they can be raised later without touching call sites.
_password_hasher = PasswordHasher()

# A legacy record is exactly 64 lowercase hex characters: the hexdigest of the
# previous unsalted SHA-256 scheme. Anchored so a malformed or attacker-chosen
# value cannot be mistaken for a legacy hash.
_LEGACY_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Argon2 encoded hashes are self-describing and start with the variant.
_ARGON2_PREFIX = "$argon2"


def hash_password(password: str) -> str:
    """Return an Argon2id encoded hash (includes salt and parameters)."""
    return _password_hasher.hash(password)


def _legacy_sha256_hexdigest(password: str) -> str:
    """The previous, insecure scheme. Retained only to verify old records."""
    return hashlib.sha256(password.encode()).hexdigest()


def is_legacy_hash(stored: object) -> bool:
    """True only for a value that is unambiguously an old SHA-256 hexdigest."""
    return isinstance(stored, str) and bool(_LEGACY_SHA256_RE.fullmatch(stored))


def is_argon2_hash(stored: object) -> bool:
    return isinstance(stored, str) and stored.startswith(_ARGON2_PREFIX)


class PasswordCheck:
    """Outcome of verifying a password against a stored hash.

    `needs_rehash` is True when the password was correct but the stored hash
    should be replaced, either because it used the legacy scheme or because the
    Argon2 parameters have since changed.
    """

    __slots__ = ("ok", "needs_rehash", "was_legacy")

    def __init__(self, ok: bool, needs_rehash: bool = False, was_legacy: bool = False):
        self.ok = ok
        self.needs_rehash = needs_rehash
        self.was_legacy = was_legacy

    def __bool__(self) -> bool:
        return self.ok


def check_password(password: str, stored: object) -> PasswordCheck:
    """Verify `password` against `stored`, reporting whether a rehash is due.

    Recognises exactly two formats. Anything else (None, empty, truncated, a
    bcrypt hash, arbitrary text) is rejected outright rather than being
    guessed at, so a malformed record can never authenticate.
    """
    if is_argon2_hash(stored):
        try:
            _password_hasher.verify(stored, password)  # type: ignore[arg-type]
        except VerifyMismatchError:
            return PasswordCheck(False)
        except (InvalidHashError, HashingError):
            # Stored value claims to be Argon2 but cannot be parsed.
            logger.warning("Stored password hash is malformed; rejecting login")
            return PasswordCheck(False)
        # Parameters may have been raised since this hash was written.
        try:
            needs = _password_hasher.check_needs_rehash(stored)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - defensive
            needs = False
        return PasswordCheck(True, needs_rehash=needs)

    if is_legacy_hash(stored):
        # Constant-time comparison of the legacy digests.
        matched = hmac.compare_digest(
            _legacy_sha256_hexdigest(password), stored  # type: ignore[arg-type]
        )
        if not matched:
            return PasswordCheck(False)
        return PasswordCheck(True, needs_rehash=True, was_legacy=True)

    # Unrecognised format: refuse rather than fall through to a weaker check.
    return PasswordCheck(False)


def verify_password(password: str, hashed: object) -> bool:
    """Backwards-compatible boolean wrapper around check_password."""
    return bool(check_password(password, hashed))


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

# Token scopes. "access" is a normal session token. "media" is a short-lived
# token bound to a single video id, used so a browser <video> element (which
# cannot send an Authorization header) can still fetch a protected file.
SCOPE_ACCESS = "access"
SCOPE_MEDIA = "media"


def create_access_token(username: str, role: str) -> str:
    """Mint a session token. The role must come from stored account data."""
    if role not in VALID_ROLES:
        # Prevents an unknown role from ever being embedded in a token.
        raise ValueError("refusing to issue a token for an unknown role")
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims = {
        "sub": username,
        "role": role,
        "scope": SCOPE_ACCESS,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_media_token(username: str, video_id: str) -> str:
    """Mint a short-lived token that grants read access to one video only."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims = {
        "sub": username,
        "scope": SCOPE_MEDIA,
        "vid": video_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.media_token_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# One generic message for every authentication failure, so responses cannot be
# used to distinguish "expired" from "bad signature" from "unknown user".
_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _decode(token: str, expected_scope: str) -> dict:
    """Decode and validate a token, or raise a generic 401.

    The accepted algorithm is pinned to the configured one, so a token whose
    header advertises a different algorithm (including "none") is rejected
    before any signature check.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require_exp": True, "verify_exp": True, "verify_signature": True},
        )
    except JWTError:
        # Includes bad signature, expiry, malformed token, and algorithm
        # mismatch. Logged without the token itself.
        logger.info("Rejected a token that failed validation")
        raise _CREDENTIALS_ERROR

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise _CREDENTIALS_ERROR

    # Tokens minted before scopes existed have no scope claim; treat a missing
    # scope as "access" so existing sessions keep working, but never let a
    # media token satisfy an access requirement or vice versa.
    scope = payload.get("scope", SCOPE_ACCESS)
    if scope != expected_scope:
        raise _CREDENTIALS_ERROR

    if expected_scope == SCOPE_ACCESS:
        role = payload.get("role")
        if not isinstance(role, str) or role not in VALID_ROLES:
            raise _CREDENTIALS_ERROR

    return payload


async def decode_token(token: str) -> dict:
    """Validate a session token and return its claims, or raise 401."""
    return _decode(token, SCOPE_ACCESS)


def decode_media_token(token: str) -> dict:
    """Validate a media token and return its claims, or raise 401."""
    return _decode(token, SCOPE_MEDIA)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

# tokenUrl is the externally reachable path, so the OpenAPI "Authorize" flow
# and any OAuth2 client point at the real login route.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """Resolve the caller from a bearer token.

    Returns the validated claims: {"sub": username, "role": role, ...}.
    """
    return await decode_token(token)


def require_roles(*roles: str) -> Callable:
    """Build a dependency that admits only the given roles.

    Authorization is decided from the token's role claim, which is itself
    derived from stored account data at login. Request bodies are never
    consulted.
    """
    allowed = frozenset(roles)
    unknown = allowed - VALID_ROLES
    if unknown:
        raise ValueError(f"unknown role(s) in require_roles: {sorted(unknown)}")

    async def _dependency(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role for this operation",
            )
        return user

    return _dependency


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    """Credentials for login.

    Deliberately has NO minimum length. Length policy applies to newly chosen
    passwords (see SignupRequest); enforcing it here would lock out existing
    accounts whose password predates the policy. Maximum lengths are kept to
    bound the work done per request.

    A `role` field is accepted and ignored, for compatibility with the existing
    frontend which posts the same body shape to both endpoints. The role used
    for the session always comes from stored account data.
    """

    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    role: str | None = None


class SignupRequest(BaseModel):
    """Credentials for public registration.

    A `role` field is accepted for backwards compatibility with the existing
    frontend but is deliberately IGNORED: the server always assigns
    DEFAULT_SIGNUP_ROLE. Privileged roles are not self-assignable. See
    docs/SECURITY_AUDIT.md for how an operator grants the agent role.
    """

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=1024)
    role: str | None = None

    @field_validator("username")
    @classmethod
    def _no_surrounding_whitespace(cls, value: str) -> str:
        """Reject names that differ from their trimmed form.

        Applied to NEW accounts only. Existing records are matched exactly and
        are never rewritten, because silently trimming a stored username would
        make that account unreachable. Rejecting here stops new
        " alice" / "alice" confusion from being created.
        """
        if value != value.strip():
            raise ValueError("username must not start or end with whitespace")
        return value


# Retained name for existing callers and tests; signup is the stricter schema.
AuthRequest = SignupRequest


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class UserResponse(BaseModel):
    username: str
    role: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse)
async def signup(body: SignupRequest):
    """Create an account with the least-privileged role.

    Public endpoint by design. Any `role` in the request body is ignored.
    """
    database = await get_database()
    users_collection = database["users"]

    existing_user = await users_collection.find_one({"username": body.username})
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already exists")

    if body.role is not None and body.role != DEFAULT_SIGNUP_ROLE:
        # Not an error: the request still succeeds, with the safe role. Logged
        # without the username so the record is useful but not identifying.
        logger.info(
            "Ignored a client-supplied role on signup; assigned the default role"
        )

    assigned_role = DEFAULT_SIGNUP_ROLE

    new_user = {
        "username": body.username,
        "password": hash_password(body.password),
        "role": assigned_role,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        await users_collection.insert_one(new_user)
    except DuplicateKeyError:
        # The find_one check above is not atomic. Once the unique index from
        # db_init exists, a concurrent signup for the same username surfaces
        # here, and it must look identical to the checked case rather than
        # becoming an unhandled 500.
        logger.info("Signup rejected by the unique index (concurrent request)")
        raise HTTPException(status_code=400, detail="Username already exists")

    token = create_access_token(body.username, assigned_role)
    return TokenResponse(access_token=token, role=assigned_role)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """Authenticate and mint a session token.

    Transparently upgrades a legacy SHA-256 record to Argon2id after the
    password has been verified. A failed login never modifies the record.
    """
    database = await get_database()
    users_collection = database["users"]

    user_data = await users_collection.find_one({"username": body.username})

    stored_hash = user_data.get("password") if user_data else None
    result = check_password(body.password, stored_hash)

    if not user_data or not result.ok:
        # Identical response whether the account is unknown or the password is
        # wrong, so the endpoint cannot be used to enumerate usernames.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    role = user_data.get("role")
    if role not in VALID_ROLES:
        # A corrupt or unexpected stored role must not become a token claim.
        logger.error("Account has an unrecognised stored role; refusing login")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if result.needs_rehash:
        await _upgrade_password_hash(
            users_collection, user_data, body.password, was_legacy=result.was_legacy
        )

    token = create_access_token(body.username, role)
    return TokenResponse(access_token=token, role=role)


async def _upgrade_password_hash(
    users_collection, user_data: dict, password: str, was_legacy: bool
) -> None:
    """Replace a verified legacy or outdated hash with a fresh Argon2id hash.

    Guarded by a conditional update on the hash value we just verified, so two
    concurrent logins cannot clobber each other and a record that changed in
    the meantime is left alone. Failures are logged and swallowed: the user is
    already authenticated, and a failed upgrade must not break their login.
    """
    old_hash = user_data.get("password")
    try:
        new_hash = hash_password(password)
        result = await users_collection.update_one(
            {"_id": user_data["_id"], "password": old_hash},
            {
                "$set": {
                    "password": new_hash,
                    "password_algo": "argon2id",
                    "password_migrated_at": datetime.now(timezone.utc),
                }
            },
        )
        if result.modified_count == 1:
            logger.info(
                "Upgraded a stored password hash to Argon2id (legacy=%s)", was_legacy
            )
        else:
            # Another request won the race, or the record moved on. Either way
            # the stored hash is not the one we verified, so leave it.
            logger.info("Password hash upgrade skipped; record already changed")
    except Exception:
        logger.exception("Password hash upgrade failed; login still succeeded")


@router.get("/me", response_model=UserResponse)
async def me(user: dict = Depends(get_current_user)):
    """Return the caller's account, re-read from storage.

    The role is taken from the database rather than the token, so a role change
    takes effect without waiting for the token to expire.
    """
    username = user["sub"]

    database = await get_database()
    users_collection = database["users"]

    user_data = await users_collection.find_one({"username": username})
    if not user_data:
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    role = user_data.get("role")
    if role not in VALID_ROLES:
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    return UserResponse(username=username, role=role)
