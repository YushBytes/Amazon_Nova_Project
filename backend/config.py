"""Security-relevant configuration for the NovaFlow backend.

Phase 3. Everything here is read from the environment. No secret value is
hardcoded, logged, or written to disk.

Environment variables (names only; never commit values):

    NOVAFLOW_ENV            "development" (default) | "production"
    JWT_SECRET_KEY          HMAC signing key for access tokens.
                            REQUIRED in production. In development, an
                            ephemeral per-process key is generated when unset.
    JWT_ALGORITHM           Optional. Default HS256. Restricted to an allowlist.
    ACCESS_TOKEN_EXPIRE_MINUTES
                            Optional. Default 1440 (24h), preserving the
                            previous session lifetime.
    MAX_UPLOAD_BYTES        Optional. Default 104857600 (100 MiB).
    MAX_VIDEO_DURATION_SECONDS
                            Optional. Longest clip accepted for analysis.
                            Default 60.
    FRAME_SAMPLE_FPS        Optional. Frames extracted per second of video.
                            Default 1.
    MAX_FRAMES_PER_VIDEO    Optional. Hard cap on extracted frames, so a long
                            or high-rate video cannot run unbounded. Default 120.
    FFMPEG_TIMEOUT_SECONDS  Optional. Wall-clock limit for one FFmpeg run.
                            Default 300.
    BEDROCK_MAX_ATTEMPTS    Optional. Bounded retries per frame for retryable
                            Bedrock errors. Default 3.
    MEDIA_TOKEN_TTL_MINUTES Optional. Lifetime of the video playback token
                            carried in the ?access_token query parameter.
                            Default 15. Keep it short: it appears in URLs.
    ALLOWED_VIDEO_EXTENSIONS
                            Optional. Comma-separated. Default mp4,mov,avi,webm,mkv.
    CORS_ALLOWED_ORIGINS    Optional. Comma-separated exact origins.
                            Default http://localhost:3000.
    RATE_LIMIT_ENABLED      Optional. "false" disables the limiter entirely.
                            Default true.
    RATE_LIMIT_LOGIN        Optional. Limit string for POST /auth/login.
                            Default "5/minute;20/hour".
    RATE_LIMIT_SIGNUP       Optional. Limit string for POST /auth/signup.
                            Default "3/minute;10/hour".
    TRUSTED_PROXY_COUNT     Optional. Number of trusted reverse proxies in
                            front of the app. 0 (default) means
                            X-Forwarded-For is IGNORED entirely.

See .env.example for a placeholder template.
"""

from __future__ import annotations

import logging
import os
import secrets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

PRODUCTION = "production"


def _env_name() -> str:
    return (os.getenv("NOVAFLOW_ENV") or "development").strip().lower()


def is_production() -> bool:
    return _env_name() == PRODUCTION


# ---------------------------------------------------------------------------
# JWT signing key
# ---------------------------------------------------------------------------

# Minimum length for an HMAC key supplied by an operator. 32 characters is the
# smallest value that is not trivially brute-forced; longer is better.
MIN_SECRET_LENGTH = 32

# Values that must never be accepted, regardless of environment. The first
# entry is the secret that was previously hardcoded in auth.py and is therefore
# public knowledge for anyone who has seen the repository history.
KNOWN_INSECURE_SECRETS = frozenset(
    {
        "brand-guardian-dev-secret-change-in-prod",
        "change-in-prod",
        "changeme",
        "secret",
        "supersecret",
        "test",
        "testing",
        "dev",
        "development",
        "password",
        "jwt-secret",
        "your-secret-key",
    }
)

SUPPORTED_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


class ConfigurationError(RuntimeError):
    """Raised when security configuration is missing, malformed, or insecure.

    The message never contains the offending value.
    """


def _validate_secret(secret: str) -> str:
    """Reject empty, short, or known-insecure signing keys.

    Error messages deliberately describe the problem without echoing the value.
    """
    stripped = secret.strip()
    if not stripped:
        raise ConfigurationError("JWT_SECRET_KEY is set but empty.")
    if stripped.lower() in KNOWN_INSECURE_SECRETS:
        raise ConfigurationError(
            "JWT_SECRET_KEY is a known-insecure placeholder value and was "
            "rejected. Generate a fresh random key and set it in the "
            "environment."
        )
    if len(stripped) < MIN_SECRET_LENGTH:
        raise ConfigurationError(
            f"JWT_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters; "
            f"the supplied value is {len(stripped)}."
        )
    return stripped


def load_jwt_secret() -> str:
    """Return the JWT signing key for this process.

    Production: JWT_SECRET_KEY is mandatory and validated. A missing or
    insecure value raises ConfigurationError rather than falling back to
    anything guessable.

    Development: a missing value yields a random ephemeral key generated in
    memory for this process only. It is never written to disk or logged, and
    tokens stop validating when the process restarts, which is the intended
    signal to configure a real key. A value that *is* supplied is still
    validated, so the old hardcoded placeholder cannot be reintroduced.
    """
    raw = os.getenv("JWT_SECRET_KEY")

    if raw is not None:
        return _validate_secret(raw)

    if is_production():
        raise ConfigurationError(
            "JWT_SECRET_KEY is required when NOVAFLOW_ENV=production. "
            "Refusing to start without an operator-supplied signing key."
        )

    logger.warning(
        "JWT_SECRET_KEY is not set; generated an ephemeral development key. "
        "Sessions will not survive a restart. Set JWT_SECRET_KEY for stable "
        "local sessions, and always set it outside development."
    )
    return secrets.token_urlsafe(48)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}.")
    return value


def load_algorithm() -> str:
    algorithm = (os.getenv("JWT_ALGORITHM") or "HS256").strip().upper()
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ConfigurationError(
            f"JWT_ALGORITHM must be one of {sorted(SUPPORTED_ALGORITHMS)}."
        )
    return algorithm


def load_media_token_ttl_minutes() -> int:
    """Lifetime of a video playback token.

    Deliberately short and separate from the session lifetime: this token
    travels in a URL query string, so it can end up in browser history, a
    Referer header, and any proxy log outside this process. 15 minutes is long
    enough to start playback and short enough to limit the damage if a URL
    leaks.
    """
    return _int_env("MEDIA_TOKEN_TTL_MINUTES", 15, minimum=1)


def load_token_lifetime_minutes() -> int:
    # Default preserves the previous 24-hour session so existing logins behave
    # the same way.
    return _int_env("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

# The complete role model. Authorization decisions may only use these values.
ROLE_CLIENT = "client"
ROLE_AGENT = "agent"
VALID_ROLES = frozenset({ROLE_CLIENT, ROLE_AGENT})

# Role assigned to every account created through public registration. "agent"
# is privileged (it accepts projects and uploads reels) and is therefore never
# self-assignable; see docs/SECURITY_AUDIT.md for the operator procedure.
DEFAULT_SIGNUP_ROLE = ROLE_CLIENT


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def load_max_upload_bytes() -> int:
    # 100 MiB. The UI advertises a 60 MB limit and the extraction pipeline
    # rejects clips longer than 60s, so this is a generous server-side ceiling
    # that still bounds disk use.
    return _int_env("MAX_UPLOAD_BYTES", 100 * 1024 * 1024, minimum=1024)


def load_max_video_duration_seconds() -> int:
    """Longest clip accepted for analysis.

    Bounds both FFmpeg work and Bedrock spend: at FRAME_SAMPLE_FPS frames per
    second, duration directly determines how many model calls a video costs.
    """
    return _int_env("MAX_VIDEO_DURATION_SECONDS", 60, minimum=1)


def load_frame_sample_fps() -> int:
    """Frames extracted per second of source video.

    1 fps is the pre-existing behaviour and is retained as the default so
    results stay comparable with earlier runs.
    """
    return _int_env("FRAME_SAMPLE_FPS", 1, minimum=1)


def load_max_frames_per_video() -> int:
    """Hard cap on extracted frames.

    Duration and fps already bound this, but a malformed container can report
    a short duration and still yield far more frames. The cap is the backstop
    that keeps one upload from costing an unbounded number of model calls.
    """
    return _int_env("MAX_FRAMES_PER_VIDEO", 120, minimum=1)


def load_ffmpeg_timeout_seconds() -> int:
    """Wall-clock limit for one FFmpeg invocation."""
    return _int_env("FFMPEG_TIMEOUT_SECONDS", 300, minimum=5)


def load_bedrock_max_attempts() -> int:
    """Bounded retries per frame for retryable Bedrock failures."""
    return _int_env("BEDROCK_MAX_ATTEMPTS", 3, minimum=1)


def load_allowed_video_extensions() -> frozenset[str]:
    raw = os.getenv("ALLOWED_VIDEO_EXTENSIONS")
    if raw is None or not raw.strip():
        return frozenset({"mp4", "mov", "avi", "webm", "mkv"})
    parsed = {
        part.strip().lower().lstrip(".")
        for part in raw.split(",")
        if part.strip()
    }
    if not parsed:
        raise ConfigurationError("ALLOWED_VIDEO_EXTENSIONS is set but empty.")
    if not all(part.isalnum() for part in parsed):
        raise ConfigurationError(
            "ALLOWED_VIDEO_EXTENSIONS entries must be alphanumeric."
        )
    return frozenset(parsed)


# Content types the frontend's <input accept="video/*"> can plausibly send.
# Treated as a secondary signal only; the extension allowlist is authoritative
# and neither proves the bytes are a safe video.
ALLOWED_UPLOAD_CONTENT_TYPE_PREFIXES = ("video/",)
# Some browsers send this for .mkv/.avi files.
ALLOWED_UPLOAD_CONTENT_TYPES = frozenset(
    {"application/octet-stream", "application/x-matroska"}
)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

# Defaults are deliberately strict for credential endpoints and far below what
# a human types by hand. Login is the credential-stuffing target, so it gets a
# small per-minute budget plus an hourly ceiling that a slow attacker cannot
# walk around. Signup is lower still: a legitimate visitor registers once.
DEFAULT_RATE_LIMIT_LOGIN = "5/minute;20/hour"
DEFAULT_RATE_LIMIT_SIGNUP = "3/minute;10/hour"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value.")


def load_rate_limit_enabled() -> bool:
    return _bool_env("RATE_LIMIT_ENABLED", True)


def _validate_limit_string(name: str, raw: str) -> str:
    """Check a limit string parses before the app starts serving.

    Format is one or more `<count>/<period>` rules separated by ";", e.g.
    "5/minute;20/hour". Validation happens here so a typo fails at startup
    rather than at the first login attempt.
    """
    from limits import parse

    cleaned = raw.strip()
    if not cleaned:
        raise ConfigurationError(f"{name} is set but empty.")
    for part in cleaned.split(";"):
        part = part.strip()
        if not part:
            raise ConfigurationError(f"{name} contains an empty rule.")
        try:
            parse(part)
        except Exception as exc:
            raise ConfigurationError(
                f"{name} contains an unparseable rule. Expected forms like "
                f"'5/minute' or '20/hour'."
            ) from exc
    return cleaned


def load_rate_limit_login() -> str:
    return _validate_limit_string(
        "RATE_LIMIT_LOGIN", os.getenv("RATE_LIMIT_LOGIN") or DEFAULT_RATE_LIMIT_LOGIN
    )


def load_rate_limit_signup() -> str:
    return _validate_limit_string(
        "RATE_LIMIT_SIGNUP", os.getenv("RATE_LIMIT_SIGNUP") or DEFAULT_RATE_LIMIT_SIGNUP
    )


def load_trusted_proxy_count() -> int:
    """How many reverse proxies sit in front of this app.

    0 means X-Forwarded-For is ignored completely and the peer address is used.
    That is the safe default: a client can put anything in X-Forwarded-For, so
    honouring it without a known proxy depth lets an attacker mint a fresh
    rate-limit bucket per request.
    """
    return _int_env("TRUSTED_PROXY_COUNT", 0, minimum=0)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

def load_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOWED_ORIGINS")
    if raw is None or not raw.strip():
        return ["http://localhost:3000"]
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    if "*" in origins:
        # A wildcard cannot be combined with credentialed requests and would
        # let any site call the API with a user's token.
        raise ConfigurationError(
            "CORS_ALLOWED_ORIGINS must list exact origins; '*' is not allowed."
        )
    return origins


# ---------------------------------------------------------------------------
# Resolved settings
# ---------------------------------------------------------------------------

class Settings:
    """Resolved configuration, validated at construction.

    Instantiating this in production with absent or insecure security
    configuration raises ConfigurationError, so the application fails to start
    rather than running in an insecure state.
    """

    def __init__(self) -> None:
        self.environment = _env_name()
        self.is_production = is_production()
        self.jwt_secret = load_jwt_secret()
        self.jwt_algorithm = load_algorithm()
        self.access_token_expire_minutes = load_token_lifetime_minutes()
        self.media_token_ttl_minutes = load_media_token_ttl_minutes()
        self.max_upload_bytes = load_max_upload_bytes()
        self.allowed_video_extensions = load_allowed_video_extensions()
        self.max_video_duration_seconds = load_max_video_duration_seconds()
        self.frame_sample_fps = load_frame_sample_fps()
        self.max_frames_per_video = load_max_frames_per_video()
        self.ffmpeg_timeout_seconds = load_ffmpeg_timeout_seconds()
        self.bedrock_max_attempts = load_bedrock_max_attempts()
        self.cors_origins = load_cors_origins()
        self.rate_limit_enabled = load_rate_limit_enabled()
        self.rate_limit_login = load_rate_limit_login()
        self.rate_limit_signup = load_rate_limit_signup()
        self.trusted_proxy_count = load_trusted_proxy_count()
        # API docs expose the full route surface; keep them off in production
        # unless an operator explicitly opts in.
        self.expose_docs = not self.is_production or (
            (os.getenv("EXPOSE_API_DOCS") or "").strip().lower() == "true"
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        # Never include the signing key in a repr; these objects can end up in
        # tracebacks and log lines.
        return (
            f"Settings(environment={self.environment!r}, "
            f"jwt_algorithm={self.jwt_algorithm!r}, "
            f"access_token_expire_minutes={self.access_token_expire_minutes}, "
            f"media_token_ttl_minutes={self.media_token_ttl_minutes}, "
            f"max_upload_bytes={self.max_upload_bytes}, "
            f"allowed_video_extensions={sorted(self.allowed_video_extensions)}, "
            f"max_video_duration_seconds={self.max_video_duration_seconds}, "
            f"frame_sample_fps={self.frame_sample_fps}, "
            f"max_frames_per_video={self.max_frames_per_video}, "
            f"ffmpeg_timeout_seconds={self.ffmpeg_timeout_seconds}, "
            f"bedrock_max_attempts={self.bedrock_max_attempts}, "
            f"cors_origins={self.cors_origins}, "
            f"rate_limit_enabled={self.rate_limit_enabled}, "
            f"rate_limit_login={self.rate_limit_login!r}, "
            f"rate_limit_signup={self.rate_limit_signup!r}, "
            f"trusted_proxy_count={self.trusted_proxy_count}, "
            f"expose_docs={self.expose_docs}, jwt_secret=<redacted>)"
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, constructing them on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Drop the cached settings. Intended for tests that vary the environment."""
    global _settings
    _settings = None
