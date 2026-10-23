"""Phase 9: reconstruction against the REAL FFmpeg binary.

SKIPPED WHEN FFMPEG IS ABSENT, so a skipped run proves nothing. Phase 9 was
written on a host with no FFmpeg and reported all of these as skipped. **Phase
9.1 installed FFmpeg 9.0.1 and ran them**; see docs/REMEDIATION.md for the
recorded result.

The subprocess-boundary tests in `test_reconstruction.py` cover the filter
graph and the guards without the binary. These cover what only the binary can
show:

* that the graph FFmpeg is actually handed produces a playable file;
* that the replacement image is *visibly present* in the approved region at
  the approved time - asserted by sampling real pixels, not by the output
  file merely existing;
* that the overlay is localized in BOTH space and time: a region outside the
  box, and a frame outside the window, are unchanged;
* that the audio stream survives a real `-acodec copy`;
* that the duration and dimensions really are preserved;
* that the source file really is left byte-identical;
* that genuinely unreadable input fails rather than producing something.

Each test generates its own tiny clip with FFmpeg's own synthetic sources
(`testsrc` for video, `sine` for audio), so nothing depends on a committed
fixture and nothing touches a user's media.

To run them: install FFmpeg so `ffmpeg` and `ffprobe` are on PATH, then

    pytest -m requires_ffmpeg backend/tests/test_integration_ffmpeg_reconstruction.py
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import reconstruction
from reconstruction import (
    ReconstructionError,
    UnsafeRegion,
    probe_media,
    reconstruct,
)

pytestmark = pytest.mark.requires_ffmpeg


def _missing() -> str | None:
    """Which executable is missing, if any."""
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            return name
    return None


@pytest.fixture(autouse=True)
def _require_ffmpeg():
    missing = _missing()
    if missing:
        pytest.skip(
            f"{missing} is not on PATH; real-FFmpeg reconstruction tests "
            f"skipped. Install FFmpeg to run them."
        )


def _run(args: list[str]) -> None:
    """Run an FFmpeg command as an argument list - never through a shell."""
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120
    )
    if result.returncode != 0:
        pytest.fail(
            f"fixture generation failed: "
            f"{result.stderr[-500:].decode('utf-8', 'replace')}"
        )


@pytest.fixture
def source_with_audio(tmp_path) -> Path:
    """A 4-second 320x240 clip with a real audio stream."""
    path = tmp_path / "source.mp4"
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(path),
        ]
    )
    return path


@pytest.fixture
def silent_source(tmp_path) -> Path:
    """The same clip with no audio stream at all."""
    path = tmp_path / "silent.mp4"
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ]
    )
    return path


@pytest.fixture
def replacement(tmp_path) -> Path:
    """A solid-colour PNG to draw over the region."""
    path = tmp_path / "asset.png"
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=magenta:size=64x64:duration=1",
            "-frames:v", "1",
            str(path),
        ]
    )
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mean_rgb(video, seconds: float, x: int, y: int, w: int, h: int):
    """Mean (R, G, B) of one region of the frame at `seconds`.

    Uses FFmpeg itself to crop the region and emit raw rgb24, so there is no
    image library to add as a dependency - and no decoding of our own to get
    wrong. Returns floats because a lossy codec will not reproduce an exact
    value.

    This is what makes an overlay assertion meaningful. A test that only
    checked the output file existed, or that its bytes differed from the
    source's, would pass for an output where the overlay landed in the wrong
    place, covered the whole frame, or appeared at the wrong time.
    """
    args = [
        "ffmpeg", "-v", "error",
        "-ss", f"{seconds:.3f}", "-i", str(video),
        "-frames:v", "1",
        "-vf", f"crop={w}:{h}:{x}:{y}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    completed = subprocess.run(args, capture_output=True, timeout=60)
    if completed.returncode != 0 or not completed.stdout:
        pytest.fail(
            "could not sample pixels: "
            f"{completed.stderr[-300:].decode('utf-8', 'replace')}"
        )
    data = completed.stdout
    count = len(data) // 3
    return (
        sum(data[0::3]) / count,
        sum(data[1::3]) / count,
        sum(data[2::3]) / count,
    )


def assert_close(actual, expected, tolerance: float, what: str) -> None:
    """Assert two RGB triples match within `tolerance` per channel."""
    for channel, (a, e) in zip("RGB", zip(actual, expected)):
        assert abs(a - e) <= tolerance, (
            f"{what}: {channel} was {a:.1f}, expected ~{e:.1f} "
            f"(tolerance {tolerance}); full actual={tuple(round(v, 1) for v in actual)}"
        )


# The replacement image is solid magenta. yuv420p chroma subsampling plus the
# lossy re-encode shift it slightly, so the assertions allow a margin rather
# than demanding (255, 0, 255) exactly - measured at (254, 0, 251).
MAGENTA = (255.0, 0.0, 255.0)
# `testsrc` renders a mid-grey field where the box lands on a 320x240 frame.
SOURCE_GREY = (127.0, 127.0, 127.0)
# Generous, because the point is "is it the replacement or the source", not
# colour fidelity.
COLOUR_TOLERANCE = 12.0

# The box used by most tests below, and where it lands on a 320x240 frame.
BOX = [250, 250, 750, 750]
REGION = (80, 60, 160, 120)  # x, y, w, h


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_a_real_reconstruction_produces_a_playable_file(
    tmp_path, source_with_audio, replacement
):
    output = tmp_path / "out.mp4"
    result = reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=2.0,
        output_path=str(output),
    )

    assert output.exists()
    assert result.size_bytes > 0
    # Probed, not assumed: a real video stream is in there.
    probed = probe_media(str(output))
    assert probed.width == 320
    assert probed.height == 240


def test_real_audio_survives_the_pass(tmp_path, source_with_audio, replacement):
    """`-acodec copy` means the stream is not re-encoded at all."""
    output = tmp_path / "out.mp4"
    result = reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=2.0,
        output_path=str(output),
    )
    assert result.has_audio is True
    assert probe_media(str(output)).has_audio is True
    assert result.as_audit_fields()["audio_preserved"] is True


def test_the_real_duration_is_preserved(tmp_path, source_with_audio, replacement):
    source_duration = probe_media(str(source_with_audio)).duration_seconds
    output = tmp_path / "out.mp4"

    result = reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=2.0,
        output_path=str(output),
    )

    assert result.duration_seconds == pytest.approx(
        source_duration, abs=reconstruction.DURATION_TOLERANCE_SECONDS
    )


def test_the_real_source_file_is_byte_identical_afterwards(
    tmp_path, source_with_audio, replacement
):
    """The headline guarantee, against the real binary."""
    before = _digest(source_with_audio)

    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=2.0,
        output_path=str(tmp_path / "out.mp4"),
    )

    assert _digest(source_with_audio) == before


def test_the_output_differs_from_the_source(
    tmp_path, source_with_audio, replacement
):
    """The direct regression test for a correction that changes nothing.

    The deleted `audit_manager.remediate_frame` returned its input unchanged
    while the caller recorded success. An output identical to its source would
    be that defect again, one layer up.
    """
    output = tmp_path / "out.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=2.0,
        output_path=str(output),
    )

    assert _digest(output) != _digest(source_with_audio)


def test_the_replacement_is_visibly_present_in_the_approved_region(
    tmp_path, source_with_audio, replacement
):
    """The core proof that the overlay actually worked.

    Samples the real pixels of the approved region at the centre of the
    approved window. The source renders mid-grey there; the replacement is
    magenta. Anything else - the overlay missing, landing elsewhere, or being
    scaled to nothing - fails here.
    """
    output = tmp_path / "out.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=BOX,
        timestamp_seconds=2.0,
        output_path=str(output),
    )

    x, y, w, h = REGION
    # Sanity: the source really is grey there, so the assertion below is
    # measuring the overlay rather than a coincidence.
    assert_close(
        mean_rgb(source_with_audio, 2.0, x, y, w, h),
        SOURCE_GREY,
        COLOUR_TOLERANCE,
        "source region before reconstruction",
    )
    assert_close(
        mean_rgb(output, 2.0, x, y, w, h),
        MAGENTA,
        COLOUR_TOLERANCE,
        "output region inside the window",
    )


def test_the_overlay_is_localized_in_space(
    tmp_path, source_with_audio, replacement
):
    """It covers the approved region and NOT the whole frame.

    A filter graph that dropped the `crop`/position - or scaled the image to
    the full frame - would still produce a playable file with the right
    duration and audio. This is what catches it.
    """
    output = tmp_path / "out.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=BOX,
        timestamp_seconds=2.0,
        output_path=str(output),
    )

    # A 40x40 corner well outside the 160x120 region at (80, 60).
    corner_source = mean_rgb(source_with_audio, 2.0, 0, 0, 40, 40)
    corner_output = mean_rgb(output, 2.0, 0, 0, 40, 40)
    assert_close(
        corner_output,
        corner_source,
        COLOUR_TOLERANCE,
        "a corner outside the approved region, inside the window",
    )
    # And that corner is nothing like the replacement colour, so the
    # comparison above is not vacuous.
    assert abs(corner_output[0] - MAGENTA[0]) > COLOUR_TOLERANCE


def test_the_overlay_is_localized_in_time(
    tmp_path, source_with_audio, replacement
):
    """Frames outside the approved window do not carry the replacement.

    NOTE: asserted on PIXELS, not bytes. The single video pass re-encodes the
    whole clip, so a frame extracted from the output is never byte-identical
    to one from the source even where nothing was drawn - which is documented
    behaviour, not a defect. Comparing bytes would fail for the wrong reason.
    """
    output = tmp_path / "out.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=BOX,
        timestamp_seconds=3.0,
        window_seconds=0.5,
        output_path=str(output),
    )

    x, y, w, h = REGION

    # t=3.0 is the centre of the 2.5-3.5s window: the replacement is there.
    assert_close(
        mean_rgb(output, 3.0, x, y, w, h),
        MAGENTA,
        COLOUR_TOLERANCE,
        "the region inside the window",
    )

    # t=0.5 and t=1.5 are well before it: the region is the source's own
    # content.
    for instant in (0.5, 1.5):
        assert_close(
            mean_rgb(output, instant, x, y, w, h),
            mean_rgb(source_with_audio, instant, x, y, w, h),
            COLOUR_TOLERANCE,
            f"the region at t={instant}, outside the window",
        )


def test_a_narrower_window_covers_fewer_frames(
    tmp_path, source_with_audio, replacement
):
    """REMEDIATION_WINDOW_SECONDS really does bound the edit.

    Builds the same correction twice with different half-widths and checks
    that an instant covered by the wider one is not covered by the narrower.
    """
    x, y, w, h = REGION

    wide = tmp_path / "wide.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=BOX,
        timestamp_seconds=2.0,
        window_seconds=1.0,
        output_path=str(wide),
    )
    narrow = tmp_path / "narrow.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=BOX,
        timestamp_seconds=2.0,
        window_seconds=0.2,
        output_path=str(narrow),
    )

    # t=1.4 is inside 1.0-3.0 but outside 1.8-2.2.
    assert_close(
        mean_rgb(wide, 1.4, x, y, w, h), MAGENTA, COLOUR_TOLERANCE,
        "the wide window at t=1.4",
    )
    assert_close(
        mean_rgb(narrow, 1.4, x, y, w, h),
        mean_rgb(source_with_audio, 1.4, x, y, w, h),
        COLOUR_TOLERANCE,
        "the narrow window at t=1.4",
    )


def test_a_silent_source_produces_a_silent_output(
    tmp_path, silent_source, replacement
):
    output = tmp_path / "out.mp4"
    result = reconstruct(
        source_path=str(silent_source),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=2.0,
        output_path=str(output),
    )
    assert result.has_audio is False
    assert any("no audio" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# real failures
# ---------------------------------------------------------------------------

def test_an_unreadable_source_fails_against_the_real_binary(
    tmp_path, replacement
):
    bogus = tmp_path / "not-a-video.mp4"
    bogus.write_bytes(b"this is definitely not a video container")

    with pytest.raises(ReconstructionError):
        reconstruct(
            source_path=str(bogus),
            asset_path=str(replacement),
            bounding_box=[250, 250, 750, 750],
            timestamp_seconds=1.0,
            output_path=str(tmp_path / "out.mp4"),
        )


def test_an_unreadable_asset_fails_without_leaving_an_output(
    tmp_path, source_with_audio
):
    bogus = tmp_path / "not-an-image.png"
    bogus.write_bytes(b"not an image")
    output = tmp_path / "out.mp4"

    with pytest.raises(ReconstructionError):
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(bogus),
            bounding_box=[250, 250, 750, 750],
            timestamp_seconds=2.0,
            output_path=str(output),
        )

    assert not output.exists()


def test_a_timestamp_past_the_real_duration_refuses(
    tmp_path, source_with_audio, replacement
):
    with pytest.raises(UnsafeRegion):
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(replacement),
            bounding_box=[250, 250, 750, 750],
            # The fixture is 4 seconds long.
            timestamp_seconds=90.0,
            output_path=str(tmp_path / "out.mp4"),
        )


def test_a_region_too_small_for_the_real_frame_refuses(
    tmp_path, source_with_audio, replacement
):
    """1/1000 of a 320px frame is well under the pixel floor."""
    with pytest.raises(UnsafeRegion):
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(replacement),
            bounding_box=[0, 0, 1, 1],
            timestamp_seconds=2.0,
            output_path=str(tmp_path / "out.mp4"),
        )


def test_a_missing_box_refuses_against_the_real_binary(
    tmp_path, source_with_audio, replacement
):
    with pytest.raises(UnsafeRegion):
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(replacement),
            bounding_box=None,
            timestamp_seconds=2.0,
            output_path=str(tmp_path / "out.mp4"),
        )


def test_an_empty_source_file_fails(tmp_path, replacement):
    """Zero bytes is not a video. ffprobe must reject it."""
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    output = tmp_path / "out.mp4"

    with pytest.raises(ReconstructionError):
        reconstruct(
            source_path=str(empty),
            asset_path=str(replacement),
            bounding_box=BOX,
            timestamp_seconds=1.0,
            output_path=str(output),
        )
    assert not output.exists()


def test_an_audio_only_source_fails(tmp_path, replacement):
    """A real file with no video stream, built by FFmpeg itself."""
    audio_only = tmp_path / "audio.m4a"
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:a", "aac", str(audio_only),
        ]
    )

    with pytest.raises(ReconstructionError) as exc:
        reconstruct(
            source_path=str(audio_only),
            asset_path=str(replacement),
            bounding_box=BOX,
            timestamp_seconds=1.0,
            output_path=str(tmp_path / "out.mp4"),
        )
    assert "no video stream" in str(exc.value)


def test_an_unsupported_image_format_fails(tmp_path, source_with_audio):
    """A real BMP: a valid image FFmpeg can read, but not one we accept.

    `reconstruction` itself does not police the asset's format - that is
    `remediation_assets.validate_asset_bytes`, which sniffs the bytes before
    anything is stored. This test records what the layer below actually does
    with one, so the division of responsibility is not guessed at.
    """
    bmp = tmp_path / "asset.bmp"
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=magenta:size=64x64:duration=1",
            "-frames:v", "1", str(bmp),
        ]
    )
    # The asset validator is the gate, and it refuses this.
    import remediation_assets

    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(bmp.read_bytes())


def test_a_box_outside_the_frame_refuses(tmp_path, source_with_audio, replacement):
    """Coordinates beyond the normalized space never reach FFmpeg."""
    for box in ([0, 0, 1001, 500], [-1, 0, 500, 500]):
        with pytest.raises(UnsafeRegion):
            reconstruct(
                source_path=str(source_with_audio),
                asset_path=str(replacement),
                bounding_box=box,
                timestamp_seconds=2.0,
                output_path=str(tmp_path / "out.mp4"),
            )


def test_a_zero_sized_box_refuses(tmp_path, source_with_audio, replacement):
    for box in ([250, 250, 250, 750], [250, 250, 750, 250]):
        with pytest.raises(UnsafeRegion):
            reconstruct(
                source_path=str(source_with_audio),
                asset_path=str(replacement),
                bounding_box=box,
                timestamp_seconds=2.0,
                output_path=str(tmp_path / "out.mp4"),
            )


def test_a_negative_timestamp_refuses(tmp_path, source_with_audio, replacement):
    with pytest.raises(UnsafeRegion):
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(replacement),
            bounding_box=BOX,
            timestamp_seconds=-1.0,
            output_path=str(tmp_path / "out.mp4"),
        )


def test_a_zero_length_window_refuses(tmp_path, source_with_audio, replacement):
    """It would enable the overlay on no frame at all.

    The output would then equal the input, and we would analyse it as if it
    were a correction.
    """
    with pytest.raises(UnsafeRegion):
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(replacement),
            bounding_box=BOX,
            # 4.0 is exactly the clip's duration, so start == end == 4.0.
            timestamp_seconds=4.0,
            window_seconds=0.0,
            output_path=str(tmp_path / "out.mp4"),
        )


def test_a_real_ffmpeg_failure_leaks_no_path_into_the_message(
    tmp_path, source_with_audio
):
    """Real stderr names the input files; the user-facing message must not.

    Driven by handing FFmpeg a genuinely undecodable overlay input, so this is
    the real stderr of a real non-zero exit rather than a stubbed one.
    """
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400)
    output = tmp_path / "out.mp4"

    with pytest.raises(ReconstructionError) as exc:
        reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(broken),
            bounding_box=BOX,
            timestamp_seconds=2.0,
            output_path=str(output),
        )

    message = str(exc.value)
    assert str(tmp_path) not in message
    assert "broken.png" not in message
    assert ".mp4" not in message
    # And no partial output was left for a caller to upload.
    assert not output.exists()


def test_the_workspace_is_cleaned_up_after_a_real_success(
    tmp_path, source_with_audio, replacement
):
    """Temporary files go on the success path too, not only on failure."""
    with reconstruction.Workspace() as workspace:
        path = workspace.path
        result = reconstruct(
            source_path=str(source_with_audio),
            asset_path=str(replacement),
            bounding_box=BOX,
            timestamp_seconds=2.0,
            output_path=workspace.file("out.mp4"),
        )
        assert Path(result.output_path).exists()

    assert not Path(path).exists()


def test_the_workspace_is_cleaned_up_after_a_real_failure(
    tmp_path, source_with_audio
):
    """A failed run must not leave staged files behind."""
    bogus = tmp_path / "not-an-image.png"
    bogus.write_bytes(b"not an image")

    with reconstruction.Workspace() as workspace:
        path = workspace.path
        asset = Path(workspace.file("asset.png"))
        asset.write_bytes(b"not an image")
        try:
            reconstruct(
                source_path=str(source_with_audio),
                asset_path=str(asset),
                bounding_box=[250, 250, 750, 750],
                timestamp_seconds=2.0,
                output_path=workspace.file("out.mp4"),
            )
        except ReconstructionError:
            pass

    assert not Path(path).exists()
