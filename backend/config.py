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
    BEDROCK_MODEL_ID        Optional. The Bedrock model or inference profile
                            the analyzer invokes. Default
                            amazon.nova-pro-v1:0. Set this to a regional
                            inference profile (us.amazon.nova-pro-v1:0) where
                            on-demand access is unavailable.
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
    JOB_EXECUTION_MODE      Optional. "worker" | "inline". Default "worker" in
                            production, "inline" in development.
    WORKER_POLL_INTERVAL_SECONDS       Optional. Default 5.
    WORKER_RECOVERY_INTERVAL_SECONDS   Optional. Default 60. 0 disables.
    WORKER_SHUTDOWN_GRACE_SECONDS      Optional. Default 30.
    RETRY_BACKOFF_SECONDS   Optional. Base retry delay. Default 30.
    RETRY_BACKOFF_CAP_SECONDS          Optional. Default 600.
    READINESS_TIMEOUT_SECONDS          Optional. Per-dependency. Default 3.
    AWS_CONNECT_TIMEOUT_SECONDS        Optional. Default 10.
    AWS_READ_TIMEOUT_SECONDS           Optional. Default 60.
    AWS_MAX_ATTEMPTS        Optional. botocore attempt budget. Default 3.

See .env.example for a placeholder template.
"""

from __future__ import annotations

import logging
import os
import re
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
#
# `_validate_secret` compares `supplied.lower()` against this set, which is
# only correct while every entry is itself lower-case. That was an unenforced
# invariant: a future entry containing a capital letter would have been
# compared against a lower-cased input and could therefore NEVER match,
# silently un-blocking the value it was added to block. Normalising here makes
# the invariant hold by construction rather than by reviewer vigilance.
# `test_every_blocklist_entry_is_lowercase` asserts it too.
KNOWN_INSECURE_SECRETS = frozenset(
    value.lower()
    for value in {
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


# ---------------------------------------------------------------------------
# Remediation (Phase 8)
# ---------------------------------------------------------------------------
#
# Two separate switches, because they gate two different things:
#
#   REMEDIATION_ENABLED          the proposal and review workflow. Pure
#                                application logic over findings already
#                                persisted by the analyzer. No provider, no
#                                spend, no change to any stored video.
#   REMEDIATION_IMAGE_PROVIDER   automated image generation. A separate,
#                                consequential capability; see
#                                backend/image_provider.py for why no provider
#                                is implemented.
#
# Keeping them apart means an operator can run the human review workflow - the
# part that actually exists - without implying any generation capability.

REMEDIATION_PROVIDER_NONE = "none"
REMEDIATION_PROVIDER_NOVA_CANVAS = "nova-canvas"
# Mirrors image_provider.KNOWN_PROVIDERS. Duplicated as a literal rather than
# imported because config must not import an application module: config is
# resolved at import time by app.py, and a cycle here would break startup.
# A test asserts the two sets stay identical.
REMEDIATION_IMAGE_PROVIDERS = frozenset(
    {REMEDIATION_PROVIDER_NONE, REMEDIATION_PROVIDER_NOVA_CANVAS}
)


def load_remediation_enabled() -> bool:
    """Whether the proposal and review workflow is served.

    Default on. It reads findings the analyzer already persisted and writes
    only to its own collection, so there is nothing to protect an operator
    from by defaulting it off.
    """
    return _bool_env("REMEDIATION_ENABLED", True)


def load_remediation_image_provider() -> str:
    """Which image-generation provider to use. Default "none".

    An unrecognised value is a ConfigurationError, so a typo surfaces at
    startup rather than as an opaque failure on the first approved
    remediation. A recognised-but-unimplemented value ("nova-canvas") is
    ACCEPTED here and reported as unavailable at the API, deliberately: the
    operator asked for something real that does not exist yet, and the precise
    reason belongs in the response they will actually read, not in a crash
    that stops the rest of the application from serving.
    """
    raw = (os.getenv("REMEDIATION_IMAGE_PROVIDER") or "").strip().lower()
    if not raw:
        return REMEDIATION_PROVIDER_NONE
    if raw not in REMEDIATION_IMAGE_PROVIDERS:
        raise ConfigurationError(
            f"REMEDIATION_IMAGE_PROVIDER must be one of "
            f"{sorted(REMEDIATION_IMAGE_PROVIDERS)}; got {raw!r}."
        )
    return raw


def load_remediation_min_confidence() -> float:
    """Model confidence at or above which a finding may be auto-remediated.

    `backend/prompts.py` asks the model for a confidence and
    `docs/planning.md` specifies 0.8 as the threshold, but until Phase 8 **no
    code read either** - the number was documentation only. This is that
    threshold.

    What it gates: a proposal records whether the finding met it, and
    execution of a proposal that did not is refused. It does NOT gate whether
    a proposal can be created or reviewed - a human should be able to look at
    a low-confidence finding; that is the point of review. It gates spending
    provider budget on a finding the model itself was unsure about.

    A finding with NO reported confidence does not pass. Absent is not the
    same as high, and `detections.py` deliberately keeps it absent rather than
    defaulting it.
    """
    raw = os.getenv("REMEDIATION_MIN_CONFIDENCE")
    if raw is None or not raw.strip():
        return 0.8
    try:
        value = float(raw.strip())
    except ValueError:
        raise ConfigurationError(
            "REMEDIATION_MIN_CONFIDENCE must be a number between 0 and 1."
        )
    if value != value or not (0.0 <= value <= 1.0):
        raise ConfigurationError(
            "REMEDIATION_MIN_CONFIDENCE must be between 0 and 1 inclusive."
        )
    return value


def load_max_remediations_per_video() -> int:
    """Cap on proposals per video.

    A proposal is cheap, but unbounded creation against one video is a way to
    grow the collection without limit on an endpoint that is otherwise free to
    call. The cap is generous relative to MAX_FRAMES_PER_VIDEO (120 frames,
    each able to carry several findings) while still being a bound.
    """
    return _int_env("MAX_REMEDIATIONS_PER_VIDEO", 200, minimum=1)


# ---------------------------------------------------------------------------
# Reconstruction and verification (Phase 9)
# ---------------------------------------------------------------------------

VERIFICATION_POLICY_TARGET_ONLY = "target_only"
VERIFICATION_POLICY_CLEAN_OUTPUT = "clean_output"
# Mirrors verification.POLICIES. Duplicated as literals rather than imported
# because config must not import an application module - config is resolved at
# import time by app.py and a cycle would break startup. A test holds the two
# sets equal.
VERIFICATION_POLICIES = frozenset(
    {VERIFICATION_POLICY_TARGET_ONLY, VERIFICATION_POLICY_CLEAN_OUTPUT}
)


def load_verification_policy() -> str:
    """When a re-analysis of the output counts as verifying a remediation.

    "target_only" (default)  The targeted finding is no longer detected.
                             Other findings in the output are recorded and
                             exposed but do not block, because a remediation
                             addresses ONE finding and a video with two
                             problems should not report the first fix as a
                             failure.
    "clean_output"           The output must carry no findings at all.

    An unrecognised value raises rather than defaulting, so a typo cannot
    silently select the more permissive policy.
    """
    raw = (os.getenv("VERIFICATION_POLICY") or "").strip().lower()
    if not raw:
        return VERIFICATION_POLICY_TARGET_ONLY
    if raw not in VERIFICATION_POLICIES:
        raise ConfigurationError(
            f"VERIFICATION_POLICY must be one of "
            f"{sorted(VERIFICATION_POLICIES)}; got {raw!r}."
        )
    return raw


def load_reconstruction_enabled() -> bool:
    """Whether an approved remediation may be executed at all.

    Default on. Execution needs the FFmpeg executable, so a deployment
    without it should turn this off rather than letting every attempt fail
    after claiming the work - the route then reports the capability as absent
    up front, which is what the UI renders.
    """
    return _bool_env("RECONSTRUCTION_ENABLED", True)


def load_reconstruction_timeout_seconds() -> int:
    """Wall-clock limit for one reconstruction's FFmpeg pass.

    Separate from FFMPEG_TIMEOUT_SECONDS: extraction decodes every sampled
    frame, while this is one overlay pass that either runs promptly or is
    stuck.
    """
    return _int_env("RECONSTRUCTION_TIMEOUT_SECONDS", 300, minimum=10)


def load_remediation_window_seconds() -> float:
    """Half-width of the interval the replacement is drawn over, in seconds.

    Frames are sampled at FRAME_SAMPLE_FPS, so a finding at t=3 is evidence
    about the interval around t=3 rather than a single instant. The default of
    1.0 covers one sampling interval each side without reaching into a
    neighbouring frame's evidence.
    """
    raw = os.getenv("REMEDIATION_WINDOW_SECONDS")
    if raw is None or not raw.strip():
        return 1.0
    try:
        value = float(raw.strip())
    except ValueError:
        raise ConfigurationError(
            "REMEDIATION_WINDOW_SECONDS must be a number of seconds."
        )
    if value != value or not (0.0 < value <= 30.0):
        raise ConfigurationError(
            "REMEDIATION_WINDOW_SECONDS must be greater than 0 and at most 30."
        )
    return value


def load_max_asset_bytes() -> int:
    """Ceiling on an uploaded replacement image.

    Independent of MAX_UPLOAD_BYTES, which bounds a video. An image that
    replaces part of one frame has no business being large, and the bound is
    enforced on the BYTES RECEIVED, before anything is stored.
    """
    return _int_env("MAX_REMEDIATION_ASSET_BYTES", 8 * 1024 * 1024, minimum=1024)


# ---------------------------------------------------------------------------
# AWS client timeouts (Phase 6)
# ---------------------------------------------------------------------------
#
# Before this phase only source_store built its boto3 client with a timeout.
# Every other client - Bedrock, DynamoDB, S3 in the analyzer and extractor -
# used botocore's defaults, which for a connection that stops responding means
# the call can hang far longer than any job should take, pinning the thread
# running it. Finite timeouts here are what make a worker's lease meaningful:
# a hung call must eventually raise so the lease can lapse and the job be
# recovered, rather than the worker appearing alive forever.


DEFAULT_BEDROCK_MODEL_ID = "amazon.nova-pro-v1:0"

# Deliberately permissive. A Bedrock target can be an on-demand model id
# (`amazon.nova-pro-v1:0`), a cross-region inference profile id
# (`us.amazon.nova-pro-v1:0`), or a full inference-profile / provisioned-
# throughput ARN. Validating the *shape* beyond "no whitespace, sane charset,
# sane length" would reject targets that work, so this only catches the
# mistakes that are unambiguous: empty, padded, or containing a space.
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,254}$")


def load_bedrock_model_id() -> str:
    """Return the Bedrock model (or inference profile) the analyzer invokes.

    This used to be a hardcoded constant in `analyzer.py`, whose comment gave
    the reason: an operator silently pointing the pipeline at a different model
    "would invalidate every stored result without the pipeline version
    changing".

    That concern is real but it is already answered elsewhere, which is why
    making this configurable is safe: `analyzer` records the model id it used
    on **every frame row** (`model_id`) and `pipeline` records it on the job,
    so a result is always traceable to the model that produced it and results
    from different models remain distinguishable. Freezing the value in code
    was not what provided that guarantee.

    What the hardcoding *did* break is running against real Bedrock at all. In
    many regions on-demand access to this model is unavailable and an
    **inference profile id** (`us.amazon.nova-pro-v1:0`, `eu.` …) is required;
    `invoke_model` then fails with a ValidationException that no amount of
    retrying fixes. An operator could not correct that without editing source.

    The default is the previous constant, so behaviour is unchanged unless
    `BEDROCK_MODEL_ID` is set. Changing the model changes detection behaviour
    and invalidates comparisons with older stored results - the stored
    `model_id` is how you tell.
    """
    raw = os.getenv("BEDROCK_MODEL_ID")
    if raw is None:
        return DEFAULT_BEDROCK_MODEL_ID

    value = raw.strip()
    if not value:
        raise ConfigurationError(
            "BEDROCK_MODEL_ID is set but empty. Unset it to use the default "
            f"({DEFAULT_BEDROCK_MODEL_ID}), or give a model id."
        )
    if value != raw:
        raise ConfigurationError(
            "BEDROCK_MODEL_ID has leading or trailing whitespace. Bedrock "
            "would reject the padded value with a ValidationException, so it "
            "is refused here instead of at the first billed call."
        )
    if not _MODEL_ID_PATTERN.match(value):
        raise ConfigurationError(
            "BEDROCK_MODEL_ID is not a plausible Bedrock model id, inference "
            "profile id, or ARN. Expected something like "
            f"{DEFAULT_BEDROCK_MODEL_ID!r} or "
            "'us.amazon.nova-pro-v1:0'."
        )
    return value


def load_aws_connect_timeout() -> int:
    """Seconds to wait for a TCP/TLS connection to an AWS endpoint."""
    return _int_env("AWS_CONNECT_TIMEOUT_SECONDS", 10, minimum=1)


def load_aws_read_timeout() -> int:
    """Seconds to wait for a single read from an AWS endpoint.

    Per read, not per call, so a slow-but-progressing transfer still succeeds.
    The default is generous because one Bedrock frame analysis is a model
    invocation, not a key-value lookup.
    """
    return _int_env("AWS_READ_TIMEOUT_SECONDS", 60, minimum=1)


def load_aws_max_attempts() -> int:
    """Total attempts botocore makes for one API call, including the first.

    This is the SDK-level budget. The application adds its own bounded retries
    on top for whole stages (see BEDROCK_MAX_ATTEMPTS and MAX_ATTEMPTS), so
    keep this small: the two multiply, and 3 x 3 is already nine model
    invocations for one frame.
    """
    return _int_env("AWS_MAX_ATTEMPTS", 3, minimum=1)


# ---------------------------------------------------------------------------
# Job execution and the durable worker (Phase 6)
# ---------------------------------------------------------------------------

WORKER_MODE = "worker"
INLINE_MODE = "inline"
JOB_EXECUTION_MODES = frozenset({WORKER_MODE, INLINE_MODE})


def load_job_execution_mode() -> str:
    """How an accepted analysis job gets executed.

    `worker` - the API only records the job as queued and returns. A separate
        `python backend/worker.py` process claims and runs it. The API and the
        worker restart independently, and an accepted job survives an API
        restart because the queue IS the durable job record.

    `inline` - the API additionally hands the job to an in-process
        `BackgroundTasks` thread, which is how Phases 4 and 4.1 worked. Useful
        for local development and the test suite, where running a second
        process is friction. It is NOT durable: killing the API mid-job strands
        that job until a recovery sweep requeues it.

    Default: `inline` in development so a fresh clone works end to end with one
    process, `worker` in production so nothing depends on in-process execution
    there. Set it explicitly to remove the ambiguity.
    """
    raw = (os.getenv("JOB_EXECUTION_MODE") or "").strip().lower()
    if not raw:
        return WORKER_MODE if is_production() else INLINE_MODE
    if raw not in JOB_EXECUTION_MODES:
        raise ConfigurationError(
            f"JOB_EXECUTION_MODE must be one of {sorted(JOB_EXECUTION_MODES)}."
        )
    return raw


def load_worker_poll_seconds() -> int:
    """Pause between polls when the worker finds no claimable job.

    A poll is one indexed query, so this is cheap; the floor exists only to
    stop a misconfiguration turning into a hot loop against the database.
    """
    return _int_env("WORKER_POLL_INTERVAL_SECONDS", 5, minimum=1)


def load_worker_recovery_seconds() -> int:
    """How often a worker sweeps for jobs abandoned by a dead process.

    This is what makes recovery continuous rather than startup-only. Set it to
    0 to disable the in-worker sweep and rely on an external schedule instead.
    """
    return _int_env("WORKER_RECOVERY_INTERVAL_SECONDS", 60, minimum=0)


def load_worker_shutdown_grace_seconds() -> int:
    """Seconds a worker waits for the running job to finish after a stop signal.

    On expiry the worker exits anyway and the job's lease is left to lapse, so
    a recovery sweep requeues it. Nothing is reported as completed.
    """
    return _int_env("WORKER_SHUTDOWN_GRACE_SECONDS", 30, minimum=0)


def load_retry_backoff_seconds() -> int:
    """Base delay before a job requeued after a retryable failure runs again.

    Doubled per attempt, so a dependency that is down briefly does not consume
    the whole attempt budget in a few seconds. Without this the worker would
    reclaim a just-failed job immediately and burn all MAX_ATTEMPTS at once.
    """
    return _int_env("RETRY_BACKOFF_SECONDS", 30, minimum=0)


def load_retry_backoff_cap_seconds() -> int:
    """Upper bound on the doubled backoff, so a delay cannot grow absurd."""
    return _int_env("RETRY_BACKOFF_CAP_SECONDS", 600, minimum=1)


def load_readiness_timeout_seconds() -> int:
    """Per-dependency budget for the readiness probe.

    Short and finite on purpose: a readiness check that can hang is worse than
    one that reports a dependency as unreachable.
    """
    return _int_env("READINESS_TIMEOUT_SECONDS", 3, minimum=1)


# ---------------------------------------------------------------------------
# Object storage
# ---------------------------------------------------------------------------

# The name the application has always defaulted to. It is a placeholder, not a
# real bucket: S3 bucket names are globally unique, so this one is already
# taken by somebody and will not be yours. It exists so local development and
# the test suite work with no configuration, and it is REFUSED in production
# (see load_s3_bucket) rather than being silently used there.
DEFAULT_S3_BUCKET = "novaflow-media"

# Length and character rules from the S3 bucket naming requirements. Checked
# so a typo fails at startup with a clear message instead of surfacing much
# later as a confusing NoSuchBucket on the first upload.
_BUCKET_MIN_LENGTH = 3
_BUCKET_MAX_LENGTH = 63
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _validate_bucket(name: str) -> str:
    """Validate an operator-supplied bucket name.

    Raises:
        ConfigurationError: the value cannot be a valid S3 bucket name.
    """
    stripped = name.strip()
    if not stripped:
        raise ConfigurationError("S3_BUCKET_NAME is set but empty.")
    if not (_BUCKET_MIN_LENGTH <= len(stripped) <= _BUCKET_MAX_LENGTH):
        raise ConfigurationError(
            f"S3_BUCKET_NAME must be between {_BUCKET_MIN_LENGTH} and "
            f"{_BUCKET_MAX_LENGTH} characters."
        )
    if not _BUCKET_RE.fullmatch(stripped):
        raise ConfigurationError(
            "S3_BUCKET_NAME must be a valid S3 bucket name: lowercase "
            "letters, digits, dots and hyphens only, starting and ending "
            "with a letter or digit."
        )
    if ".." in stripped or stripped.endswith("-s3alias"):
        raise ConfigurationError("S3_BUCKET_NAME is not a valid S3 bucket name.")
    return stripped


def load_s3_bucket() -> str:
    """Return the bucket holding source videos, frames and reel uploads.

    Production: `S3_BUCKET_NAME` is mandatory. The development default is a
    placeholder that no account owns, so accepting it in production would mean
    every upload failing at the first S3 call with an opaque error. Failing at
    startup instead names the actual problem.

    Development: the placeholder is used so a clone runs with no setup. There
    is deliberately NO local-disk fallback for new uploads in any environment:
    if the bucket is wrong, uploads fail loudly rather than writing to a
    filesystem that a redeployed container discards.

    One bucket is used with separate key prefixes rather than several buckets;
    `aws_resources` owns the prefix layout and is the only caller of this.
    """
    raw = os.getenv("S3_BUCKET_NAME")

    # Validated whenever the variable is present at all, matching
    # load_jwt_secret. An explicitly empty value is an operator mistake, so it
    # is reported rather than quietly treated as "unset" and replaced with the
    # placeholder - which would hide the mistake until the first upload.
    if raw is not None:
        return _validate_bucket(raw)

    if is_production():
        raise ConfigurationError(
            "S3_BUCKET_NAME is required when NOVAFLOW_ENV=production. "
            "Refusing to start with the development placeholder "
            f"{DEFAULT_S3_BUCKET!r}, which is not a bucket you own. "
            "Provision storage with infra/cloudformation/storage.yaml and set "
            "S3_BUCKET_NAME to its MediaBucketNameOut output."
        )

    logger.warning(
        "S3_BUCKET_NAME is not set; using the development placeholder %r. "
        "Uploads will fail unless a bucket of that name exists and you can "
        "write to it. Set S3_BUCKET_NAME for real storage.",
        DEFAULT_S3_BUCKET,
    )
    return DEFAULT_S3_BUCKET


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
        self.s3_bucket = load_s3_bucket()
        self.allowed_video_extensions = load_allowed_video_extensions()
        self.max_video_duration_seconds = load_max_video_duration_seconds()
        self.frame_sample_fps = load_frame_sample_fps()
        self.max_frames_per_video = load_max_frames_per_video()
        self.ffmpeg_timeout_seconds = load_ffmpeg_timeout_seconds()
        self.bedrock_max_attempts = load_bedrock_max_attempts()
        self.bedrock_model_id = load_bedrock_model_id()
        self.remediation_enabled = load_remediation_enabled()
        self.reconstruction_enabled = load_reconstruction_enabled()
        self.reconstruction_timeout_seconds = load_reconstruction_timeout_seconds()
        self.remediation_window_seconds = load_remediation_window_seconds()
        self.max_asset_bytes = load_max_asset_bytes()
        self.verification_policy = load_verification_policy()
        self.remediation_image_provider = load_remediation_image_provider()
        self.remediation_min_confidence = load_remediation_min_confidence()
        self.max_remediations_per_video = load_max_remediations_per_video()
        self.aws_connect_timeout = load_aws_connect_timeout()
        self.aws_read_timeout = load_aws_read_timeout()
        self.aws_max_attempts = load_aws_max_attempts()
        self.job_execution_mode = load_job_execution_mode()
        self.worker_poll_seconds = load_worker_poll_seconds()
        self.worker_recovery_seconds = load_worker_recovery_seconds()
        self.worker_shutdown_grace_seconds = load_worker_shutdown_grace_seconds()
        self.retry_backoff_seconds = load_retry_backoff_seconds()
        self.retry_backoff_cap_seconds = load_retry_backoff_cap_seconds()
        self.readiness_timeout_seconds = load_readiness_timeout_seconds()
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
            f"s3_bucket={self.s3_bucket!r}, "
            f"allowed_video_extensions={sorted(self.allowed_video_extensions)}, "
            f"max_video_duration_seconds={self.max_video_duration_seconds}, "
            f"frame_sample_fps={self.frame_sample_fps}, "
            f"max_frames_per_video={self.max_frames_per_video}, "
            f"ffmpeg_timeout_seconds={self.ffmpeg_timeout_seconds}, "
            f"bedrock_max_attempts={self.bedrock_max_attempts}, "
            f"remediation_enabled={self.remediation_enabled}, "
            f"remediation_image_provider={self.remediation_image_provider!r}, "
            f"remediation_min_confidence={self.remediation_min_confidence}, "
            f"reconstruction_enabled={self.reconstruction_enabled}, "
            f"verification_policy={self.verification_policy!r}, "
            f"job_execution_mode={self.job_execution_mode!r}, "
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
