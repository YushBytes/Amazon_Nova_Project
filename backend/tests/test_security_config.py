"""Phase 3: JWT signing-key configuration must fail safely.

Audit reference: docs/SECURITY_AUDIT.md, finding S1.

`auth.py` previously contained a hardcoded HMAC signing key
("brand-guardian-dev-secret-change-in-prod"). Anyone who had seen the
repository could forge a token for any username and any role. The key now comes
from the environment, and these tests pin the fail-safe behaviour.

No real secret appears in this file; the keys below are synthetic test values.
"""

import importlib

import pytest

import config


SYNTHETIC_GOOD_KEY = "unit-test-signing-key-long-enough-0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Each test resolves configuration from scratch."""
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


# ---------------------------------------------------------------------------
# The old hardcoded secret must be gone from runtime code
# ---------------------------------------------------------------------------

def test_auth_module_has_no_hardcoded_secret():
    """The signing key must not be a module-level literal in auth.py."""
    import auth

    source = importlib.util.find_spec("auth").origin
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "brand-guardian-dev-secret" not in text, (
        "the previously hardcoded signing key is still present in auth.py"
    )
    # There must be no module-level SECRET_KEY constant any more.
    assert not hasattr(auth, "SECRET_KEY"), (
        "auth.SECRET_KEY still exists; the key must come from config"
    )


def test_known_insecure_secret_list_includes_the_old_value():
    """The leaked placeholder must be permanently blocklisted."""
    assert "brand-guardian-dev-secret-change-in-prod" in config.KNOWN_INSECURE_SECRETS


# ---------------------------------------------------------------------------
# Production fails safely
# ---------------------------------------------------------------------------

def test_production_without_secret_refuses_to_start(monkeypatch):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(config.ConfigurationError) as excinfo:
        config.Settings()

    assert "JWT_SECRET_KEY" in str(excinfo.value)


def test_production_rejects_the_old_hardcoded_secret(monkeypatch):
    """Even if an operator pastes the leaked value, it must be refused."""
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "brand-guardian-dev-secret-change-in-prod")

    with pytest.raises(config.ConfigurationError):
        config.Settings()


@pytest.mark.parametrize("weak", ["changeme", "secret", "test", "password", "dev"])
def test_known_insecure_placeholders_are_rejected(monkeypatch, weak):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", weak)
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_short_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * (config.MIN_SECRET_LENGTH - 1))
    with pytest.raises(config.ConfigurationError) as excinfo:
        config.Settings()
    assert str(config.MIN_SECRET_LENGTH) in str(excinfo.value)


def test_empty_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "   ")
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_error_message_never_echoes_the_rejected_value(monkeypatch):
    """A misconfiguration must not leak the value into logs or tracebacks."""
    sentinel = "zzz-should-never-be-echoed-zzz"
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", sentinel)  # too short -> rejected

    with pytest.raises(config.ConfigurationError) as excinfo:
        config.Settings()

    assert sentinel not in str(excinfo.value)


def test_valid_production_secret_is_accepted(monkeypatch):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)

    settings = config.Settings()
    assert settings.is_production is True
    assert settings.jwt_secret == SYNTHETIC_GOOD_KEY


def test_settings_repr_redacts_the_secret(monkeypatch):
    """Settings objects can appear in tracebacks; the key must not."""
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)

    rendered = repr(config.Settings())
    assert SYNTHETIC_GOOD_KEY not in rendered
    assert "redacted" in rendered


# ---------------------------------------------------------------------------
# Development behaviour
# ---------------------------------------------------------------------------

def test_development_without_secret_uses_an_ephemeral_key(monkeypatch):
    """Dev must work without configuration, but never with a guessable key."""
    monkeypatch.setenv("NOVAFLOW_ENV", "development")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    first = config.Settings().jwt_secret
    config.reset_settings_cache()
    second = config.Settings().jwt_secret

    assert len(first) >= config.MIN_SECRET_LENGTH
    assert first.lower() not in config.KNOWN_INSECURE_SECRETS
    # Freshly generated each time, so it is not a constant baked into the code.
    assert first != second


def test_development_still_rejects_an_insecure_supplied_secret(monkeypatch):
    """The blocklist applies in development too, so the old value cannot return."""
    monkeypatch.setenv("NOVAFLOW_ENV", "development")
    monkeypatch.setenv("JWT_SECRET_KEY", "brand-guardian-dev-secret-change-in-prod")

    with pytest.raises(config.ConfigurationError):
        config.Settings()


# ---------------------------------------------------------------------------
# Algorithm, limits, CORS
# ---------------------------------------------------------------------------

def test_algorithm_is_restricted_to_an_allowlist(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.setenv("JWT_ALGORITHM", "none")
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_asymmetric_algorithm_is_not_silently_accepted(monkeypatch):
    """RS256 with an HMAC key would be a confused-algorithm hazard."""
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.setenv("JWT_ALGORITHM", "RS256")
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_cors_wildcard_is_rejected(monkeypatch):
    """"*" with credentials would let any site use a victim's token."""
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,*")
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_cors_defaults_to_the_local_frontend(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    assert config.Settings().cors_origins == ["http://localhost:3000"]


def test_upload_limit_defaults_and_validates(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    assert config.Settings().max_upload_bytes == 100 * 1024 * 1024

    config.reset_settings_cache()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "not-a-number")
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_extension_allowlist_is_alphanumeric_only(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.setenv("ALLOWED_VIDEO_EXTENSIONS", "mp4,../sh")
    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_docs_are_hidden_in_production_by_default(monkeypatch):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.delenv("EXPOSE_API_DOCS", raising=False)
    assert config.Settings().expose_docs is False

    config.reset_settings_cache()
    monkeypatch.setenv("EXPOSE_API_DOCS", "true")
    assert config.Settings().expose_docs is True


def test_default_signup_role_is_least_privileged():
    assert config.DEFAULT_SIGNUP_ROLE == config.ROLE_CLIENT
    assert config.ROLE_AGENT in config.VALID_ROLES


# ---------------------------------------------------------------------------
# Phase 5: object-storage configuration
# ---------------------------------------------------------------------------
#
# Before this phase the bucket name was read with a bare
# `os.getenv("S3_BUCKET_NAME", "novaflow-media")`. The default is a placeholder
# nobody owns, so a production deployment that forgot the variable started
# cleanly and then failed on every upload with an opaque S3 error. These tests
# pin the contract: required in production, validated always, and never a
# silent fall back to local disk.

def test_bucket_name_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("S3_BUCKET_NAME", "my-real-bucket")
    assert config.load_s3_bucket() == "my-real-bucket"


def test_bucket_name_is_surrounding_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("S3_BUCKET_NAME", "  my-real-bucket  ")
    assert config.load_s3_bucket() == "my-real-bucket"


def test_development_falls_back_to_the_placeholder(monkeypatch):
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.setenv("NOVAFLOW_ENV", "development")
    assert config.load_s3_bucket() == config.DEFAULT_S3_BUCKET


def test_production_requires_an_explicit_bucket(monkeypatch):
    """The placeholder must never be used in production."""
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)

    with pytest.raises(config.ConfigurationError) as exc:
        config.load_s3_bucket()
    assert "S3_BUCKET_NAME is required" in str(exc.value)


def test_production_startup_fails_without_a_bucket(monkeypatch):
    """Settings construction - i.e. app startup - refuses to proceed."""
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)

    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_production_accepts_an_explicit_bucket(monkeypatch):
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", SYNTHETIC_GOOD_KEY)
    monkeypatch.setenv("S3_BUCKET_NAME", "novaflow-prod-media-7f3a1c")
    assert config.Settings().s3_bucket == "novaflow-prod-media-7f3a1c"


@pytest.mark.parametrize(
    "value",
    [
        "",                     # set but empty
        "   ",                  # whitespace only
        "ab",                   # too short
        "a" * 64,               # too long
        "UPPERCASE",            # S3 forbids uppercase
        "under_scores",         # underscores are not allowed
        "-leading-hyphen",      # must start alphanumeric
        "trailing-hyphen-",     # must end alphanumeric
        "double..dot",          # consecutive dots
        "has spaces",
        "bucket/with/slashes",  # a key, not a bucket
    ],
)
def test_malformed_bucket_names_are_rejected(monkeypatch, value):
    """A typo must fail at startup, not on the first upload."""
    monkeypatch.setenv("S3_BUCKET_NAME", value)
    with pytest.raises(config.ConfigurationError):
        config.load_s3_bucket()


def test_bucket_error_messages_do_not_echo_the_value(monkeypatch):
    """Configuration errors name the problem, not the operator's input.

    Consistent with the signing-key errors: these messages reach logs, and a
    bucket name can identify a customer or an internal project.
    """
    secretish = "acme-confidential-launch-footage"
    monkeypatch.setenv("S3_BUCKET_NAME", secretish.upper())
    with pytest.raises(config.ConfigurationError) as exc:
        config.load_s3_bucket()
    assert secretish not in str(exc.value).lower()


def test_aws_resources_resolves_the_bucket_through_config(monkeypatch):
    """There must be exactly one storage contract.

    `aws_resources.media_bucket()` is what the pipeline calls; it must honour
    the same validation, or production enforcement could be bypassed simply by
    calling the other accessor.
    """
    import aws_resources

    monkeypatch.setenv("S3_BUCKET_NAME", "resolved-through-config")
    assert aws_resources.media_bucket() == "resolved-through-config"

    monkeypatch.setenv("S3_BUCKET_NAME", "Not A Bucket")
    with pytest.raises(config.ConfigurationError):
        aws_resources.media_bucket()


def test_no_module_reimplements_the_bucket_fallback():
    """Only config may name the placeholder or read S3_BUCKET_NAME.

    A second `os.getenv("S3_BUCKET_NAME", ...)` elsewhere would reintroduce the
    contradictory-default problem this phase removed.
    """
    import re
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parent.parent
    allowed = {"config.py"}
    offenders = []
    for path in sorted(backend_dir.glob("*.py")):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"""getenv\(\s*["']S3_BUCKET_NAME["']""", line):
                offenders.append(f"{path.name}:{number} reads S3_BUCKET_NAME directly")
            if "novaflow-media" in line:
                offenders.append(f"{path.name}:{number} hardcodes the placeholder")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# The blocklist comparison is case-robust (Phase 10 finalization)
# ---------------------------------------------------------------------------
#
# `_validate_secret` tests `supplied.lower() in KNOWN_INSECURE_SECRETS`. That
# is only correct while every entry in the set is itself lower-case: an entry
# containing a capital letter would be compared against a lower-cased input,
# could never match, and would silently fail to block the very value it was
# added for. All twelve entries happened to be lower-case, so the bug was
# latent rather than live - these tests stop it becoming live.

def test_every_blocklist_entry_is_lowercase():
    """The invariant `_validate_secret`'s comparison depends on."""
    offenders = [s for s in config.KNOWN_INSECURE_SECRETS if s != s.lower()]
    assert not offenders, (
        f"{offenders} would never match a lower-cased input and so would "
        f"never actually be blocked"
    )


@pytest.mark.parametrize(
    "variant",
    [
        "BRAND-GUARDIAN-DEV-SECRET-CHANGE-IN-PROD",
        "Brand-Guardian-Dev-Secret-Change-In-Prod",
        "  brand-guardian-dev-secret-change-in-prod  ",
    ],
)
def test_a_cased_or_padded_leaked_secret_is_still_rejected(monkeypatch, variant):
    """Re-casing or padding the exposed value must not get it past the gate.

    The value is public: it sits in this repository's history, and it is also
    published deliberately in `KNOWN_INSECURE_SECRETS`. The security property
    is therefore that it is REFUSED, not that it is unknown - so the refusal
    has to survive the obvious ways someone might retype it.
    """
    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", variant)

    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_development_also_refuses_the_leaked_secret(monkeypatch):
    """Not only production: a supplied insecure value is rejected everywhere.

    Development's convenience is a RANDOM ephemeral key when nothing is set -
    never a fallback to a known value.
    """
    monkeypatch.setenv("NOVAFLOW_ENV", "development")
    monkeypatch.setenv("JWT_SECRET_KEY", "brand-guardian-dev-secret-change-in-prod")

    with pytest.raises(config.ConfigurationError):
        config.Settings()


def test_an_unset_development_secret_is_random_not_a_known_value(monkeypatch):
    """Two processes must not share a guessable key."""
    monkeypatch.setenv("NOVAFLOW_ENV", "development")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    first = config.load_jwt_secret()
    second = config.load_jwt_secret()

    assert first != second, "the development key is not ephemeral"
    assert first.lower() not in config.KNOWN_INSECURE_SECRETS
    assert len(first) >= config.MIN_SECRET_LENGTH


# ---------------------------------------------------------------------------
# Bedrock model id is configurable (Phase 10 completion)
# ---------------------------------------------------------------------------
#
# `analyzer.MODEL_ID` was a hardcoded constant. Its comment gave a real
# reason - an operator repointing the pipeline would "invalidate every stored
# result" - but that is answered by RECORDING the model id, which the analyzer
# already does on every frame row and the pipeline on every job. Freezing it
# was not what provided traceability.
#
# What freezing it did prevent was running against real Bedrock at all: many
# regions have no on-demand access to this model and require an inference
# profile id (`us.amazon.nova-pro-v1:0`). `invoke_model` rejects the plain id
# there with a ValidationException, which `analyzer.PERMANENT_ERROR_CODES`
# correctly refuses to retry - so every frame fails and no operator could fix
# it without editing source.

def test_the_default_model_is_unchanged(monkeypatch):
    """Behaviour must be identical unless BEDROCK_MODEL_ID is set."""
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    assert config.load_bedrock_model_id() == "amazon.nova-pro-v1:0"
    assert config.DEFAULT_BEDROCK_MODEL_ID == "amazon.nova-pro-v1:0"


@pytest.mark.parametrize(
    "value",
    [
        "amazon.nova-pro-v1:0",
        # The reason this setting exists: regional inference profiles.
        "us.amazon.nova-pro-v1:0",
        "eu.amazon.nova-pro-v1:0",
        # A full inference-profile ARN must also be accepted.
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0",
    ],
)
def test_a_plausible_bedrock_target_is_accepted(monkeypatch, value):
    monkeypatch.setenv("BEDROCK_MODEL_ID", value)
    assert config.load_bedrock_model_id() == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        " amazon.nova-pro-v1:0",      # padded; Bedrock would reject it
        "amazon.nova-pro-v1:0 ",
        "amazon nova pro",            # contains a space
        "-leading-dash",              # must start alphanumeric
        "ab",                         # implausibly short
    ],
)
def test_an_implausible_model_id_is_refused_before_any_billed_call(
    monkeypatch, value
):
    """Refused at startup, not at the first paid invocation."""
    monkeypatch.setenv("BEDROCK_MODEL_ID", value)
    with pytest.raises(config.ConfigurationError):
        config.load_bedrock_model_id()


def test_settings_exposes_the_configured_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
    assert config.Settings().bedrock_model_id == "us.amazon.nova-pro-v1:0"


def test_the_analyzer_uses_the_configured_model(monkeypatch):
    """The whole point: the analyzer must honour the configured value."""
    import analyzer

    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
    config.reset_settings_cache()
    try:
        assert analyzer.model_id() == "us.amazon.nova-pro-v1:0"
    finally:
        config.reset_settings_cache()


def test_the_model_id_error_names_the_variable_not_a_guess(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "not a model")
    with pytest.raises(config.ConfigurationError) as excinfo:
        config.load_bedrock_model_id()
    assert "BEDROCK_MODEL_ID" in str(excinfo.value)
