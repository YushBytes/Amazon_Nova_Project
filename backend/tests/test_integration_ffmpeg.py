"""Real-FFmpeg integration tests for frame extraction.

Every test here is marked `requires_ffmpeg` and skips when the executable is
absent. **They skip on the development machine this phase was written on**, so
nothing in Phase 4.1 claims real FFmpeg extraction was verified - see the
Phase 4.1 report and docs/RELEASE_CHECKLIST.md.

The clip is generated at test time by FFmpeg's own `testsrc` source rather
than committed: a binary fixture in the repository would be another thing to
review on every diff, and generating it guarantees the input matches whatever
FFmpeg build is actually installed.

S3 is still the in-memory `fake_s3` from conftest, so a run of this file
exercises real decoding with mocked storage.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import aws_resources
import pipeline
import source_store
from job_state import JobState

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE),
    reason="ffmpeg/ffprobe not on PATH; real extraction not verified",
)

pytestmark = [pytest.mark.requires_ffmpeg, requires_ffmpeg]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _generate(path: Path, *, seconds: int = 3, fps: int = 10, size="160x120") -> Path:
    """Write a tiny synthetic clip with FFmpeg's built-in test pattern."""
    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size={size}:rate={fps}",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        timeout=120,
    )
    assert path.is_file() and path.stat().st_size > 0
    return path


@pytest.fixture
def tiny_video(tmp_path):
    return _generate(tmp_path / "tiny.mp4", seconds=3)


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(pipeline, "_legacy_upload_dir", lambda: target.resolve())
    return target.resolve()


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------

def test_duration_is_probed_from_a_real_clip(tiny_video):
    from extractor import get_video_duration

    duration = get_video_duration(str(tiny_video))

    assert 2.5 <= duration <= 3.5, f"expected roughly 3s, probed {duration}"


def test_a_corrupt_file_is_rejected(tmp_path):
    from extractor import ExtractionError, get_video_duration

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video" * 100)

    with pytest.raises(ExtractionError) as exc:
        get_video_duration(str(broken))

    # The owner-facing message must not carry a path or FFmpeg's stderr.
    assert str(broken) not in str(exc.value)


def test_an_audio_only_file_has_no_video_stream(tmp_path):
    from extractor import ExtractionError, get_video_duration

    audio = tmp_path / "audio.m4a"
    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=duration=1:frequency=440",
            str(audio),
        ],
        check=True,
        timeout=60,
    )

    with pytest.raises(ExtractionError, match="no video stream"):
        get_video_duration(str(audio))


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def test_real_extraction_stores_frames_in_s3(
    tiny_video, fake_s3, fake_frames_table
):
    from extractor import extract_and_store

    result = extract_and_store(str(tiny_video), "vid_ffmpeg01")

    assert result.frames_stored >= 1
    # 1 fps over ~3s. Allow a frame either side for rounding at the boundary.
    assert 2 <= result.frames_stored <= 4
    assert result.fps == 1

    bucket = aws_resources.media_bucket()
    for number in range(result.frames_stored):
        assert fake_s3.has(bucket, aws_resources.frame_key("vid_ffmpeg01", number))


def test_extracted_frames_are_real_jpegs(tiny_video, fake_s3, fake_frames_table):
    from extractor import extract_and_store

    extract_and_store(str(tiny_video), "vid_ffmpeg02")

    bucket = aws_resources.media_bucket()
    data = fake_s3.objects[(bucket, aws_resources.frame_key("vid_ffmpeg02", 0))]
    assert data.startswith(b"\xff\xd8"), "missing JPEG start-of-image marker"
    assert data.endswith(b"\xff\xd9"), "missing JPEG end-of-image marker"
    assert len(data) > 200


def test_frame_metadata_is_registered(tiny_video, fake_s3, fake_frames_table):
    from extractor import STATUS_PENDING, extract_and_store

    result = extract_and_store(str(tiny_video), "vid_ffmpeg03")

    numbers = fake_frames_table.frame_numbers("vid_ffmpeg03")
    assert numbers == list(range(result.frames_stored))
    item = fake_frames_table.items[("vid_ffmpeg03", 0)]
    assert item["status"] == STATUS_PENDING
    assert item["s3_key"] == aws_resources.frame_key("vid_ffmpeg03", 0)


def test_frame_timestamps_are_sequential_sampling_times(
    tiny_video, fake_s3, fake_frames_table
):
    from extractor import extract_and_store

    result = extract_and_store(str(tiny_video), "vid_ffmpeg04")

    assert result.frame_timestamps == [
        float(i) for i in range(result.frames_stored)
    ]
    for number, expected in enumerate(result.frame_timestamps):
        stored = fake_frames_table.items[("vid_ffmpeg04", number)]["timestamp"]
        assert float(stored) == expected


def test_the_frame_cap_truncates_a_longer_clip(
    tmp_path, fake_s3, fake_frames_table, monkeypatch
):
    import config
    from extractor import extract_and_store

    clip = _generate(tmp_path / "longer.mp4", seconds=8)
    settings = config.get_settings()
    monkeypatch.setattr(settings, "max_frames_per_video", 3)

    result = extract_and_store(str(clip), "vid_ffmpeg05")

    assert result.frames_stored == 3
    assert result.truncated is True
    assert fake_frames_table.frame_numbers("vid_ffmpeg05") == [0, 1, 2]


def test_an_overlong_clip_is_refused_before_any_frame_is_stored(
    tmp_path, fake_s3, fake_frames_table, monkeypatch
):
    import config
    from extractor import ExtractionError, extract_and_store

    clip = _generate(tmp_path / "overlong.mp4", seconds=4)
    settings = config.get_settings()
    monkeypatch.setattr(settings, "max_video_duration_seconds", 2)

    with pytest.raises(ExtractionError, match="limit for analysis"):
        extract_and_store(str(clip), "vid_ffmpeg06")

    assert fake_s3.objects == {}
    assert fake_frames_table.items == {}


def test_a_corrupt_clip_stores_nothing(tmp_path, fake_s3, fake_frames_table):
    from extractor import ExtractionError, extract_and_store

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x01\x02" * 500)

    with pytest.raises(ExtractionError):
        extract_and_store(str(broken), "vid_ffmpeg07")

    assert fake_s3.objects == {}


def test_a_short_ffmpeg_timeout_is_reported_not_hung(
    tiny_video, fake_s3, fake_frames_table, monkeypatch
):
    """The timeout must surface as a failure, never as a silent partial run."""
    import config
    from extractor import ExtractionError, extract_and_store

    settings = config.get_settings()
    monkeypatch.setattr(settings, "ffmpeg_timeout_seconds", 5)

    # Force wait() to time out regardless of how fast the clip decodes, so
    # the test does not depend on finding a clip slow enough to really hang.
    #
    # Scoped to the EXTRACTION process (Phase 9.1). `extract_and_store` probes
    # the clip with ffprobe before it extracts, and ffprobe waits on its own
    # child; patching `Popen.wait` unconditionally broke that probe instead,
    # so the call failed with "The video could not be inspected." and this
    # test asserted a timeout it had never actually induced. It never showed
    # up because the whole file skipped on a host with no FFmpeg.
    original_wait = subprocess.Popen.wait

    def patched_wait(self, timeout=None):
        args = self.args if isinstance(self.args, (list, tuple)) else [self.args]
        if any("ffprobe" in str(arg) for arg in args):
            return original_wait(self, timeout=timeout)
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)

    with patch.object(subprocess.Popen, "wait", patched_wait):
        with pytest.raises(ExtractionError, match="took too long"):
            extract_and_store(str(tiny_video), "vid_ffmpeg08")


def test_the_subprocess_is_not_left_running(tiny_video, fake_s3, fake_frames_table):
    """Pipes and the child process must be released on the success path."""
    import extractor

    captured = {}
    original = extractor._terminate

    def spy(process):
        captured["process"] = process
        return original(process)

    with patch.object(extractor, "_terminate", spy):
        extractor.extract_and_store(str(tiny_video), "vid_ffmpeg09")

    process = captured.get("process")
    assert process is not None, "_terminate must be called on the success path"
    assert process.poll() is not None, "the FFmpeg subprocess is still running"


# ---------------------------------------------------------------------------
# the S3-sourced path, end to end
# ---------------------------------------------------------------------------

def test_a_downloaded_source_can_be_extracted(
    tiny_video, fake_s3, fake_frames_table, upload_dir
):
    """Real decoding of bytes that came back out of (mocked) object storage.

    This is the Phase 4.1 path: the worker never sees the original upload, it
    downloads the object and hands FFmpeg the temporary file.
    """
    bucket = aws_resources.media_bucket()
    key = aws_resources.source_key("vid_ffmpeg10", "mp4")
    fake_s3.seed(bucket, key, tiny_video.read_bytes())

    record = {"video_id": "vid_ffmpeg10", "s3_bucket": bucket, "s3_key": key}

    from extractor import extract_and_store

    with source_store.materialise(record, upload_dir) as local_path:
        assert Path(local_path).is_file()
        result = extract_and_store(local_path, "vid_ffmpeg10")
        downloaded = Path(local_path)

    assert result.frames_stored >= 1
    assert not downloaded.exists(), "the downloaded source must be cleaned up"
    assert not downloaded.parent.exists()


def test_a_full_run_over_a_downloaded_source_completes(
    tiny_video, fake_s3, fake_frames_table, upload_dir, monkeypatch
):
    """run_job end to end with real FFmpeg; only Bedrock is mocked."""
    import video_repo

    bucket = aws_resources.media_bucket()
    key = aws_resources.source_key("vid_ffmpeg11", "mp4")
    fake_s3.seed(bucket, key, tiny_video.read_bytes())

    # Minimal queued record, as the upload route would have written.
    import anyio
    import db as db_module

    from tests.conftest import FakeDatabase

    database = FakeDatabase()

    async def _get_database():
        return database

    monkeypatch.setattr(db_module, "get_database", _get_database)
    monkeypatch.setattr(video_repo, "get_database", _get_database)

    database["videos"].insert_raw({
        "video_id": "vid_ffmpeg11",
        "owner": "alice",
        "stored_filename": "vid_ffmpeg11.mp4",
        "s3_bucket": bucket,
        "s3_key": key,
        "status": JobState.QUEUED.value,
        "attempt": 1,
        "claim_id": None,
        "lease_expires_at": None,
    })

    clean = {"pending": 3, "audited": 3, "failed": 0, "detections": 0}
    with patch("analyzer.check_pending_frames", return_value=clean):
        result = pipeline.run_job("vid_ffmpeg11")

    assert result["state"] == JobState.COMPLETED.value
    # Real frames were produced and stored before the (mocked) audit.
    assert fake_s3.has(bucket, aws_resources.frame_key("vid_ffmpeg11", 0))
