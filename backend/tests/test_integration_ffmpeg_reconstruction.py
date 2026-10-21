"""Phase 9: reconstruction against the REAL FFmpeg binary.

SKIPPED WHEN FFMPEG IS ABSENT, and it is absent on the development host these
tests were written on - `which ffmpeg` found nothing. So everything in this
file is unverified there, and a skipped test is not a passing one. The
subprocess-boundary tests in `test_reconstruction.py` cover the filter graph
and the guards without the binary; these cover what only the binary can show:

* that the graph FFmpeg is actually handed produces a playable file;
* that the audio stream survives a real `-acodec copy`;
* that the duration and dimensions really are preserved;
* that the source file really is left byte-identical;
* that a genuinely unreadable input fails rather than producing something.

Each test generates its own tiny clip with FFmpeg's own synthetic sources
(`testsrc` for video, `sine` for audio), so nothing depends on a fixture file
being committed and nothing touches a user's media.

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


def test_frames_outside_the_window_are_left_alone(
    tmp_path, source_with_audio, replacement
):
    """The overlay is enabled only inside the approved interval.

    Extracts one frame from well before the window and one from inside it, and
    asserts only the second changed relative to the source's own frames.
    """
    output = tmp_path / "out.mp4"
    reconstruct(
        source_path=str(source_with_audio),
        asset_path=str(replacement),
        bounding_box=[250, 250, 750, 750],
        timestamp_seconds=3.0,
        window_seconds=0.5,
        output_path=str(output),
    )

    def frame_at(video: Path, seconds: float, name: str) -> bytes:
        target = tmp_path / name
        _run(
            [
                "ffmpeg", "-y", "-ss", str(seconds), "-i", str(video),
                "-frames:v", "1", str(target),
            ]
        )
        return target.read_bytes()

    # t=0.5 is far outside the 2.5-3.5s window.
    assert frame_at(output, 0.5, "out-early.png") == frame_at(
        source_with_audio, 0.5, "src-early.png"
    )
    # t=3.0 is the centre of it.
    assert frame_at(output, 3.0, "out-inside.png") != frame_at(
        source_with_audio, 3.0, "src-inside.png"
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
