"""Durable storage for uploaded source videos.

Phase 4.1. Phase 4 wrote every upload to `uploads/{video_id}.{ext}` on the
application host's local disk and kept only that filename on the ownership
record. Three consequences, all of which this module removes:

* a replaced container or a scaled-out second instance lost or could not see
  the source video, so analysis failed with "Video payload not found";
* crash recovery was pointless even once leases existed, because the recovered
  worker might not be on the host that holds the file;
* the only copy of a user's upload lived on an ephemeral filesystem.

Source videos now go to the same S3 bucket the pipeline already uses, under
the server-generated `sources/{video_id}/source.{ext}` key. Nothing else about
the pipeline changes: frames still go to `frames/{video_id}/`, their metadata
still goes to DynamoDB, and ownership still lives on the MongoDB record.

LEGACY RECORDS

Records written before this phase have `stored_filename` but no `s3_key`.
`local_fallback_path()` and `SourceMissing` exist for exactly those: they are
read from local disk when the file is still there, and reported as missing
when it is not. Nothing is migrated automatically, because migrating would
mean reading files this process may not have. See docs/PIPELINE_OPERATIONS.md
for the operator-run backfill.

WHAT IS DELIBERATELY NOT DONE

* The source object is never deleted by the pipeline. A retry needs it, and a
  user may still be playing it back. Expiry is a bucket lifecycle rule, which
  is declarative and auditable; see infra/cloudformation/storage.yaml.
* Downloads are streamed to a file, never held in memory: a 100 MiB upload
  limit times concurrent jobs is not a safe amount of heap.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from botocore.exceptions import BotoCoreError, ClientError

import aws_clients
import aws_resources
from config import get_settings

logger = logging.getLogger(__name__)

# Timeouts and the retry budget now come from aws_clients (Phase 6), so the
# whole application shares one configurable policy instead of this module
# being the only one with a bound. The values are unchanged by default.

# Error codes that mean "this object will never appear", as opposed to a
# transport problem that may succeed on retry.
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})

_client = None


def s3_client():
    """Lazily build the S3 client.

    Lazy rather than module-scope so importing this module performs no
    credential resolution, and so a test can patch the module attribute
    before the first call.
    """
    global _client
    if _client is None:
        _client = aws_clients.get_client("s3")
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests that swap credentials or endpoints."""
    global _client
    _client = None


class SourceStoreError(RuntimeError):
    """A source-video storage operation failed.

    The message is written to be shown to the video's owner: no bucket name,
    object key, filesystem path, credential, or botocore string.

    `retryable` says whether the SAME input could plausibly succeed later.
    It is set explicitly at each raise site rather than inferred from the
    message, because the two kinds are not distinguishable by text and
    guessing wrong is expensive in both directions: retrying a permanent
    failure bills three times for one answer, and failing a transient one
    turns a momentary outage into a dead job. Default False - a new raise site
    has to opt in to being retried.
    """

    retryable = False

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = retryable


class SourceMissing(SourceStoreError):
    """The source object does not exist.

    Permanent by definition: no amount of retrying conjures an object back, so
    this never sets `retryable`.
    """

    def __init__(self, *args):
        super().__init__(*args, retryable=False)


@dataclass(frozen=True)
class SourceRef:
    """Where a video's source bytes actually are."""

    bucket: str
    key: str
    size_bytes: int

    def as_fields(self) -> dict:
        """The subset persisted on the ownership record."""
        return {
            "s3_bucket": self.bucket,
            "s3_key": self.key,
            "size_bytes": self.size_bytes,
        }


def _content_type_for(extension: str) -> str:
    """A conservative stored content type for a source video.

    Only the extensions the upload allowlist permits appear here. Anything
    else is stored as a generic binary rather than guessed at, so a stored
    object can never carry a type that a browser would execute.
    """
    known = {
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "webm": "video/webm",
        "mkv": "video/x-matroska",
        "avi": "video/x-msvideo",
    }
    return known.get(extension.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_source(
    *, local_path: str | Path, video_id: str, extension: str, size_bytes: int
) -> SourceRef:
    """Put a freshly received upload in S3 under a server-generated key.

    `local_path` is the temporary file the request body was streamed to. This
    function does not delete it: the caller owns that file's lifetime and must
    remove it on every path, including failure.

    Raises:
        SourceStoreError: the object could not be stored.
    """
    bucket = aws_resources.media_bucket()
    key = aws_resources.source_key(video_id, extension)

    try:
        with open(local_path, "rb") as handle:
            s3_client().put_object(
                Bucket=bucket,
                Key=key,
                Body=handle,
                ContentType=_content_type_for(extension),
                # Defence in depth: the bucket template also enforces
                # encryption, but a bucket created by hand might not.
                ServerSideEncryption="AES256",
            )
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Could not store the source video for %s", video_id)
        raise SourceStoreError(
            "The video could not be stored. Please try the upload again.",
            retryable=True,
        ) from exc
    except OSError as exc:
        logger.exception("Could not read the staged upload for %s", video_id)
        raise SourceStoreError("The upload could not be read.") from exc

    logger.info("Stored source video for %s (%d bytes)", video_id, size_bytes)
    return SourceRef(bucket=bucket, key=key, size_bytes=size_bytes)


def delete_source(ref) -> None:
    """Best-effort removal of a stored source object.

    Used as compensating cleanup when the ownership write fails after the
    upload succeeded: without a record the object is unattributable and
    unreachable, so leaving it would be an orphan nobody can find or bill.

    Never raises. A failure here is logged and left to the bucket's lifecycle
    rule, because the caller is already handling a more important error.
    """
    bucket, key = _bucket_and_key(ref)
    if not bucket or not key:
        return
    try:
        s3_client().delete_object(Bucket=bucket, Key=key)
        logger.info("Removed an orphaned source object")
    except Exception:
        logger.warning(
            "Could not remove an orphaned source object; the bucket lifecycle "
            "rule will expire it"
        )


def _bucket_and_key(ref):
    if isinstance(ref, SourceRef):
        return ref.bucket, ref.key
    if isinstance(ref, dict):
        return ref.get("s3_bucket"), ref.get("s3_key")
    return None, None


# ---------------------------------------------------------------------------
# Locating a source
# ---------------------------------------------------------------------------

def has_durable_source(record: dict) -> bool:
    """True when this record points at an S3 object.

    False for a legacy record, which is not an error - it only means the sole
    copy is whatever is still on local disk.
    """
    return bool(record.get("s3_key") and record.get("s3_bucket"))


def local_fallback_path(record: dict, upload_dir: Path):
    """The pre-Phase-4.1 local path for a legacy record, if it still exists.

    Containment is re-checked here even though `stored_filename` is
    server-generated, because this value comes back out of the database and a
    database is not a trust boundary.
    """
    stored = record.get("stored_filename")
    if not stored:
        return None
    candidate = (Path(upload_dir) / str(stored)).resolve()
    upload_dir = Path(upload_dir).resolve()
    if candidate != upload_dir and upload_dir not in candidate.parents:
        logger.error(
            "Refused a stored filename that resolved outside the upload directory"
        )
        return None
    return candidate if candidate.is_file() else None


def expected_key_prefix(video_id: str) -> str:
    """The only key prefix a given video's source may live under."""
    return f"{aws_resources.SOURCE_PREFIX}/{video_id}/"


def verify_key_belongs_to(record: dict) -> None:
    """Assert the stored key is the one this video's id would generate.

    The key is server-generated, so a key that does not start with this
    video's source prefix means the record has been tampered with or crossed
    with another video's. Refuse rather than process the wrong file under this
    job's identity and attribute another user's content to this owner.

    Raises:
        SourceMissing: the key does not belong to this record.
    """
    video_id = record.get("video_id")
    key = str(record.get("s3_key") or "")
    if not video_id:
        return
    if not key.startswith(expected_key_prefix(str(video_id))):
        logger.error(
            "Refusing a source object whose key does not belong to %s", video_id
        )
        raise SourceMissing(
            "The uploaded video could not be verified. Please upload it again."
        )


# ---------------------------------------------------------------------------
# Download for processing
# ---------------------------------------------------------------------------

@contextmanager
def materialise(record: dict, upload_dir=None) -> Iterator[str]:
    """Yield a local filesystem path to this video's source bytes.

    FFmpeg needs a seekable file, so the object is downloaded rather than
    streamed through a pipe.

    The temporary directory comes from `mkdtemp`, which guarantees a unique
    name, so two jobs - or two attempts at the same job - can never collide on
    a path. The directory and its contents are removed on exit whether the
    body raised or not.

    A legacy record with no S3 reference falls back to its local file, which
    is yielded in place and NOT deleted: it is the only copy.

    Raises:
        SourceMissing: the record points at nothing retrievable.
        SourceStoreError: the object exists but could not be downloaded.
    """
    if not has_durable_source(record):
        fallback = (
            local_fallback_path(record, upload_dir) if upload_dir else None
        )
        if fallback is None:
            raise SourceMissing(
                "The uploaded video is no longer available. Please upload it again."
            )
        logger.info(
            "Processing %s from its legacy local file; it has no durable copy",
            record.get("video_id"),
        )
        yield str(fallback)
        return

    verify_key_belongs_to(record)

    bucket = record["s3_bucket"]
    key = record["s3_key"]
    video_id = record.get("video_id") or "job"

    limit = get_settings().max_upload_bytes
    temp_dir = tempfile.mkdtemp(prefix=f"novaflow-{video_id}-")
    # Keep the extension: FFmpeg's demuxer probing is more reliable with it.
    suffix = Path(str(key)).suffix or ".mp4"
    destination = Path(temp_dir) / f"source{suffix}"

    try:
        _download_to(bucket, key, destination, limit)
        yield str(destination)
    finally:
        # Removed on success and on failure. A later attempt downloads again
        # rather than reusing a possibly truncated file.
        shutil.rmtree(temp_dir, ignore_errors=True)


def _download_to(bucket: str, key: str, destination: Path, limit: int) -> None:
    """Stream one object to disk, enforcing the size limit as it goes."""
    try:
        response = s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
        if code in _MISSING_CODES:
            logger.error("Source object is missing for a requested video")
            raise SourceMissing(
                "The uploaded video is no longer available. Please upload it again."
            ) from exc
        logger.exception("Could not download a source video")
        raise SourceStoreError(
            "The video could not be retrieved for analysis.", retryable=True
        ) from exc
    except BotoCoreError as exc:
        # Connection and read timeouts land here.
        logger.exception("Transport failure downloading a source video")
        raise SourceStoreError(
            "The video could not be retrieved for analysis.", retryable=True
        ) from exc

    body = response.get("Body")
    if body is None:
        raise SourceStoreError("The video could not be retrieved for analysis.")

    written = 0
    chunk_size = 1024 * 1024
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    # The upload route enforces the same ceiling, so this can
                    # only trigger if the object was replaced out of band.
                    # Deterministic, so NOT retryable: the same object would
                    # breach the same limit on every attempt.
                    raise SourceStoreError(
                        "The stored video is larger than this service accepts."
                    )
                handle.write(chunk)
    except SourceStoreError:
        raise
    except (BotoCoreError, ClientError, OSError) as exc:
        logger.exception("Failed while writing a downloaded source video")
        raise SourceStoreError(
            "The video could not be retrieved for analysis.", retryable=True
        ) from exc
    finally:
        try:
            body.close()
        except Exception:
            pass

    if written == 0:
        raise SourceMissing(
            "The uploaded video is empty. Please upload it again."
        )


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

@dataclass
class PlaybackStream:
    """One playback response, ready to hand to Starlette.

    `status` is 206 for a satisfied range request and 200 otherwise, and
    `headers` carries whatever S3 reported for that range. Seeking in a
    <video> element depends on both, so they are passed through rather than
    reconstructed.
    """

    chunks: object
    status: int
    headers: dict


_RANGE_RE = None


def _valid_range_header(value: str) -> bool:
    """True for a byte-range header worth forwarding to S3.

    Only the single-range `bytes=` forms a media element actually sends are
    accepted. Anything else - multiple ranges, a different unit, junk - is
    dropped and the whole object is served, which is a legal response to an
    unsatisfiable range request and avoids forwarding client text verbatim
    into an SDK call.
    """
    import re

    global _RANGE_RE
    if _RANGE_RE is None:
        _RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
    match = _RANGE_RE.match(value.strip())
    if not match:
        return False
    start, end = match.group(1), match.group(2)
    if not start and not end:
        return False
    if start and end and int(end) < int(start):
        return False
    return True


def open_stream(record: dict, range_header: str | None = None) -> PlaybackStream:
    """Open a source object for authenticated playback.

    The caller has already proven ownership; this performs no authorization of
    its own.

    A `Range` header is forwarded to S3 so the browser can seek, which is what
    `FileResponse` did for the old local files. Without this, switching to S3
    would have silently broken scrubbing in the player.
    """
    verify_key_belongs_to(record)

    bucket = record["s3_bucket"]
    key = record["s3_key"]

    kwargs = {"Bucket": bucket, "Key": key}
    if range_header and _valid_range_header(range_header):
        kwargs["Range"] = range_header.strip()

    try:
        response = s3_client().get_object(**kwargs)
    except ClientError as exc:
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
        if code in _MISSING_CODES:
            raise SourceMissing("Not found") from exc
        if code == "InvalidRange":
            # Re-fetch whole: a stale or malformed range must not fail playback.
            kwargs.pop("Range", None)
            try:
                response = s3_client().get_object(**kwargs)
            except Exception as retry_exc:
                logger.exception("Could not open a source video for playback")
                raise SourceStoreError(
                    "The video is temporarily unavailable."
                ) from retry_exc
        else:
            logger.exception("Could not open a source video for playback")
            raise SourceStoreError("The video is temporarily unavailable.") from exc
    except BotoCoreError as exc:
        logger.exception("Transport failure opening a source video for playback")
        raise SourceStoreError("The video is temporarily unavailable.") from exc

    body = response["Body"]

    headers = {
        "Accept-Ranges": "bytes",
        # Never let a stored object be interpreted as something executable on
        # this origin; the Phase 3 stored-XSS fix depends on this staying.
        "X-Content-Type-Options": "nosniff",
    }
    length = response.get("ContentLength")
    if length is not None:
        headers["Content-Length"] = str(length)

    content_range = response.get("ContentRange")
    status = 200
    if content_range:
        headers["Content-Range"] = content_range
        status = 206

    def chunks():
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass

    return PlaybackStream(chunks=chunks(), status=status, headers=headers)


__all__ = (
    "PlaybackStream",
    "SourceMissing",
    "SourceRef",
    "SourceStoreError",
    "delete_source",
    "expected_key_prefix",
    "has_durable_source",
    "local_fallback_path",
    "materialise",
    "open_stream",
    "reset_client",
    "s3_client",
    "upload_source",
    "verify_key_belongs_to",
)
