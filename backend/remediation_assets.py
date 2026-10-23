"""Validated storage for a remediation's replacement image.

Phase 9.

WHERE THE ASSET COMES FROM
--------------------------
Two possible sources, and the record says which:

  "upload"   the owner supplied the file. This is the only path that works
             today, and it is why Phase 9 can build an end-to-end workflow at
             all: Phase 8 established that the Nova Canvas request schema
             could not be verified from anything in this repository, so there
             is no provider to generate one.
  <provider> a provider generated it. `image_provider` still ships only
             `NullImageProvider`, so nothing can take this path.

An uploaded asset is NOT a shortcut around review. It can only be attached to
a proposal that is already `approved`, by that proposal's owner, and the
finding is re-fingerprinted first. Supplying a file is the equivalent of a
provider returning bytes - it replaces the generation step, not the approval.

WHAT IS TRUSTED
---------------
Nothing the client sends about the file. The declared filename and the
declared content type are both ignored for every decision:

* the media type is determined by SNIFFING the bytes, through the same
  `image_provider.validate_image_bytes` a real provider's output would pass
  through, so an executable renamed `logo.png` is rejected on its magic
  number;
* the stored extension is derived from the sniffed type;
* the object key is built from the server-minted remediation id.

So no part of a client filename reaches a storage key, and a stored object can
never carry a media type that a browser would execute.
"""

from __future__ import annotations

import logging
import os

from botocore.exceptions import BotoCoreError, ClientError

import aws_resources
import image_provider
from image_provider import InvalidProviderOutput

logger = logging.getLogger(__name__)

# Source labels recorded on the record. "upload" is the only one reachable.
SOURCE_UPLOAD = "upload"

# Pixel bounds on a replacement image (Phase 9.1). The floor matches
# `reconstruction.MIN_REGION_PIXELS`: an image smaller than the smallest
# region we will edit cannot usefully cover one. The ceiling bounds the
# scaler's work - an 8 MiB PNG can still decode to something enormous, and
# `MAX_IMAGE_BYTES` alone does not bound dimensions.
MIN_ASSET_PIXELS = 8
MAX_ASSET_PIXELS = 8192

# Extension per sniffed media type. Only the two formats
# `image_provider.validate_image_bytes` accepts appear here, so an unknown
# type cannot reach a key.
_EXTENSION_FOR = {
    "image/jpeg": "jpg",
    "image/png": "png",
}


class AssetError(RuntimeError):
    """A replacement asset could not be accepted or stored.

    `retryable` says whether the SAME input could plausibly succeed later -
    set explicitly at each raise site, never inferred from the message, which
    is the rule `source_store` and `image_provider` both follow.
    """

    retryable = False

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = retryable


class AssetRejected(AssetError):
    """The bytes are not an acceptable image. Permanent by definition."""

    def __init__(self, *args):
        super().__init__(*args, retryable=False)


class AssetStoreError(AssetError):
    """The asset is fine but could not be stored."""


def _s3_client():
    """The shared, timed S3 client.

    Through `source_store.s3_client` rather than a second builder, so there is
    one place that applies the finite connect/read timeouts and the bounded
    retry budget, and one place the test fake patches.
    """
    import source_store

    return source_store.s3_client()


def validate_asset_bytes(data: object, *, require_decodable: bool = True) -> tuple[str, str]:
    """Return (content_type, extension) for acceptable bytes, else raise.

    Two checks, and the second matters more than it looks:

    1. **Sniffing and bounds**, delegated to
       `image_provider.validate_image_bytes`, so an owner-supplied file and a
       provider-generated one are held to exactly the same standard:
       non-empty, not truncated, within the size cap, and a recognised format.

    2. **Decodability** (Phase 9.1). A magic number proves only that the first
       few bytes look like PNG or JPEG. A file that passes the sniff and then
       fails to decode is not harmless: handed to FFmpeg as a looping overlay
       input it **blocks**, so the reconstruction pass runs until its timeout
       expires - and a timeout is retryable, so the whole attempt budget is
       spent on a file that can never work. Measured against FFmpeg 9.0.1, an
       8-byte PNG header followed by zeroes consumed the full 300s timeout,
       three times over. Rejecting it here costs ~60ms and gives the uploader
       an accurate answer instead.

    `require_decodable=False` skips (2). Used only where the bytes have
    already been probed, never to get a stubborn file through.

    Raises:
        AssetRejected: the bytes are empty, truncated, oversized, not bytes,
            not a supported image, or not decodable as one.
    """
    try:
        content_type = image_provider.validate_image_bytes(data)
    except InvalidProviderOutput as exc:
        # The message is already safe and user-facing; it names the problem
        # with the file, not an internal detail.
        raise AssetRejected(str(exc)) from exc

    extension = _EXTENSION_FOR.get(content_type)
    if extension is None:
        # Defence in depth: unreachable unless the two modules drift apart,
        # which a test asserts they do not.
        raise AssetRejected("That image format is not supported.")

    if require_decodable:
        _require_decodable(bytes(data), extension)  # type: ignore[arg-type]

    return content_type, extension


def _require_decodable(data: bytes, extension: str) -> None:
    """Probe the bytes as an image, rejecting anything that will not decode.

    Degrades deliberately when FFmpeg is absent: on such a host
    `RECONSTRUCTION_ENABLED` should be false, so nothing will consume the
    asset and refusing the upload would block the review workflow for no
    benefit. The sniff and the size bound still applied.
    """
    import shutil
    import tempfile

    import reconstruction

    if shutil.which("ffprobe") is None:
        logger.info(
            "ffprobe is unavailable; accepting an asset on its magic number "
            "and size alone. Reconstruction should be disabled on this host."
        )
        return

    # A real file, because ffprobe needs to seek. Removed on every path.
    handle = tempfile.NamedTemporaryFile(
        suffix=f".{extension}", delete=False, prefix="novaflow-asset-"
    )
    try:
        handle.write(data)
        handle.close()
        try:
            width, height = reconstruction.probe_image(handle.name)
        except reconstruction.ReconstructionError as exc:
            # `probe_image` messages are already user-facing and name the
            # problem with the file, not an internal detail.
            raise AssetRejected(str(exc)) from exc

        if width < MIN_ASSET_PIXELS or height < MIN_ASSET_PIXELS:
            raise AssetRejected(
                f"That image is {width}x{height} pixels, which is too small "
                f"to use as a replacement."
            )
        if width > MAX_ASSET_PIXELS or height > MAX_ASSET_PIXELS:
            raise AssetRejected(
                f"That image is {width}x{height} pixels, which is larger than "
                f"the {MAX_ASSET_PIXELS}px limit."
            )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            logger.warning("Could not remove a staged asset probe file")


def store_asset(
    *,
    remediation_id: str,
    data: bytes,
    source: str,
) -> dict:
    """Validate and store one replacement asset. Returns the stored facts.

    The returned mapping is exactly the set of fields the caller persists on
    the remediation record. It carries the key for the server's own use; the
    client-facing `remediation_repo.view` never emits it.

    Raises:
        AssetRejected: the bytes are not an acceptable image.
        AssetStoreError: the object could not be written.
    """
    content_type, extension = validate_asset_bytes(data)

    bucket = aws_resources.media_bucket()
    key = aws_resources.remediation_asset_key(remediation_id, extension)

    try:
        _s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # Defence in depth: the bucket template also enforces encryption,
            # but a bucket created by hand might not.
            ServerSideEncryption="AES256",
        )
    except (BotoCoreError, ClientError) as exc:
        # No bucket name, key or botocore text reaches the caller's message.
        logger.exception("Could not store a remediation asset")
        raise AssetStoreError(
            "The replacement image could not be stored. Please try again.",
            retryable=True,
        ) from exc

    logger.info(
        "Stored a remediation asset (%d bytes, %s, source=%s)",
        len(data),
        content_type,
        source,
    )
    return {
        "asset_key": key,
        "asset_bytes": len(data),
        "asset_content_type": content_type,
        "asset_source": source,
    }


def download_asset(record: dict, destination) -> str:
    """Fetch a stored asset to a local path for FFmpeg to read.

    Raises:
        AssetError: the record names no asset, the key does not belong to this
            remediation, or the object could not be read.
    """
    key = record.get("asset_key")
    remediation_id = record.get("remediation_id")
    if not key or not remediation_id:
        raise AssetRejected("No replacement image is attached to this remediation.")

    # The key is server-generated from the remediation id, so one that does
    # not name THIS remediation means the record was tampered with or crossed
    # with another's. Refuse rather than composite another user's image into
    # this owner's video.
    expected_prefix = (
        f"{aws_resources.REMEDIATION_ASSET_PREFIX}/{remediation_id}/"
    )
    if not str(key).startswith(expected_prefix):
        logger.error(
            "Refusing an asset whose key does not belong to this remediation"
        )
        raise AssetRejected("The replacement image could not be verified.")

    bucket = aws_resources.media_bucket()
    try:
        response = _s3_client().get_object(Bucket=bucket, Key=str(key))
        body = response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Could not read a remediation asset")
        raise AssetStoreError(
            "The replacement image could not be retrieved.", retryable=True
        ) from exc

    # Re-validated on the way OUT as well as in. The object could have been
    # replaced out of band, and these bytes are about to be composited into a
    # video that a compliance claim will be made about.
    validate_asset_bytes(body)

    with open(destination, "wb") as handle:
        handle.write(body)
    return str(destination)


def delete_asset(key: str | None) -> None:
    """Best-effort removal of a stored asset.

    Used as compensating cleanup when the record write fails after the upload
    succeeded: without a record the object is unattributable and unreachable,
    so leaving it would be an orphan nobody can find or bill. Never used to
    tidy up a retained asset - a record's asset stays for audit even after the
    remediation is cancelled.
    """
    if not key:
        return
    try:
        _s3_client().delete_object(
            Bucket=aws_resources.media_bucket(), Key=str(key)
        )
    except (BotoCoreError, ClientError):
        # Logged, not raised: the caller is already handling a failure, and an
        # orphaned object is a smaller problem than masking the original one.
        logger.warning("Could not remove an orphaned remediation asset")


__all__ = (
    "MAX_ASSET_PIXELS",
    "MIN_ASSET_PIXELS",
    "SOURCE_UPLOAD",
    "AssetError",
    "AssetRejected",
    "AssetStoreError",
    "delete_asset",
    "download_asset",
    "store_asset",
    "validate_asset_bytes",
)
