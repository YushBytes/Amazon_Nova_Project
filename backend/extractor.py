"""Frame extraction stage.

Phase 4 rewrite of the extraction contract. Previously `extract_and_store`
returned `None` on every failure path — no video stream, duration over the
limit, a probe error — so the caller could not tell success from failure and a
job stayed "pending" forever. It now returns a result on success and raises
`ExtractionError` on every failure, so `pipeline` can record a truthful
terminal state.

Also added: an explicit frame cap, an FFmpeg timeout, real logging instead of
`print`, and cleanup of the subprocess on every path.

What is unchanged: frames still go to S3 under `frames/{video_id}/`, their
metadata still goes to the DynamoDB frames table keyed on
(`video_id`, `frame_number`), and the default sampling rate is still 1 fps so
results stay comparable with earlier runs.

SAMPLING POLICY AND ITS LIMITS

Frames are sampled at a fixed `FRAME_SAMPLE_FPS` (default 1) up to
`MAX_FRAMES_PER_VIDEO` (default 120), for clips no longer than
`MAX_VIDEO_DURATION_SECONDS` (default 60). Consequences worth stating plainly:

* A violation visible for less than one sampling interval can be missed
  entirely. At 1 fps this is anything under ~1 second.
* Each frame's timestamp is `frame_number / fps`, which is the *sampling*
  time, not a container-reported presentation timestamp. It is accurate for
  constant-rate sampling and is what the UI uses to seek.
* Nothing here does scene detection or keyframe selection; sampling is
  uniform.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
import ffmpeg
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

import aws_resources
from config import get_settings

load_dotenv()

logger = logging.getLogger(__name__)

# Resource names come from aws_resources (Phase 4.1), the single place they
# are defined. The module-level names are kept for the tests and tooling that
# refer to them.
S3_BUCKET = aws_resources.media_bucket()
DYNAMO_TABLE = aws_resources.frames_table_name()
AWS_REGION = aws_resources.aws_region()

# Initialize AWS clients
s3_client = boto3.client("s3", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMO_TABLE)

# Retained for backwards compatibility with anything importing these names.
# The authoritative values come from config so they stay configurable.
FPS = 1
MAX_DURATION = 60

# JPEG start-of-image and end-of-image markers, used to split the MJPEG
# stream FFmpeg writes to the pipe.
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

STATUS_PENDING = "pending_audit"


class ExtractionError(RuntimeError):
    """Frame extraction failed.

    The message is a safe, operator-readable summary with no credentials,
    bucket names, or filesystem paths, so it can be stored as a job's
    `error_summary` and shown to the owner.
    """


@dataclass
class ExtractionResult:
    """What extraction actually produced."""

    video_id: str
    frames_stored: int
    duration_seconds: float
    fps: int
    truncated: bool = False
    frame_timestamps: list[float] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"{self.frames_stored} frame(s) at {self.fps} fps"
        if self.truncated:
            text += " (truncated at the frame cap)"
        return text


def get_video_duration(file_path: str) -> float:
    """Probe the clip's duration in seconds.

    Raises:
        ExtractionError: if the file cannot be probed or has no video stream.
    """
    try:
        probe = ffmpeg.probe(file_path)
    except FileNotFoundError as exc:
        # The ffmpeg-python wrapper is installed but the executable is not.
        raise ExtractionError(
            "Video processing is unavailable on this server (FFmpeg not found)."
        ) from exc
    except ffmpeg.Error as exc:
        raise ExtractionError(
            "The file could not be read as a video. It may be corrupt or in an "
            "unsupported format."
        ) from exc
    except Exception as exc:
        raise ExtractionError("The video could not be inspected.") from exc

    streams = probe.get("streams") or []
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    if not video_stream:
        raise ExtractionError("The file contains no video stream.")

    raw = video_stream.get("duration") or probe.get("format", {}).get("duration")
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        raise ExtractionError(
            "The video's duration could not be determined."
        ) from None

    if duration <= 0:
        raise ExtractionError("The video reports a zero or negative duration.")

    return duration


def extract_and_store(file_path: str, video_id: str | None = None) -> ExtractionResult:
    """Extract frames, store them in S3, and register metadata in DynamoDB.

    Returns:
        ExtractionResult describing what was stored.

    Raises:
        ExtractionError: on any failure. Never returns silently, because the
            caller uses the outcome to decide a job's terminal state.
    """
    settings = get_settings()
    fps = settings.frame_sample_fps
    max_frames = settings.max_frames_per_video
    max_duration = settings.max_video_duration_seconds
    timeout = settings.ffmpeg_timeout_seconds

    if not video_id:
        video_id = f"video_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    duration = get_video_duration(file_path)

    if duration > max_duration:
        # A real, reported failure rather than the previous silent return.
        raise ExtractionError(
            f"The video is {duration:.1f}s long; the limit for analysis is "
            f"{max_duration}s."
        )

    logger.info(
        "Extracting frames for %s: %.1fs at %d fps (cap %d)",
        video_id, duration, fps, max_frames,
    )

    process = None
    frame_count = 0
    truncated = False
    timestamps: list[float] = []

    try:
        process = (
            ffmpeg.input(file_path)
            .filter("fps", fps=fps)
            .output("pipe:", format="image2pipe", vcodec="mjpeg", qscale=2)
            .run_async(pipe_stdout=True, pipe_stderr=True)
        )

        buffer = bytearray()
        chunk_size = 1024 * 1024
        # Bound the buffer: a stream that never yields an EOI marker must not
        # grow without limit. 64 MiB is far beyond any single sampled frame.
        max_buffer = 64 * 1024 * 1024

        while True:
            chunk = process.stdout.read(chunk_size)
            if not chunk:
                if process.poll() is not None:
                    break
                continue

            buffer.extend(chunk)
            if len(buffer) > max_buffer:
                raise ExtractionError(
                    "The video produced an unreadable frame stream."
                )

            while True:
                start = buffer.find(_SOI)
                if start == -1:
                    break
                if start > 0:
                    del buffer[:start]
                end = buffer.find(_EOI)
                if end == -1:
                    break

                frame_data = bytes(buffer[: end + 2])
                del buffer[: end + 2]

                if frame_count >= max_frames:
                    truncated = True
                    break

                timestamp = round(frame_count / fps, 3)
                _store_frame(video_id, frame_count, timestamp, frame_data)
                timestamps.append(timestamp)
                frame_count += 1

            if truncated:
                break

        # Reap the process and surface a non-zero exit.
        try:
            returncode = process.wait(timeout=timeout)
        except Exception as exc:
            raise ExtractionError(
                "Video processing took too long and was stopped."
            ) from exc

        if returncode != 0 and frame_count == 0:
            # Log stderr for operators; never return it to a client.
            stderr = b""
            try:
                stderr = process.stderr.read() or b""
            except Exception:
                pass
            logger.error(
                "FFmpeg exited %s for %s: %s",
                returncode, video_id, stderr.decode(errors="replace")[:500],
            )
            raise ExtractionError("The video could not be decoded.")

    except ExtractionError:
        raise
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Frame storage failed for %s", video_id)
        raise ExtractionError(
            "Frame storage is currently unavailable."
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected extraction failure for %s", video_id)
        raise ExtractionError("Frame extraction failed.") from exc
    finally:
        # Always release the subprocess and its pipes, on success or failure.
        _terminate(process)

    if frame_count == 0:
        raise ExtractionError(
            "No frames could be extracted from the video."
        )

    logger.info("Extraction complete for %s: %d frame(s)", video_id, frame_count)
    return ExtractionResult(
        video_id=video_id,
        frames_stored=frame_count,
        duration_seconds=duration,
        fps=fps,
        truncated=truncated,
        frame_timestamps=timestamps,
    )


def _store_frame(
    video_id: str, frame_number: int, timestamp: float, frame_data: bytes
) -> None:
    """Put one frame in S3 and its metadata in DynamoDB.

    The S3 key is entirely server-generated. A failure here aborts extraction
    rather than being swallowed: the previous implementation logged and
    continued without incrementing the counter, which silently reused the same
    key and frame number for the next frame and corrupted the sequence.
    """
    s3_key = aws_resources.frame_key(video_id, frame_number)

    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=frame_data,
        ContentType="image/jpeg",
    )

    table.put_item(
        Item={
            "video_id": video_id,
            "frame_number": frame_number,
            # Stored as a string for backwards compatibility with existing
            # rows; readers coerce with float().
            "timestamp": str(timestamp),
            "s3_key": s3_key,
            "s3_bucket": S3_BUCKET,
            "status": STATUS_PENDING,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _terminate(process) -> None:
    """Close pipes and stop the subprocess if it is still running."""
    if process is None:
        return
    for stream in ("stdout", "stderr", "stdin"):
        handle = getattr(process, stream, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    except Exception:
        logger.warning("Could not cleanly stop the FFmpeg subprocess")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python extractor.py path/to/video.mp4 [video_id]")
        sys.exit(1)

    path = sys.argv[1]
    vid = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        result = extract_and_store(path, vid)
    except ExtractionError as error:
        print(f"Extraction failed: {error}")
        sys.exit(1)
    print(f"Extraction complete: {result.summary}")
