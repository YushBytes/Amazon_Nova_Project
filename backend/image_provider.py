"""The image-generation provider contract, and why no provider ships with it.

Phase 8.

WHY THERE IS NO NOVA CANVAS ADAPTER IN THIS FILE
------------------------------------------------
A Bedrock image model is invoked through `bedrock-runtime.invoke_model`, whose
`body` member is typed as an opaque **blob** in the installed SDK. Verified
against this repository's own pinned dependency:

    botocore 1.43.114
    data/bedrock-runtime/2023-09-30/service-2.json.gz
    InvokeModelRequest.body -> shape "Body", type "blob"

So the SDK validates nothing about a Canvas request. The per-model JSON schema
(`taskType`, `inPaintingParams`, how the image and mask are encoded, what the
response envelope looks like) is documented only by the service, and nothing
in this repository states it:

* `backend/` has never contained a working Canvas call.
* The one prior attempt, `audit_manager.remediate_frame` (deleted in Phase 5,
  recoverable at `94205bb^:backend/audit_manager.py`), carried its own
  admission in a comment - "Since I don't have the exact Canvas schema
  confirmed, I'll use a placeholder structure" - had its `invoke_model` call
  commented out, and encoded image bytes with `.decode('latin1')`, which is
  not a valid encoding for a Bedrock image field under any schema. It returned
  its input unchanged while the caller recorded success. It is evidence of the
  bug, not of the API.
* `docs/planning.md` names the model but specifies no payload.
* `infra/iam_policy_least_privilege.json` keeps the `amazon.nova-canvas-v1:0`
  grant commented out for exactly this reason.

`docs/IMPLEMENTATION_ROADMAP.md` Phase 8 task 1 says: "Verify the payload
schema against current Bedrock documentation; **do not guess**." Writing an
adapter here would be guessing a wire format, and a guessed adapter is worse
than none: it would look implemented, pass its own mocked tests (because the
mock would encode the same guess), and fail only against the real service -
after an operator had granted it model access and pointed it at live video.

So this module ships the CONTRACT and a provider that truthfully reports
itself unavailable. `remediation_routes` surfaces that as a 503 with a
machine-readable reason, and the UI shows it as "not configured" rather than
pretending. `docs/REMEDIATION.md` records exactly what an operator and a
future phase must supply to add a real one.

WHAT A REAL PROVIDER MUST GUARANTEE
-----------------------------------
Any implementation of `ImageProvider` must:

* accept only a validated `GenerationRequest` - the caller has already checked
  the bounding box, so a provider must not re-derive geometry from model text;
* apply a finite timeout and a bounded attempt budget, built through
  `aws_clients` if it talks to AWS, so a hung call cannot keep a dead worker
  heartbeating;
* classify every failure as retryable or permanent **at the raise site**,
  never by inspecting a message afterwards;
* return bytes that have already passed `validate_image_bytes`, or raise. A
  2xx response is not evidence of an image;
* never log a prompt, a signed URL, a token, or response bytes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output limits
# ---------------------------------------------------------------------------

# Accepted output formats, by magic number. Provider-supplied filenames and
# content types are not trusted: the bytes are what gets stored and served.
_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}

# A remediated frame is one frame of a <=60s clip. 16 MiB is far above any
# legitimate single frame and well below anything that would strain memory;
# the point is that the bound exists and is enforced before a write.
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MIN_IMAGE_BYTES = 64


class ProviderError(RuntimeError):
    """A provider call failed.

    `retryable` says whether the SAME request could plausibly succeed later.
    It is set explicitly at each raise site, never inferred from the message -
    the same rule `source_store` follows, and for the same reason: retrying a
    deterministic failure bills the attempt budget for one answer.
    """

    retryable = False

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = retryable


class ProviderUnavailable(ProviderError):
    """No provider is configured, or the configured one cannot be used.

    Permanent by definition: no amount of retrying configures a provider. The
    message is operator-facing and safe to show a user, because it names a
    capability rather than an internal detail.
    """

    def __init__(self, *args):
        super().__init__(*args, retryable=False)


class InvalidProviderOutput(ProviderError):
    """The provider answered, but not with a usable image.

    Permanent: the same request would produce the same unusable answer. Raised
    for an empty body, an unrecognised format, an oversized payload, or a
    response envelope that does not carry image data at all.
    """

    def __init__(self, *args):
        super().__init__(*args, retryable=False)


# ---------------------------------------------------------------------------
# Request and result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GenerationRequest:
    """Everything a provider needs, already validated by the caller.

    Deliberately carries no S3 key, no media token, no username and no raw
    model text. A provider receives the image bytes and a validated region,
    so a provider implementation cannot reach storage or identity on its own.

    `region` is [ymin, xmin, ymax, xmax] in the documented 0-1000 normalized
    space - the same contract as `backend/prompts.py` and
    `detections.BoundingBox` - already checked for length, type, range and
    ordering. A provider converts to pixels using `image_bytes`' own
    dimensions; it must never trust a separately supplied width or height.
    """

    video_id: str
    frame_number: int
    image_bytes: bytes
    image_content_type: str
    region: tuple[float, float, float, float]
    instruction: str


@dataclass(frozen=True)
class GenerationResult:
    """A validated generated image.

    Constructing one is a claim that `image_bytes` passed
    `validate_image_bytes`. `content_type` is derived from the bytes, never
    from the provider's own declaration.
    """

    image_bytes: bytes
    content_type: str
    provider: str
    model_id: str | None = None


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def validate_image_bytes(data: object) -> str:
    """Return the detected content type, or raise InvalidProviderOutput.

    Treats provider output as untrusted. Checks, in order: it is bytes; it is
    not empty; it is not absurdly small; it is within MAX_IMAGE_BYTES; and its
    leading bytes match a format this application serves. The declared content
    type of the response is ignored entirely - a provider that mislabels PNG
    as JPEG would otherwise have its label stored and served.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise InvalidProviderOutput(
            "The image service did not return image data."
        )
    if len(data) == 0:
        raise InvalidProviderOutput("The image service returned no image data.")
    if len(data) < MIN_IMAGE_BYTES:
        raise InvalidProviderOutput(
            "The image service returned a truncated image."
        )
    if len(data) > MAX_IMAGE_BYTES:
        # Checked before any write, so an oversized payload never reaches
        # storage or a response body.
        raise InvalidProviderOutput(
            "The generated image exceeded the maximum permitted size."
        )

    head = bytes(data[:8])
    for magic, content_type in _MAGIC.items():
        if head.startswith(magic):
            return content_type

    raise InvalidProviderOutput(
        "The image service returned an unsupported image format."
    )


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class ImageProvider(Protocol):
    """A source of remediated frame images.

    Narrow on purpose: one method, no storage access, no identity. That is
    what keeps a provider swappable and keeps its blast radius to the bytes it
    returns.
    """

    name: str

    def is_available(self) -> bool:
        """Whether a call could be attempted at all.

        Must not perform network I/O. It answers a configuration question, so
        a route can report "not configured" without spending a request.
        """
        ...

    def unavailable_reason(self) -> str | None:
        """A safe, operator-facing explanation, or None when available."""
        ...

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Produce a remediated image.

        Raises:
            ProviderUnavailable: not configured.
            InvalidProviderOutput: answered, but not with a usable image.
            ProviderError: anything else, with `retryable` set at the raise
                site.
        """
        ...


# Providers an operator may name. Declared before NullImageProvider because
# that class defaults its name to PROVIDER_NONE.
#
# `none` is the default and the only one with an implementation.
# `nova-canvas` is listed so that configuring it produces a precise error
# naming what is missing, rather than an opaque "unknown provider" - the
# operator has asked for something real that does not exist yet, and deserves
# to be told which part.
PROVIDER_NONE = "none"
PROVIDER_NOVA_CANVAS = "nova-canvas"
KNOWN_PROVIDERS = frozenset({PROVIDER_NONE, PROVIDER_NOVA_CANVAS})

NOVA_CANVAS_NOT_IMPLEMENTED = (
    "The nova-canvas image provider is not implemented. Its request and "
    "response schema could not be verified from this project's pinned SDK "
    "(bedrock-runtime InvokeModel takes an opaque blob body), from existing "
    "code, or from documentation available in the repository, and the project "
    "forbids guessing a provider wire format. See docs/REMEDIATION.md for "
    "what an implementation must supply."
)


class NullImageProvider:
    """The only provider that ships: it generates nothing and says so.

    This is NOT a mock standing in for a real one. It never returns an image,
    never fabricates a result, and `generate` always raises. Its purpose is to
    make "no generation capability" an explicit, testable state that the API
    and the UI can report truthfully, instead of leaving the feature looking
    half-wired.
    """

    def __init__(self, reason: str | None = None, name: str = PROVIDER_NONE) -> None:
        # The CONFIGURED name, not a fixed "none". An operator who set
        # REMEDIATION_IMAGE_PROVIDER=nova-canvas must see that name in the
        # capability response alongside available=false and the reason -
        # reporting "none" would read as "you configured nothing", which is a
        # different and misleading problem to debug.
        self.name = name
        self._reason = reason or (
            "Automated image generation is not configured on this deployment."
        )

    def is_available(self) -> bool:
        return False

    def unavailable_reason(self) -> str | None:
        return self._reason

    def generate(self, request: GenerationRequest) -> GenerationResult:
        # Never returns input bytes, a placeholder, or a fabricated success.
        raise ProviderUnavailable(self._reason)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_provider(name: str) -> ImageProvider:
    """Build the configured provider.

    Returns a provider for `none`. For a known-but-unimplemented name it
    returns a NullImageProvider carrying the specific reason, so the API
    answers "this provider is not implemented, here is why" rather than
    silently behaving as if nothing were configured.

    An unknown name is a configuration error and is raised by `config`, not
    here; this function is given an already-validated value.
    """
    normalized = (name or PROVIDER_NONE).strip().lower()
    if normalized == PROVIDER_NOVA_CANVAS:
        logger.warning(
            "Image provider %r is configured but not implemented; "
            "remediation will report generation as unavailable",
            normalized,
        )
        return NullImageProvider(
            NOVA_CANVAS_NOT_IMPLEMENTED, name=PROVIDER_NOVA_CANVAS
        )
    return NullImageProvider()


_provider: ImageProvider | None = None


def get_provider() -> ImageProvider:
    """The process-wide provider, built on first use."""
    global _provider
    if _provider is None:
        import config

        _provider = resolve_provider(config.load_remediation_image_provider())
    return _provider


def reset_provider() -> None:
    """Drop the cached provider. For tests that vary the configuration."""
    global _provider
    _provider = None


def provider_status() -> dict:
    """A safe description of generation capability, for an API response.

    Carries no credential, endpoint, bucket or model ARN - only whether a call
    could be attempted and, if not, why in words a user can act on.
    """
    provider = get_provider()
    return {
        "provider": provider.name,
        "available": provider.is_available(),
        "reason": provider.unavailable_reason(),
    }


__all__ = (
    "GenerationRequest",
    "GenerationResult",
    "ImageProvider",
    "InvalidProviderOutput",
    "KNOWN_PROVIDERS",
    "MAX_IMAGE_BYTES",
    "MIN_IMAGE_BYTES",
    "NOVA_CANVAS_NOT_IMPLEMENTED",
    "NullImageProvider",
    "PROVIDER_NONE",
    "PROVIDER_NOVA_CANVAS",
    "ProviderError",
    "ProviderUnavailable",
    "get_provider",
    "provider_status",
    "reset_provider",
    "resolve_provider",
    "validate_image_bytes",
)
