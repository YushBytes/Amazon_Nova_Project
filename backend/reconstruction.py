"""Build an output video from a source video plus a replacement image.

Phase 9. Pure media work over local file paths: nothing here reads the
database, touches S3, or knows who owns anything. `remediation_executor`
supplies the paths and persists the result.

WHY AN OVERLAY, NOT A FRAME RE-STITCH
-------------------------------------
`video_stitcher.py` (a legacy CLI, never on the request path) rebuilds a video
from the extracted 1 fps JPEGs. That is the wrong tool here and would be a
dishonest "correction": it discards the audio stream entirely, resamples 25+
fps footage down to 1 fps, and re-encodes every frame of the clip - so the
output would differ from the original everywhere, not just where the approved
finding is.

This module instead takes ONE pass over the original with FFmpeg's `overlay`
filter, enabled only between the approved finding's start and end instants:

    [0:v][1:v] overlay=x=X:y=Y:enable='between(t,START,END)'

and copies the audio stream through untouched (`acodec="copy"`). So:

* audio is bit-identical, because it is not re-encoded;
* duration, frame rate and dimensions are the source's;
* every instant outside the approved window is unchanged apart from the
  lossy video re-encode the single pass costs.

WHAT THIS IS NOT
----------------
It is NOT seamless compositing. The replacement image is scaled to the
approved region and drawn over it with hard edges. It conceals the region; it
does not inpaint, match grain, or track motion. The audit record says
`"method": "scaled_overlay"` so nothing downstream can mistake it for more
than that, and `docs/REMEDIATION.md` says the same in words.

NOTHING IS INVENTED
-------------------
Every number comes from the approved finding or from probing the real file:

* the region comes from the finding's validated bounding box, which
  `detections.BoundingBox` already checked for length, type, 0-1000 range and
  ordering, and which is re-checked here against the probed frame size;
* the time window comes from the finding's own frame timestamp;
* the pixel geometry comes from ffprobe on the actual source.

If the box is absent, the timestamp is outside the clip, or the source cannot
be probed, this REFUSES. It never guesses a region, substitutes a default
window, or falls back to covering the whole frame.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import ffmpeg

logger = logging.getLogger(__name__)

# The normalized coordinate space the prompt contract and
# `detections.BoundingBox` use.
COORD_MAX = 1000.0

# How long the overlay stays on screen, centred on the finding's timestamp.
# Frames are sampled at 1 fps, so a finding at t=3 is evidence about the
# interval around t=3 and not about a single instant. One second each side
# covers the sampling interval without reaching into a neighbouring frame's
# evidence.
DEFAULT_WINDOW_SECONDS = 1.0

# A region smaller than this many pixels on a side cannot be meaningfully
# covered, and scaling an image into it produces artefacts rather than a
# concealment. Refusing is more honest than emitting something useless.
MIN_REGION_PIXELS = 8

# Wall-clock bound for the FFmpeg pass. Separate from the extraction timeout:
# one overlay pass over a <=60s clip is fast, and a run that exceeds this is
# stuck rather than slow.
DEFAULT_TIMEOUT_SECONDS = 300

# Tolerances for the output check. A single re-encode can shift duration by a
# frame or two; anything beyond this means the pass did something unintended
# (dropped a stream, changed the rate, truncated).
DURATION_TOLERANCE_SECONDS = 1.0


class ReconstructionError(RuntimeError):
    """Reconstruction could not produce a usable output.

    `retryable` says whether the SAME inputs could plausibly succeed later.
    Set explicitly at each raise site. A malformed box, an out-of-range
    timestamp or an unreadable source is deterministic and is NOT retryable:
    retrying would spend the attempt budget on the same answer.

    Messages are shown to the video's owner, so they carry no path, bucket,
    key or FFmpeg stderr.
    """

    retryable = False

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = retryable


class UnsafeRegion(ReconstructionError):
    """The approved finding does not describe a region we can safely edit.

    Deliberately distinct from a processing failure: the inputs are the
    problem, nothing was attempted, and the caller should tell the user what
    is missing rather than retrying.
    """

    def __init__(self, *args):
        super().__init__(*args, retryable=False)


@dataclass(frozen=True)
class SourceProbe:
    """What ffprobe says about a real file."""

    width: int
    height: int
    duration_seconds: float
    has_audio: bool
    video_codec: str | None = None
    pixel_format: str | None = None


@dataclass(frozen=True)
class PixelRegion:
    """The approved region, resolved to whole pixels of the real frame."""

    x: int
    y: int
    width: int
    height: int

    def as_fields(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class ReconstructionResult:
    """A validated output file, plus the audit trail of how it was made."""

    output_path: str
    size_bytes: int
    duration_seconds: float
    has_audio: bool
    region: PixelRegion
    start_seconds: float
    end_seconds: float
    source_probe: SourceProbe
    method: str = "scaled_overlay"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_audit_fields(self) -> dict:
        """The subset persisted on the remediation record.

        No local path, no bucket, no key: this is an audit record a user may
        read, and the storage location is the server's business.
        """
        return {
            "method": self.method,
            "region_pixels": self.region.as_fields(),
            "window_seconds": [
                round(self.start_seconds, 3),
                round(self.end_seconds, 3),
            ],
            "source_width": self.source_probe.width,
            "source_height": self.source_probe.height,
            "source_duration_seconds": round(self.source_probe.duration_seconds, 3),
            "source_had_audio": self.source_probe.has_audio,
            "output_duration_seconds": round(self.duration_seconds, 3),
            "output_has_audio": self.has_audio,
            "audio_preserved": self.source_probe.has_audio == self.has_audio,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe_media(path: str) -> SourceProbe:
    """Read the real dimensions, duration and streams of a file.

    Raises:
        ReconstructionError: FFmpeg is unavailable, or the file cannot be read
            as a video. Not retryable in either case - a missing executable
            and a corrupt file both give the same answer on a second attempt.
    """
    try:
        probe = ffmpeg.probe(path)
    except FileNotFoundError as exc:
        # The ffmpeg-python wrapper is installed but the executable is not.
        raise ReconstructionError(
            "Video processing is unavailable on this server (FFmpeg not found)."
        ) from exc
    except ffmpeg.Error as exc:
        raise ReconstructionError(
            "The video could not be read. It may be corrupt or in an "
            "unsupported format."
        ) from exc
    except Exception as exc:
        raise ReconstructionError("The video could not be inspected.") from exc

    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ReconstructionError("The file contains no video stream.")

    try:
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError):
        raise ReconstructionError(
            "The video's dimensions could not be determined."
        ) from None
    if width <= 0 or height <= 0:
        raise ReconstructionError("The video reports invalid dimensions.")

    raw_duration = video.get("duration") or probe.get("format", {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        raise ReconstructionError(
            "The video's duration could not be determined."
        ) from None
    if duration <= 0:
        raise ReconstructionError("The video reports a zero or negative duration.")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    return SourceProbe(
        width=width,
        height=height,
        duration_seconds=duration,
        has_audio=has_audio,
        video_codec=video.get("codec_name"),
        pixel_format=video.get("pix_fmt"),
    )


# ---------------------------------------------------------------------------
# Resolving the approved region and window
# ---------------------------------------------------------------------------

def resolve_region(
    bounding_box, probe: SourceProbe
) -> PixelRegion:
    """Convert an approved normalized box into whole pixels of the real frame.

    The box arrives as [ymin, xmin, ymax, xmax] in the 0-1000 space -
    `backend/prompts.py`' contract, already validated by
    `detections.BoundingBox` for length, type, range and ordering. It is
    re-validated here against the PROBED frame size, because a box that is
    legal in the normalized space can still resolve to a region too small to
    edit, and because this module must never trust a caller to have checked.

    Raises:
        UnsafeRegion: the box is absent, malformed, or resolves to a region
            that cannot be safely covered.
    """
    if bounding_box is None:
        raise UnsafeRegion(
            "This finding has no region on the frame, so an automatic "
            "correction cannot be placed. The model did not return a bounding "
            "box for it."
        )
    if not isinstance(bounding_box, (list, tuple)) or len(bounding_box) != 4:
        raise UnsafeRegion(
            "This finding's region is not in a usable form, so an automatic "
            "correction cannot be placed."
        )

    numbers: list[float] = []
    for value in bounding_box:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UnsafeRegion(
                "This finding's region is not in a usable form, so an "
                "automatic correction cannot be placed."
            )
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise UnsafeRegion(
                "This finding's region is not in a usable form, so an "
                "automatic correction cannot be placed."
            )
        numbers.append(number)

    ymin, xmin, ymax, xmax = numbers
    if not all(0.0 <= n <= COORD_MAX for n in numbers):
        raise UnsafeRegion(
            "This finding's region falls outside the frame, so an automatic "
            "correction cannot be placed."
        )
    if ymin >= ymax or xmin >= xmax:
        raise UnsafeRegion(
            "This finding's region is empty or inverted, so an automatic "
            "correction cannot be placed."
        )

    # Normalized -> pixels, then clamped to the frame. Rounding outward on the
    # far edges so the region covers at least what was approved rather than
    # slightly less.
    left = int(xmin / COORD_MAX * probe.width)
    top = int(ymin / COORD_MAX * probe.height)
    right = min(probe.width, int(round(xmax / COORD_MAX * probe.width)))
    bottom = min(probe.height, int(round(ymax / COORD_MAX * probe.height)))

    width = right - left
    height = bottom - top

    if width < MIN_REGION_PIXELS or height < MIN_REGION_PIXELS:
        raise UnsafeRegion(
            f"This finding's region resolves to {width}x{height} pixels on "
            f"this video, which is too small to cover reliably."
        )

    # Even dimensions: yuv420p subsamples chroma 2x2, and an odd overlay size
    # makes FFmpeg's scaler either fail or silently shift the region.
    width -= width % 2
    height -= height % 2
    left -= left % 2
    top -= top % 2

    return PixelRegion(x=left, y=top, width=width, height=height)


def resolve_window(
    timestamp_seconds,
    probe: SourceProbe,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> tuple[float, float]:
    """The interval the overlay is enabled for, clamped to the real duration.

    Raises:
        UnsafeRegion: the finding carries no usable timestamp, or one outside
            the source's duration - which means the stored finding does not
            describe this file and nothing may be edited on the strength of it.
    """
    if timestamp_seconds is None:
        raise UnsafeRegion(
            "This finding has no timestamp, so there is no point in the video "
            "at which to apply a correction."
        )
    if isinstance(timestamp_seconds, bool) or not isinstance(
        timestamp_seconds, (int, float)
    ):
        raise UnsafeRegion("This finding's timestamp is not a usable number.")

    centre = float(timestamp_seconds)
    if centre != centre or centre in (float("inf"), float("-inf")):
        raise UnsafeRegion("This finding's timestamp is not a usable number.")
    if centre < 0:
        raise UnsafeRegion("This finding's timestamp is negative.")
    if centre > probe.duration_seconds:
        raise UnsafeRegion(
            f"This finding is timestamped at {centre:.1f}s but the video is "
            f"only {probe.duration_seconds:.1f}s long, so the stored finding "
            f"does not describe this file."
        )

    half = max(0.0, float(window_seconds))
    start = max(0.0, centre - half)
    end = min(probe.duration_seconds, centre + half)
    if end <= start:
        # A zero-length window would enable the overlay on no frame at all,
        # producing an output identical to the input that we would then be
        # analysing as if it were a correction.
        raise UnsafeRegion(
            "The correction window resolves to no time at all on this video."
        )
    return start, end


# ---------------------------------------------------------------------------
# The FFmpeg pass
# ---------------------------------------------------------------------------

def _run(stream, timeout: int) -> None:
    """Execute one ffmpeg-python graph with a hard timeout.

    `ffmpeg-python` builds an ARGUMENT LIST and `run_async` passes it to
    `subprocess.Popen` without a shell, so no value below is ever parsed as a
    command. That matters because the region and the window are derived from
    model output.

    Raises:
        ReconstructionError: the run failed, timed out, or FFmpeg is missing.
    """
    process = None
    try:
        process = stream.run_async(pipe_stdout=True, pipe_stderr=True)
        try:
            _, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ReconstructionError(
                "Building the corrected video took too long and was stopped.",
                # A timeout can be load; the same inputs may succeed later.
                retryable=True,
            ) from None

        if process.returncode != 0:
            # stderr is logged for an operator and NEVER returned: it contains
            # the full command line, which includes local paths.
            logger.error(
                "FFmpeg exited %s during reconstruction: %s",
                process.returncode,
                (stderr or b"")[-2000:].decode("utf-8", "replace"),
            )
            raise ReconstructionError(
                "The corrected video could not be produced from this source."
            )
    except FileNotFoundError as exc:
        raise ReconstructionError(
            "Video processing is unavailable on this server (FFmpeg not found)."
        ) from exc
    except ReconstructionError:
        raise
    except Exception as exc:
        logger.exception("Reconstruction subprocess failed")
        raise ReconstructionError(
            "The corrected video could not be produced.", retryable=True
        ) from exc
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.communicate()
            except Exception:
                logger.warning("Could not cleanly stop the FFmpeg subprocess")


def reconstruct(
    *,
    source_path: str,
    asset_path: str,
    bounding_box,
    timestamp_seconds,
    output_path: str,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ReconstructionResult:
    """Produce a new video with the asset drawn over the approved region.

    The source file is opened read-only and never written: `output_path` must
    be a different file, which this asserts rather than trusts.

    Returns the validated result. Raises rather than returning a partial
    output, and removes a partial file on failure.

    Raises:
        UnsafeRegion: the finding does not describe an editable region.
        ReconstructionError: the pass failed, or its output did not validate.
    """
    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        # Would truncate the input mid-read. The original must never be
        # written to, and this is the last line of defence for that.
        raise ReconstructionError(
            "Refusing to write the corrected video over its own source."
        )

    probe = probe_media(str(source))
    region = resolve_region(bounding_box, probe)
    start, end = resolve_window(timestamp_seconds, probe, window_seconds)

    warnings: list[str] = []

    source_input = ffmpeg.input(str(source))
    # `loop=1` turns the still image into a video stream so the overlay has
    # something to draw for the whole window; `t` bounds it so the loop cannot
    # extend the output's duration.
    asset_input = ffmpeg.input(
        str(Path(asset_path).resolve()), loop=1, t=max(end - start, 0.1)
    )

    scaled = asset_input.filter("scale", region.width, region.height)

    video = ffmpeg.overlay(
        source_input.video,
        scaled,
        x=region.x,
        y=region.y,
        # Enabled only inside the approved window. Every other instant is the
        # source's own frame.
        enable=f"between(t,{start:.3f},{end:.3f})",
    )

    output_kwargs = {
        "vcodec": "libx264",
        "pix_fmt": "yuv420p",
        # Match the source's duration exactly rather than letting the looped
        # image extend it.
        "t": probe.duration_seconds,
        "movflags": "+faststart",
    }

    if probe.has_audio:
        # Copied, not re-encoded: the audio is then bit-identical, which is
        # the strongest "audio preserved" guarantee available.
        output_kwargs["acodec"] = "copy"
        stream = ffmpeg.output(
            video, source_input.audio, str(output), **output_kwargs
        )
    else:
        warnings.append("The source video has no audio stream.")
        stream = ffmpeg.output(video, str(output), **output_kwargs)

    stream = stream.overwrite_output()

    logger.info(
        "Reconstructing: %dx%d region at (%d,%d) over %.2f-%.2fs of a %.1fs clip",
        region.width,
        region.height,
        region.x,
        region.y,
        start,
        end,
        probe.duration_seconds,
    )

    try:
        _run(stream, timeout_seconds)
        result_probe = validate_output(
            str(output), probe, expect_audio=probe.has_audio
        )
    except Exception:
        # Never leave a partial file behind for a caller to upload and then
        # analyse as if it were a correction.
        try:
            if output.exists():
                output.unlink()
        except OSError:
            logger.warning("Could not remove a partial reconstruction output")
        raise

    if probe.has_audio and not result_probe.has_audio:
        # validate_output already raises for this; belt and braces in case the
        # expectation is ever relaxed.
        warnings.append("Audio was not preserved in the output.")

    return ReconstructionResult(
        output_path=str(output),
        size_bytes=output.stat().st_size,
        duration_seconds=result_probe.duration_seconds,
        has_audio=result_probe.has_audio,
        region=region,
        start_seconds=start,
        end_seconds=end,
        source_probe=probe,
        warnings=tuple(warnings),
    )


def validate_output(
    output_path: str, source: SourceProbe, *, expect_audio: bool
) -> SourceProbe:
    """Check a reconstructed file before anything downstream trusts it.

    This is the gate between "FFmpeg exited 0" and "we have an output worth
    analysing". A successful exit code is not evidence of a usable video.

    Raises:
        ReconstructionError: the file is missing, empty, unprobeable, has no
            video stream, lost expected audio, or drifted in duration or
            dimensions.
    """
    path = Path(output_path)
    if not path.exists():
        raise ReconstructionError("The corrected video was not produced.")
    if path.stat().st_size == 0:
        raise ReconstructionError("The corrected video came out empty.")

    # Probing is what proves there is a real, readable video stream in there.
    result = probe_media(str(path))

    if expect_audio and not result.has_audio:
        raise ReconstructionError(
            "The corrected video lost the original's audio, so it was not kept."
        )

    if abs(result.duration_seconds - source.duration_seconds) > (
        DURATION_TOLERANCE_SECONDS
    ):
        raise ReconstructionError(
            "The corrected video's length does not match the original, so it "
            "was not kept."
        )

    if result.width != source.width or result.height != source.height:
        raise ReconstructionError(
            "The corrected video's dimensions do not match the original, so "
            "it was not kept."
        )

    return result


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

class Workspace:
    """A unique temporary directory, removed on every exit path.

    `mkdtemp` guarantees a unique name, so two reconstructions - or two
    attempts at one - can never collide on a path. Used as a context manager
    so a raised FFmpeg error cannot leak the staged source, the asset, or a
    partial output onto the worker's disk.
    """

    def __init__(self, prefix: str = "novaflow-rebuild-") -> None:
        self._prefix = prefix
        self.path: str | None = None

    def __enter__(self) -> "Workspace":
        self.path = tempfile.mkdtemp(prefix=self._prefix)
        return self

    def __exit__(self, *_exc) -> None:
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None

    def file(self, name: str) -> str:
        if not self.path:
            raise RuntimeError("workspace is not open")
        return os.path.join(self.path, name)


__all__ = (
    "COORD_MAX",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "DURATION_TOLERANCE_SECONDS",
    "MIN_REGION_PIXELS",
    "PixelRegion",
    "ReconstructionError",
    "ReconstructionResult",
    "SourceProbe",
    "UnsafeRegion",
    "Workspace",
    "probe_media",
    "reconstruct",
    "resolve_region",
    "resolve_window",
    "validate_output",
)
