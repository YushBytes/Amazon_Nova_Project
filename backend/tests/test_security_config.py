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
