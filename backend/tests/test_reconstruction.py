"""Phase 9: resolving an approved finding into a safe edit, and validating it.

FFMPEG IS NOT INVOKED HERE. Two kinds of test live in this file:

* the geometry and the guards - `resolve_region`, `resolve_window`,
  `validate_output` - which are pure functions over a probe result and are
  tested directly;
* the FFmpeg pass itself, tested at the SUBPROCESS BOUNDARY with `ffmpeg.probe`
  and `run_async` stubbed, so the filter graph, the audio handling and the
  cleanup are all asserted without needing the executable.

`test_integration_ffmpeg_reconstruction.py` holds the tests that need the real
binary, and they are SKIPPED when it is absent. Nothing in this file proves the
real FFmpeg produces a correct video.

The assertions that matter most are the refusals: a missing bounding box, a
timestamp past the end of the clip, a region too small to cover, an output that
lost its audio or drifted in duration. Each of those must stop the workflow
rather than produce something that would then be analysed as if it were a
correction.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import reconstruction
from reconstruction import (
    DURATION_TOLERANCE_SECONDS,
    MIN_REGION_PIXELS,
    PixelRegion,
    ReconstructionError,
    SourceProbe,
    UnsafeRegion,
    Workspace,
    probe_media,
    reconstruct,
    resolve_region,
    resolve_window,
    validate_output,
)
from conftest import real_png

# A REAL, decodable PNG (Phase 9.1): `reconstruct` now probes the asset with
# ffprobe before building the graph, so a padded magic number is refused
# before the FFmpeg call these tests are about. See `conftest.real_png`.
PNG = real_png()


def probe(width=1920, height=1080, duration=10.0, audio=True) -> SourceProbe:
    return SourceProbe(
        width=width,
        height=height,
        duration_seconds=duration,
        has_audio=audio,
        video_codec="h264",
        pixel_format="yuv420p",
    )


def ffprobe_payload(width=1920, height=1080, duration="10.0", audio=True):
    streams = [
        {
            "codec_type": "video",
            "width": width,
            "height": height,
            "duration": duration,
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"duration": duration}}


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------

def test_probe_reads_the_real_dimensions_and_streams():
    with patch("ffmpeg.probe", return_value=ffprobe_payload()):
        result = probe_media("clip.mp4")

    assert result.width == 1920
    assert result.height == 1080
    assert result.duration_seconds == pytest.approx(10.0)
    assert result.has_audio is True


def test_probe_reports_a_missing_audio_stream():
    with patch("ffmpeg.probe", return_value=ffprobe_payload(audio=False)):
        assert probe_media("clip.mp4").has_audio is False


def test_probe_reports_a_missing_executable_distinctly():
    """A missing binary and a corrupt file are different operator problems."""
    with patch("ffmpeg.probe", side_effect=FileNotFoundError()):
        with pytest.raises(ReconstructionError) as exc:
            probe_media("clip.mp4")
    assert "FFmpeg not found" in str(exc.value)
    assert exc.value.retryable is False


def test_probe_rejects_a_file_with_no_video_stream():
    with patch(
        "ffmpeg.probe",
        return_value={"streams": [{"codec_type": "audio"}], "format": {}},
    ):
        with pytest.raises(ReconstructionError) as exc:
            probe_media("audio.m4a")
    assert "no video stream" in str(exc.value)


@pytest.mark.parametrize("duration", ["0", "-5", "not-a-number", None])
def test_probe_rejects_an_unusable_duration(duration):
    payload = ffprobe_payload(duration=duration)
    payload["format"]["duration"] = duration
    with patch("ffmpeg.probe", return_value=payload):
        with pytest.raises(ReconstructionError):
            probe_media("clip.mp4")


@pytest.mark.parametrize("width,height", [(0, 1080), (1920, 0), (-1, -1)])
def test_probe_rejects_invalid_dimensions(width, height):
    with patch("ffmpeg.probe", return_value=ffprobe_payload(width, height)):
        with pytest.raises(ReconstructionError):
            probe_media("clip.mp4")


# ---------------------------------------------------------------------------
# the region: nothing is invented
# ---------------------------------------------------------------------------

def test_a_box_resolves_to_pixels_of_the_real_frame():
    """The 0-1000 normalized space scaled by the PROBED dimensions."""
    region = resolve_region([100, 200, 300, 400], probe(1000, 500))

    # x: 200/1000 * 1000 = 200 ; y: 100/1000 * 500 = 50
    assert region.x == 200
    assert region.y == 50
    # width: (400-200)/1000 * 1000 = 200 ; height: (300-100)/1000 * 500 = 100
    assert region.width == 200
    assert region.height == 100


def test_the_region_is_even_sized_for_yuv420p():
    """Odd dimensions make the scaler fail or shift the region silently."""
    region = resolve_region([1, 1, 101, 101], probe(999, 999))
    assert region.width % 2 == 0
    assert region.height % 2 == 0
    assert region.x % 2 == 0
    assert region.y % 2 == 0


def test_the_region_never_exceeds_the_frame():
    region = resolve_region([0, 0, 1000, 1000], probe(640, 480))
    assert region.x == 0
    assert region.y == 0
    assert region.x + region.width <= 640
    assert region.y + region.height <= 480


def test_a_missing_box_refuses_rather_than_inventing_one():
    """The single most important refusal in this module.

    A finding with no region cannot be corrected automatically, and covering
    the whole frame or guessing a default would alter footage nobody approved.
    """
    with pytest.raises(UnsafeRegion) as exc:
        resolve_region(None, probe())
    assert "no region" in str(exc.value)
    assert exc.value.retryable is False


@pytest.mark.parametrize(
    "box",
    [
        [1, 2, 3],  # too short
        [1, 2, 3, 4, 5],  # too long
        "100,200,300,400",  # not a sequence of numbers
        [None, None, None, None],
        [True, False, True, False],  # bools are not coordinates
        {"ymin": 1},  # wrong shape entirely
    ],
)
def test_a_malformed_box_refuses(box):
    with pytest.raises(UnsafeRegion):
        resolve_region(box, probe())


@pytest.mark.parametrize(
    "box",
    [
        [500, 100, 100, 400],  # inverted on y
        [100, 400, 300, 200],  # inverted on x
        [100, 200, 100, 400],  # zero height
        [100, 200, 300, 200],  # zero width
    ],
)
def test_an_inverted_or_empty_box_refuses(box):
    with pytest.raises(UnsafeRegion):
        resolve_region(box, probe())


@pytest.mark.parametrize(
    "box",
    [
        [-1, 0, 100, 100],
        [0, -1, 100, 100],
        [0, 0, 1001, 100],
        [0, 0, 100, 1001],
        [float("nan"), 0, 100, 100],
        [float("inf"), 0, 100, 100],
    ],
)
def test_an_out_of_range_box_refuses(box):
    with pytest.raises(UnsafeRegion):
        resolve_region(box, probe())


def test_a_region_too_small_to_cover_refuses():
    """A legal normalized box can still resolve to a useless few pixels."""
    with pytest.raises(UnsafeRegion) as exc:
        # 1/1000 of a 320px frame is well under the floor.
        resolve_region([0, 0, 1, 1], probe(320, 240))
    assert "too small" in str(exc.value)


def test_the_minimum_region_is_a_real_bound():
    assert MIN_REGION_PIXELS >= 2


# ---------------------------------------------------------------------------
# the window: the timestamp must describe THIS file
# ---------------------------------------------------------------------------

def test_the_window_is_centred_on_the_findings_timestamp():
    start, end = resolve_window(5.0, probe(duration=10.0), window_seconds=1.0)
    assert start == pytest.approx(4.0)
    assert end == pytest.approx(6.0)


def test_the_window_is_clamped_to_the_clip():
    start, end = resolve_window(0.2, probe(duration=10.0), window_seconds=1.0)
    assert start == 0.0
    assert end == pytest.approx(1.2)

    start, end = resolve_window(9.9, probe(duration=10.0), window_seconds=1.0)
    assert end == pytest.approx(10.0)


def test_a_timestamp_past_the_end_refuses():
    """The stored finding does not describe this file.

    Editing anyway would apply an approved decision to the wrong moment, or to
    no moment at all.
    """
    with pytest.raises(UnsafeRegion) as exc:
        resolve_window(95.0, probe(duration=10.0))
    assert "does not describe this file" in str(exc.value)


@pytest.mark.parametrize(
    "timestamp", [None, -1.0, float("nan"), float("inf"), "3", True]
)
def test_an_unusable_timestamp_refuses(timestamp):
    with pytest.raises(UnsafeRegion):
        resolve_window(timestamp, probe(duration=10.0))


def test_a_zero_length_window_refuses():
    """It would enable the overlay on no frame at all.

    The output would then be identical to the input, and we would analyse it
    as if it were a correction.
    """
    with pytest.raises(UnsafeRegion):
        resolve_window(10.0, probe(duration=10.0), window_seconds=0.0)


# ---------------------------------------------------------------------------
# output validation: exit code 0 is not evidence
# ---------------------------------------------------------------------------

def test_a_missing_output_file_fails(tmp_path):
    with pytest.raises(ReconstructionError) as exc:
        validate_output(str(tmp_path / "nope.mp4"), probe(), expect_audio=True)
    assert "not produced" in str(exc.value)


def test_an_empty_output_file_fails(tmp_path):
    empty = tmp_path / "out.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ReconstructionError) as exc:
        validate_output(str(empty), probe(), expect_audio=True)
    assert "empty" in str(exc.value)


def test_an_unprobeable_output_fails(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"not a video")
    import ffmpeg

    with patch("ffmpeg.probe", side_effect=ffmpeg.Error("probe", b"", b"")):
        with pytest.raises(ReconstructionError):
            validate_output(str(out), probe(), expect_audio=True)


def test_an_output_that_lost_its_audio_fails(tmp_path):
    """Dropping audio silently is one of the named Phase 9 hazards."""
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    with patch("ffmpeg.probe", return_value=ffprobe_payload(audio=False)):
        with pytest.raises(ReconstructionError) as exc:
            validate_output(str(out), probe(audio=True), expect_audio=True)
    assert "lost the original's audio" in str(exc.value)


def test_a_silent_source_is_allowed_to_produce_a_silent_output(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    with patch("ffmpeg.probe", return_value=ffprobe_payload(audio=False)):
        result = validate_output(
            str(out), probe(audio=False), expect_audio=False
        )
    assert result.has_audio is False


def test_an_output_whose_duration_drifted_fails(tmp_path):
    """A changed length means the pass did something unintended."""
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    drifted = str(10.0 + DURATION_TOLERANCE_SECONDS + 1)
    with patch("ffmpeg.probe", return_value=ffprobe_payload(duration=drifted)):
        with pytest.raises(ReconstructionError) as exc:
            validate_output(str(out), probe(duration=10.0), expect_audio=True)
    assert "length does not match" in str(exc.value)


def test_a_duration_within_tolerance_passes(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    with patch("ffmpeg.probe", return_value=ffprobe_payload(duration="10.2")):
        result = validate_output(
            str(out), probe(duration=10.0), expect_audio=True
        )
    assert result.duration_seconds == pytest.approx(10.2)


def test_an_output_whose_dimensions_changed_fails(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    with patch("ffmpeg.probe", return_value=ffprobe_payload(640, 360)):
        with pytest.raises(ReconstructionError) as exc:
            validate_output(
                str(out), probe(1920, 1080), expect_audio=True
            )
    assert "dimensions do not match" in str(exc.value)


# ---------------------------------------------------------------------------
# the pass, at the subprocess boundary
# ---------------------------------------------------------------------------

class FakeProcess:
    """A stand-in for the FFmpeg subprocess."""

    def __init__(self, returncode=0, stderr=b"", on_communicate=None):
        self.returncode = returncode
        self._stderr = stderr
        self._on_communicate = on_communicate
        self.killed = False

    def communicate(self, timeout=None):
        if self._on_communicate:
            self._on_communicate()
        return b"", self._stderr

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def run_reconstruct(
    tmp_path,
    *,
    source_probe=None,
    output_probe=None,
    returncode=0,
    box=None,
    timestamp=3.0,
    write_output=True,
):
    """Drive `reconstruct` with FFmpeg stubbed. Returns (result, captured)."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)
    output = tmp_path / "out.mp4"

    source_payload = source_probe or ffprobe_payload()
    output_payload = output_probe or ffprobe_payload()
    # Phase 9.1 added a third probe between the two: the ASSET is probed for
    # decodability before the graph is built. Listed explicitly rather than
    # left to the fallback, so a test that sets a distinctive `output_probe`
    # cannot have it silently consumed by the asset probe instead.
    asset_payload = ffprobe_payload(16, 16, duration="0.04", audio=False)
    probes: list = [source_payload, asset_payload, output_payload]
    captured: dict = {}

    def fake_probe(path, *a, **k):
        # In order: the source, the replacement image, then the output.
        return probes.pop(0) if probes else output_payload

    def fake_run_async(self, *a, **k):
        captured["args"] = self.get_args()
        if write_output and returncode == 0:
            output.write_bytes(b"x" * 4096)
        return FakeProcess(returncode=returncode)

    with patch("ffmpeg.probe", side_effect=fake_probe), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        result = reconstruct(
            source_path=str(source),
            asset_path=str(asset),
            bounding_box=box if box is not None else [100, 200, 300, 400],
            timestamp_seconds=timestamp,
            output_path=str(output),
        )
    return result, captured, output


def test_the_filter_graph_overlays_only_inside_the_window(tmp_path):
    _result, captured, _out = run_reconstruct(tmp_path, timestamp=3.0)
    args = " ".join(captured["args"])

    assert "overlay" in args
    # Enabled between 2 and 4 seconds for a finding at t=3 with the default
    # one-second half-width - so every other instant is the source's frame.
    #
    # The commas are backslash-escaped because they are inside a filter
    # expression, which is FFmpeg's own escaping: an unescaped comma would
    # split `enable=...` into separate filter options and the window would be
    # silently wrong. Asserting the escaped form pins that too.
    assert r"between(t\,2.000\,4.000)" in args
    assert "-filter_complex" in captured["args"]


def test_the_asset_is_scaled_to_the_approved_region(tmp_path):
    _result, captured, _out = run_reconstruct(tmp_path)
    args = " ".join(captured["args"])
    # 1920x1080 frame, box [100,200,300,400] -> 384x216 at (384,108).
    assert "scale" in args
    assert "384" in args and "216" in args


def test_audio_is_copied_not_re_encoded(tmp_path):
    """The strongest "audio preserved" guarantee available."""
    _result, captured, _out = run_reconstruct(tmp_path)
    args = captured["args"]
    assert "copy" in args
    index = args.index("copy")
    assert args[index - 1] in ("-acodec", "-c:a")


def test_a_silent_source_produces_a_warning_not_a_failure(tmp_path):
    silent = ffprobe_payload(audio=False)
    result, _captured, _out = run_reconstruct(
        tmp_path, source_probe=silent, output_probe=silent
    )
    assert result.has_audio is False
    assert any("no audio" in w for w in result.warnings)


def test_the_result_records_a_truthful_audit_trail(tmp_path):
    result, _captured, _out = run_reconstruct(tmp_path)
    audit = result.as_audit_fields()

    assert audit["method"] == "scaled_overlay"
    assert audit["region_pixels"] == {
        "x": 384,
        "y": 108,
        "width": 384,
        "height": 216,
    }
    assert audit["window_seconds"] == [2.0, 4.0]
    assert audit["source_width"] == 1920
    assert audit["audio_preserved"] is True
    # No local path, bucket or key in an audit record a user may read.
    assert "path" not in audit
    assert "key" not in audit


def test_a_failed_ffmpeg_run_removes_the_partial_output(tmp_path):
    """Never leave a partial file for a caller to upload and then analyse."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)
    output = tmp_path / "out.mp4"

    def fake_run_async(self, *a, **k):
        output.write_bytes(b"partial")
        return FakeProcess(returncode=1, stderr=b"ffmpeg said no")

    with patch("ffmpeg.probe", return_value=ffprobe_payload()), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(ReconstructionError):
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=[100, 200, 300, 400],
                timestamp_seconds=3.0,
                output_path=str(output),
            )

    assert not output.exists()


def test_ffmpeg_stderr_never_reaches_the_message(tmp_path):
    """It contains the full command line, which includes local paths."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)

    def fake_run_async(self, *a, **k):
        return FakeProcess(
            returncode=1, stderr=b"/secret/path/source.mp4: Invalid data"
        )

    with patch("ffmpeg.probe", return_value=ffprobe_payload()), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(ReconstructionError) as exc:
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=[100, 200, 300, 400],
                timestamp_seconds=3.0,
                output_path=str(tmp_path / "out.mp4"),
            )

    assert "/secret/path" not in str(exc.value)
    assert "Invalid data" not in str(exc.value)


def test_a_timeout_is_retryable(tmp_path):
    """Load can cause it; the same inputs may succeed later."""
    import subprocess

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)

    def fake_run_async(self, *a, **k):
        def boom():
            raise subprocess.TimeoutExpired("ffmpeg", 1)

        return FakeProcess(on_communicate=boom)

    with patch("ffmpeg.probe", return_value=ffprobe_payload()), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(ReconstructionError) as exc:
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=[100, 200, 300, 400],
                timestamp_seconds=3.0,
                output_path=str(tmp_path / "out.mp4"),
                timeout_seconds=1,
            )
    assert exc.value.retryable is True


def test_an_output_that_does_not_validate_is_removed(tmp_path):
    """A clean exit with a bad file must not reach the caller."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)
    output = tmp_path / "out.mp4"

    def fake_run_async(self, *a, **k):
        output.write_bytes(b"x" * 4096)
        return FakeProcess(returncode=0)

    probes = [ffprobe_payload(), ffprobe_payload(audio=False)]

    with patch("ffmpeg.probe", side_effect=lambda *a, **k: probes.pop(0)), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(ReconstructionError):
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=[100, 200, 300, 400],
                timestamp_seconds=3.0,
                output_path=str(output),
            )

    assert not output.exists()


def test_the_source_bytes_are_never_modified(tmp_path):
    """The headline guarantee of this phase."""
    source = tmp_path / "source.mp4"
    original = b"source-bytes-exactly-as-uploaded"
    source.write_bytes(original)

    run_reconstruct(tmp_path)
    # run_reconstruct rewrote source.mp4 with its own content; re-run with a
    # known body to assert immutability precisely.
    source.write_bytes(original)
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)
    output = tmp_path / "out2.mp4"

    def fake_run_async(self, *a, **k):
        output.write_bytes(b"x" * 4096)
        return FakeProcess(returncode=0)

    with patch("ffmpeg.probe", return_value=ffprobe_payload()), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        reconstruct(
            source_path=str(source),
            asset_path=str(asset),
            bounding_box=[100, 200, 300, 400],
            timestamp_seconds=3.0,
            output_path=str(output),
        )

    assert source.read_bytes() == original


def test_writing_over_the_source_is_refused(tmp_path):
    """The last line of defence for source immutability."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)

    with pytest.raises(ReconstructionError) as exc:
        reconstruct(
            source_path=str(source),
            asset_path=str(asset),
            bounding_box=[100, 200, 300, 400],
            timestamp_seconds=3.0,
            output_path=str(source),
        )
    assert "over its own source" in str(exc.value)
    assert source.read_bytes() == b"source-bytes"


def test_a_bad_region_refuses_before_ffmpeg_is_invoked(tmp_path):
    """No subprocess, no spend, for inputs we already know are unusable."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(PNG)

    invoked = {"count": 0}

    def fake_run_async(self, *a, **k):
        invoked["count"] += 1
        return FakeProcess()

    with patch("ffmpeg.probe", return_value=ffprobe_payload()), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(UnsafeRegion):
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=None,
                timestamp_seconds=3.0,
                output_path=str(tmp_path / "out.mp4"),
            )

    assert invoked["count"] == 0


# ---------------------------------------------------------------------------
# the workspace
# ---------------------------------------------------------------------------

def test_the_workspace_is_removed_on_success():
    with Workspace() as workspace:
        path = workspace.path
        Path(workspace.file("x.txt")).write_text("hello")
        assert Path(path).exists()
    assert not Path(path).exists()


def test_the_workspace_is_removed_when_the_body_raises():
    path = None
    with pytest.raises(RuntimeError):
        with Workspace() as workspace:
            path = workspace.path
            Path(workspace.file("x.txt")).write_text("hello")
            raise RuntimeError("boom")
    assert path is not None
    assert not Path(path).exists()


def test_two_workspaces_never_collide():
    with Workspace() as a, Workspace() as b:
        assert a.path != b.path


def test_a_closed_workspace_refuses_to_name_a_file():
    workspace = Workspace()
    with pytest.raises(RuntimeError):
        workspace.file("x.txt")


# ---------------------------------------------------------------------------
# Phase 9.1: the asset is probed BEFORE FFmpeg is invoked
# ---------------------------------------------------------------------------
#
# Why these matter more than they look. `loop=1` on an undecodable image does
# not make FFmpeg exit non-zero - it makes it BLOCK, until the pass hits
# `timeout_seconds`. That timeout is classified retryable, so the whole
# attempt budget is spent on bytes that can never work: three waits of five
# minutes for one permanent answer. Measured on FFmpeg 9.0.1 before the fix:
# an 8-byte PNG header followed by zeroes consumed 301s per attempt.
#
# So the refusal has to happen before the subprocess starts, and it has to be
# permanent. These assert both, with FFmpeg stubbed - the real-binary
# counterparts live in test_integration_ffmpeg_reconstruction.py.

def _reconstruct_with_asset(tmp_path, asset_bytes, *, asset_probe_raises=True):
    """Call `reconstruct` with a given asset, recording whether FFmpeg ran."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(asset_bytes)
    output = tmp_path / "out.mp4"

    calls = {"run_async": 0}
    payloads = [ffprobe_payload()]

    def fake_probe(path, *a, **k):
        if payloads:
            return payloads.pop(0)
        # The asset probe. A real ffprobe reports 0x0 for a padded header.
        if asset_probe_raises:
            return {"streams": [{"codec_type": "video", "width": 0, "height": 0}]}
        return ffprobe_payload(16, 16, duration="0.04", audio=False)

    def fake_run_async(self, *a, **k):
        calls["run_async"] += 1
        output.write_bytes(b"x" * 4096)
        return FakeProcess(returncode=0)

    with patch("ffmpeg.probe", side_effect=fake_probe), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(ReconstructionError) as exc:
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=[100, 200, 300, 400],
                timestamp_seconds=3.0,
                output_path=str(output),
            )
    return exc.value, calls, output


def test_an_undecodable_asset_is_refused_before_ffmpeg_is_invoked(tmp_path):
    """The whole point: no subprocess is started, so nothing can block."""
    error, calls, _output = _reconstruct_with_asset(
        tmp_path, b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
    )

    assert calls["run_async"] == 0, (
        "FFmpeg was invoked with an undecodable image; it would block until "
        "the reconstruction timeout rather than failing."
    )
    assert isinstance(error, ReconstructionError)


def test_an_undecodable_asset_is_a_permanent_failure(tmp_path):
    """Not retryable: the same bytes decode the same way every time.

    Before Phase 9.1 this surfaced as a TIMEOUT, which `_run` marks
    retryable, so the executor spent all three attempts on it.
    """
    error, calls, _output = _reconstruct_with_asset(
        tmp_path, b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
    )

    # Both halves matter. Without the first assertion this passes even if the
    # probe is removed, because a later guard still raises a non-retryable
    # error - just after FFmpeg has already blocked for the full timeout.
    assert calls["run_async"] == 0
    assert error.retryable is False


def test_the_refusal_leaves_no_output_and_leaks_no_path(tmp_path):
    error, _calls, output = _reconstruct_with_asset(
        tmp_path, b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
    )

    assert not output.exists()
    message = str(error)
    assert str(tmp_path) not in message
    assert "asset.png" not in message
    assert ".mp4" not in message


def test_an_asset_with_no_video_stream_is_refused(tmp_path):
    """A container ffprobe can open but that holds no image."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    asset = tmp_path / "asset.png"
    asset.write_bytes(real_png())
    output = tmp_path / "out.mp4"

    payloads = [ffprobe_payload()]
    calls = {"run_async": 0}

    def fake_probe(path, *a, **k):
        if payloads:
            return payloads.pop(0)
        return {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}

    def fake_run_async(self, *a, **k):
        calls["run_async"] += 1
        return FakeProcess(returncode=0)

    with patch("ffmpeg.probe", side_effect=fake_probe), patch(
        "ffmpeg.nodes.OutputStream.run_async", fake_run_async, create=True
    ):
        with pytest.raises(ReconstructionError):
            reconstruct(
                source_path=str(source),
                asset_path=str(asset),
                bounding_box=[100, 200, 300, 400],
                timestamp_seconds=3.0,
                output_path=str(output),
            )

    assert calls["run_async"] == 0


def test_a_decodable_asset_still_reaches_ffmpeg(tmp_path):
    """The guard must not refuse a legitimate image."""
    result, captured, _out = run_reconstruct(tmp_path)

    assert result.method == "scaled_overlay"
    assert "overlay" in " ".join(captured["args"])
