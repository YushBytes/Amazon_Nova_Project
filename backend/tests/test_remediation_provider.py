"""The image-provider contract, and the honesty of its unavailable state.

Phase 8. No provider is implemented - `backend/image_provider.py` records the
evidence for why (the pinned SDK types `InvokeModel.body` as an opaque blob,
so nothing in this repository defines the Nova Canvas wire format). These
tests pin two things:

1. the output-validation contract a real provider must satisfy, exercised with
   deterministic malformed, empty, oversized and unsupported payloads;
2. that the shipped provider never fabricates a result. `NullImageProvider`
   raising rather than returning placeholder bytes is the whole difference
   between this phase and the deleted `audit_manager.remediate_frame`, which
   returned its input unchanged while its caller recorded success.

Nothing here contacts AWS or Bedrock, and no test asserts anything about real
provider behaviour.
"""

import pytest

import config
import image_provider
from image_provider import (
    MAX_IMAGE_BYTES,
    MIN_IMAGE_BYTES,
    GenerationRequest,
    InvalidProviderOutput,
    NullImageProvider,
    ProviderError,
    ProviderUnavailable,
    provider_status,
    resolve_provider,
    validate_image_bytes,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """The provider is process-wide and memoized; isolate every test."""
    image_provider.reset_provider()
    config.reset_settings_cache()
    yield
    image_provider.reset_provider()
    config.reset_settings_cache()


def a_request() -> GenerationRequest:
    return GenerationRequest(
        video_id="vid_abcd1234",
        frame_number=3,
        image_bytes=JPEG,
        image_content_type="image/jpeg",
        region=(100.0, 200.0, 300.0, 400.0),
        instruction="Obscure the reported region.",
    )


# ---------------------------------------------------------------------------
# output validation: provider output is untrusted
# ---------------------------------------------------------------------------

def test_a_jpeg_is_accepted_and_typed_from_its_bytes():
    assert validate_image_bytes(JPEG) == "image/jpeg"


def test_a_png_is_accepted_and_typed_from_its_bytes():
    assert validate_image_bytes(PNG) == "image/png"


def test_an_empty_response_is_rejected():
    with pytest.raises(InvalidProviderOutput):
        validate_image_bytes(b"")


def test_a_truncated_response_is_rejected():
    """A few valid magic bytes are not an image."""
    with pytest.raises(InvalidProviderOutput):
        validate_image_bytes(b"\xff\xd8\xff")


def test_an_oversized_response_is_rejected_before_any_write():
    oversized = b"\xff\xd8\xff\xe0" + b"\x00" * (MAX_IMAGE_BYTES + 1)
    with pytest.raises(InvalidProviderOutput):
        validate_image_bytes(oversized)


def test_a_response_at_the_size_limit_is_accepted():
    """The bound is inclusive, so a legitimate maximum is not refused."""
    at_limit = b"\xff\xd8\xff\xe0" + b"\x00" * (MAX_IMAGE_BYTES - 4)
    assert len(at_limit) == MAX_IMAGE_BYTES
    assert validate_image_bytes(at_limit) == "image/jpeg"


@pytest.mark.parametrize(
    "payload",
    [
        b"GIF89a" + b"\x00" * 200,
        b"<html><body>error</body></html>" + b"\x00" * 200,
        b'{"error": "throttled"}' + b" " * 200,
        b"%PDF-1.4" + b"\x00" * 200,
        b"\x00" * 200,
    ],
)
def test_an_unsupported_or_non_image_payload_is_rejected(payload):
    """A 2xx response is not evidence of an image.

    A JSON error body, an HTML error page and a PDF all arrive as bytes with a
    successful status; none of them may be stored or served as a frame.
    """
    with pytest.raises(InvalidProviderOutput):
        validate_image_bytes(payload)


@pytest.mark.parametrize("payload", [None, "a string", 42, {"image": "x"}, [1, 2]])
def test_a_non_bytes_response_is_rejected(payload):
    with pytest.raises(InvalidProviderOutput):
        validate_image_bytes(payload)


def test_a_bytearray_is_accepted():
    """Some SDK read paths hand back a bytearray."""
    assert validate_image_bytes(bytearray(JPEG)) == "image/jpeg"


def test_the_declared_content_type_is_never_trusted():
    """The type is derived from the bytes, not from the provider's label.

    A provider that mislabels a PNG as JPEG would otherwise have its label
    stored and later served with the wrong type.
    """
    assert validate_image_bytes(PNG) == "image/png"
    assert validate_image_bytes(JPEG) == "image/jpeg"


def test_the_minimum_size_is_smaller_than_the_maximum():
    assert 0 < MIN_IMAGE_BYTES < MAX_IMAGE_BYTES


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

def test_unavailable_is_permanent():
    """No amount of retrying configures a provider."""
    assert ProviderUnavailable("nope").retryable is False


def test_invalid_output_is_permanent():
    """The same request would produce the same unusable answer."""
    assert InvalidProviderOutput("nope").retryable is False


def test_retryability_is_explicit_at_the_raise_site():
    """Never inferred from a message - the `source_store` rule."""
    assert ProviderError("timeout", retryable=True).retryable is True
    assert ProviderError("bad request").retryable is False


def test_the_error_hierarchy_lets_one_handler_catch_everything():
    assert issubclass(ProviderUnavailable, ProviderError)
    assert issubclass(InvalidProviderOutput, ProviderError)


# ---------------------------------------------------------------------------
# the shipped provider never fabricates anything
# ---------------------------------------------------------------------------

def test_the_null_provider_reports_itself_unavailable():
    provider = NullImageProvider()
    assert provider.is_available() is False
    assert provider.unavailable_reason()


def test_the_null_provider_raises_rather_than_returning_an_image():
    """The direct regression test for the deleted remediate_frame defect.

    That function returned its INPUT unchanged while the caller recorded
    success. A provider must never return bytes it did not generate.
    """
    provider = NullImageProvider()
    with pytest.raises(ProviderUnavailable):
        provider.generate(a_request())


def test_the_null_provider_does_not_echo_the_input_bytes():
    """Belt and braces: even on the raising path, no result is constructed."""
    provider = NullImageProvider()
    try:
        result = provider.generate(a_request())
    except ProviderUnavailable:
        result = None
    assert result is None


def test_its_reason_is_safe_to_show_a_user():
    """No bucket, ARN, endpoint, credential or exception text."""
    reason = NullImageProvider().unavailable_reason() or ""
    for leaked in ("arn:", "aws_", "secret", "amazonaws.com", "Traceback"):
        assert leaked.lower() not in reason.lower()


# ---------------------------------------------------------------------------
# resolution and configuration
# ---------------------------------------------------------------------------

def test_the_default_provider_is_none_and_unavailable(monkeypatch):
    monkeypatch.delenv("REMEDIATION_IMAGE_PROVIDER", raising=False)
    provider = image_provider.get_provider()
    assert provider.name == image_provider.PROVIDER_NONE
    assert provider.is_available() is False


def test_configuring_nova_canvas_is_unavailable_with_a_specific_reason(monkeypatch):
    """An operator who asks for Canvas is told exactly what is missing.

    Not "unknown provider", and emphatically not a working-looking adapter:
    the reason names the unverified wire format and points at the document
    that says what an implementation must supply.
    """
    monkeypatch.setenv("REMEDIATION_IMAGE_PROVIDER", "nova-canvas")
    provider = image_provider.get_provider()

    assert provider.is_available() is False
    reason = provider.unavailable_reason() or ""
    assert "not implemented" in reason
    assert "docs/REMEDIATION.md" in reason

    with pytest.raises(ProviderUnavailable):
        provider.generate(a_request())


def test_an_unknown_provider_name_is_a_configuration_error(monkeypatch):
    """A typo surfaces at startup, not on the first approved remediation."""
    monkeypatch.setenv("REMEDIATION_IMAGE_PROVIDER", "stable-diffusion")
    with pytest.raises(config.ConfigurationError):
        config.load_remediation_image_provider()


def test_resolve_provider_never_returns_an_available_provider():
    """Nothing ships that can generate. Enumerated, not assumed.

    When a real provider is added this test must be changed deliberately -
    which is the point.
    """
    for name in sorted(image_provider.KNOWN_PROVIDERS):
        assert resolve_provider(name).is_available() is False


def test_config_and_provider_agree_on_the_known_names():
    """The two lists are duplicated deliberately (config imports no app
    module) so a test has to hold them equal."""
    assert config.REMEDIATION_IMAGE_PROVIDERS == image_provider.KNOWN_PROVIDERS


# ---------------------------------------------------------------------------
# the status payload
# ---------------------------------------------------------------------------

def test_provider_status_is_safe_and_complete(monkeypatch):
    monkeypatch.delenv("REMEDIATION_IMAGE_PROVIDER", raising=False)
    status = provider_status()

    assert status["available"] is False
    assert status["provider"] == "none"
    assert status["reason"]
    # No credential, endpoint, bucket or model ARN.
    assert set(status) == {"provider", "available", "reason"}


def test_provider_status_reflects_a_configured_provider(monkeypatch):
    monkeypatch.setenv("REMEDIATION_IMAGE_PROVIDER", "nova-canvas")
    status = provider_status()
    assert status["available"] is False
    assert "not implemented" in (status["reason"] or "")


# ---------------------------------------------------------------------------
# the request carries no identity or storage detail
# ---------------------------------------------------------------------------

def test_a_generation_request_exposes_no_storage_or_identity():
    """A provider cannot reach S3 or the user from what it is handed."""
    fields = set(a_request().__dataclass_fields__)
    assert fields == {
        "video_id",
        "frame_number",
        "image_bytes",
        "image_content_type",
        "region",
        "instruction",
    }
    for leaked in ("owner", "username", "token", "key", "bucket", "url"):
        assert leaked not in fields


def test_a_generation_request_is_immutable():
    """So a provider cannot mutate the validated region it was given."""
    request = a_request()
    with pytest.raises(Exception):
        request.region = (0.0, 0.0, 1.0, 1.0)
