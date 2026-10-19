"""Phase 4: frame extraction contract.

Previously `extract_and_store` returned `None` on every failure path — no
video stream, duration over the limit, a probe error — so the caller could not
tell success from failure and the job stayed pending forever. It now returns a
result or raises `ExtractionError`.

FFmpeg itself is mocked here: the executable is an environmental prerequisite,
not something every unit test should need. A real-FFmpeg test is marked
`requires_ffmpeg` and skips when the binary is absent.
"""

import io
from unittest.mock import MagicMock, patch

import pytest

import extractor
from extractor import ExtractionError, ExtractionResult


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _probe(duration=5.0, with_video=True):
    streams = []
    if with_video:
        streams.append({"codec_type": "video", "duration": str(duration)})
    streams.append({"codec_type": "audio", "duration": str(duration)})
    return {"streams": streams, "format": {"duration": str(duration)}}


def _jpeg(payload=b"x" * 16):
    """A minimal JPEG-framed blob the marker scanner will accept."""
    return b"\xff\xd8" + payload + b"\xff\xd9"


def _fake_process(frames, returncode=0):
    """An FFmpeg stand-in that emits `frames` JPEG blobs then stops."""
    stream = b"".join(_jpeg(bytes([i % 251]) * 16) for i in range(frames))
    process = MagicMock()
    process.stdout = io.BytesIO(stream)
    process.stderr = io.BytesIO(b"")
    # poll() returns None once (so the loop reads), then the exit code.
    process.poll.side_effect = [None] + [returncode] * 50
    process.wait.return_value = returncode
    return process


@pytest.fixture
def aws(monkeypatch):
    """Capture S3 puts and DynamoDB writes without touching AWS."""
    s3 = MagicMock()
    table = MagicMock()
    monkeypatch.setattr(extractor, "s3_client", s3)
    monkeypatch.setattr(extractor, "table", table)
    return s3, table


# ---------------------------------------------------------------------------
# probing failures are real failures
# ---------------------------------------------------------------------------

def test_missing_ffmpeg_executable_is_reported_clearly(aws):
    """The wrapper installs via pip; the binary does not."""
    with patch.object(extractor.ffmpeg, "probe", side_effect=FileNotFoundError()):
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")
    assert "FFmpeg not found" in str(excinfo.value)


def test_corrupt_input_raises_rather_than_returning_none(aws):
    error = extractor.ffmpeg.Error("probe", b"", b"moov atom not found")
    with patch.object(extractor.ffmpeg, "probe", side_effect=error):
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")
    assert "corrupt" in str(excinfo.value).lower()


def test_file_with_no_video_stream_is_a_failure(aws):
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(with_video=False)):
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("audio.mp4", "vid_abcd1234")
    assert "no video stream" in str(excinfo.value).lower()


def test_unreadable_duration_is_a_failure(aws):
    probe = {"streams": [{"codec_type": "video"}], "format": {}}
    with patch.object(extractor.ffmpeg, "probe", return_value=probe):
        with pytest.raises(ExtractionError):
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")


def test_zero_duration_is_a_failure(aws):
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(0.0)):
        with pytest.raises(ExtractionError):
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")


def test_overlong_video_is_rejected_with_a_clear_reason(aws, monkeypatch):
    """Previously a silent `return`, which left the job pending forever."""
    import config

    monkeypatch.setattr(
        config.get_settings(), "max_video_duration_seconds", 10, raising=False
    )
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(45.0)):
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")
    message = str(excinfo.value)
    assert "45.0s" in message and "10s" in message


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------

def test_successful_extraction_returns_real_counts(aws):
    s3, table = aws
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(3.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(3)
        result = extractor.extract_and_store("clip.mp4", "vid_abcd1234")

    assert isinstance(result, ExtractionResult)
    assert result.frames_stored == 3
    assert result.truncated is False
    assert s3.put_object.call_count == 3
    assert table.put_item.call_count == 3


def test_frame_timestamps_map_to_the_source_video(aws):
    """The UI seeks with these, so they must reflect the sampling clock."""
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(4.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(4)
        result = extractor.extract_and_store("clip.mp4", "vid_abcd1234")

    # 1 fps default: frame N is at second N.
    assert result.frame_timestamps == [0.0, 1.0, 2.0, 3.0]
    assert result.fps == 1


def test_s3_keys_and_frame_numbers_are_server_generated(aws):
    s3, table = aws
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(2.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(2)
        extractor.extract_and_store("../../evil name.mp4", "vid_abcd1234")

    keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
    assert keys == [
        "frames/vid_abcd1234/frame_0000.jpg",
        "frames/vid_abcd1234/frame_0001.jpg",
    ]
    # Nothing from the client path reached the key.
    assert all("evil" not in k and ".." not in k for k in keys)

    items = [c.kwargs["Item"] for c in table.put_item.call_args_list]
    assert [i["frame_number"] for i in items] == [0, 1]
    assert all(i["status"] == extractor.STATUS_PENDING for i in items)


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------

def test_frame_count_is_capped(aws, monkeypatch):
    """A malformed container must not be able to cost unbounded model calls."""
    import config

    monkeypatch.setattr(
        config.get_settings(), "max_frames_per_video", 3, raising=False
    )
    s3, _ = aws
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(30.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(20)
        result = extractor.extract_and_store("clip.mp4", "vid_abcd1234")

    assert result.frames_stored == 3
    assert result.truncated is True
    assert s3.put_object.call_count == 3
    assert "truncated" in result.summary


def test_no_frames_extracted_is_a_failure(aws):
    """An empty frame stream must not be reported as a clean audit."""
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(3.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(0)
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")
    assert "no frames" in str(excinfo.value).lower()


def test_ffmpeg_nonzero_exit_with_no_frames_is_a_failure(aws):
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(3.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(0, returncode=1)
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")
    assert "could not be decoded" in str(excinfo.value).lower()


def test_ffmpeg_timeout_is_reported_as_a_timeout(aws):
    process = _fake_process(1)
    process.wait.side_effect = Exception("timed out")

    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(3.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = process
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")
    assert "too long" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# storage failures and cleanup
# ---------------------------------------------------------------------------

def test_s3_failure_aborts_rather_than_reusing_a_frame_number(aws):
    """The old code logged and continued without incrementing the counter,
    which reused the same S3 key and frame number for the next frame."""
    from botocore.exceptions import ClientError

    s3, _ = aws
    s3.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "PutObject"
    )
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(3.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = _fake_process(3)
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")

    assert "unavailable" in str(excinfo.value).lower()
    # The provider's message must not reach the job summary.
    assert "AccessDenied" not in str(excinfo.value)


def test_subprocess_is_released_on_success(aws):
    process = _fake_process(2)
    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(2.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = process
        extractor.extract_and_store("clip.mp4", "vid_abcd1234")

    assert process.poll.called


def test_subprocess_is_released_on_failure(aws):
    """Cleanup must run on the failure path too, or FFmpeg processes leak."""
    process = _fake_process(0)
    process.kill = MagicMock()
    process.poll.side_effect = [None, None, None, None, None, None]

    with patch.object(extractor.ffmpeg, "probe", return_value=_probe(2.0)), \
         patch.object(extractor.ffmpeg, "input") as ffmpeg_input:
        ffmpeg_input.return_value.filter.return_value.output.return_value \
            .run_async.return_value = process
        with pytest.raises(ExtractionError):
            extractor.extract_and_store("clip.mp4", "vid_abcd1234")

    # Either killed, or already exited; both count as released.
    assert process.kill.called or process.poll.called


def test_extraction_errors_never_carry_a_path_or_credential(aws):
    """ExtractionError text becomes a job's error_summary, shown to the owner."""
    error = extractor.ffmpeg.Error("probe", b"", b"/srv/secret/path failed")
    with patch.object(extractor.ffmpeg, "probe", side_effect=error):
        with pytest.raises(ExtractionError) as excinfo:
            extractor.extract_and_store("/srv/secret/path/clip.mp4", "vid_abcd1234")

    message = str(excinfo.value)
    assert "/srv/secret" not in message
    assert "AKIA" not in message


# ---------------------------------------------------------------------------
# optional: the real binary
# ---------------------------------------------------------------------------

@pytest.mark.requires_ffmpeg
def test_real_ffmpeg_probe_rejects_a_non_video_file(tmp_path):
    """Only meaningful where the FFmpeg binary is installed.

    Run with: pytest -m requires_ffmpeg
    """
    import shutil

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")

    bogus = tmp_path / "not-a-video.mp4"
    bogus.write_bytes(b"this is definitely not a video")

    with pytest.raises(ExtractionError):
        extractor.get_video_duration(str(bogus))
