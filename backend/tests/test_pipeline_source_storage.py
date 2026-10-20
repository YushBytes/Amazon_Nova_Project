"""Phase 4.1: durable source-video storage in S3.

Every S3 call here goes to the in-memory `fake_s3` from conftest. **No live S3
upload, download, or delete is performed by any test in this file**, so these
tests prove the application's logic and ordering, not that AWS works.

The property that matters most: a job must be runnable by a process that never
saw the upload. That is asserted directly in
`test_analysis_works_on_a_host_that_never_saw_the_upload`, which clears the
local upload directory before running the worker.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from fastapi.testclient import TestClient

import app as app_module
import auth
import aws_resources
import pipeline
import source_store
import video_repo
from extractor import ExtractionResult
from job_state import JobState


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def alice():
    return {"Authorization": f"Bearer {auth.create_access_token('alice', 'client')}"}


@pytest.fixture
def mallory():
    return {"Authorization": f"Bearer {auth.create_access_token('mallory', 'client')}"}


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", target.resolve())
    monkeypatch.setattr(pipeline, "_legacy_upload_dir", lambda: target.resolve())
    return target.resolve()


@pytest.fixture
def no_background(monkeypatch):
    captured = []

    def fake_add_task(self, func, *args, **kwargs):
        captured.append((func, args, kwargs))

    from fastapi import BackgroundTasks

    monkeypatch.setattr(BackgroundTasks, "add_task", fake_add_task, raising=False)
    return captured


def _upload(client, headers, filename="clip.mp4", data=b"fake-video-bytes"):
    return client.post(
        "/upload-reel",
        files={"file": (filename, data, "video/mp4")},
        headers=headers,
    )


def _extraction(frames=3):
    return ExtractionResult(
        video_id="vid",
        frames_stored=frames,
        duration_seconds=float(frames),
        fps=1,
        truncated=False,
        frame_timestamps=[float(i) for i in range(frames)],
    )


def _clean_analysis(frames=3):
    return {"pending": frames, "audited": frames, "failed": 0, "detections": 0}


async def _record_for(fake_db, video_id):
    return await video_repo.get_video(video_id)


# ---------------------------------------------------------------------------
# upload puts the source in S3
# ---------------------------------------------------------------------------

def _source_doc(fake_db, video_id):
    """The raw stored record, for fields job_view deliberately hides."""
    for doc in fake_db["videos"].docs:
        if doc["video_id"] == video_id:
            return doc
    raise AssertionError(f"no record for {video_id}")


def test_upload_stores_the_source_in_s3(
    client, fake_db, upload_dir, alice, fake_s3
):
    body = _upload(client, alice).json()
    vid = body["video_id"]

    assert len(fake_s3.puts) == 1, "the upload must reach object storage exactly once"
    put = fake_s3.puts[0]
    assert put["Bucket"] == aws_resources.media_bucket()
    assert fake_s3.has(aws_resources.media_bucket(), f"sources/{vid}/source.mp4")


def test_object_key_is_server_generated_from_the_video_id(
    client, fake_db, upload_dir, alice, fake_s3
):
    """No fragment of the client filename may appear in the key."""
    body = _upload(client, alice, filename="../../../etc/pa ss wd.mp4").json()
    vid = body["video_id"]

    assert fake_s3.keys() == [f"sources/{vid}/source.mp4"]
    key = fake_s3.keys()[0]
    assert ".." not in key
    assert " " not in key
    assert "passwd" not in key
    assert key == aws_resources.source_key(vid, "mp4")


def test_stored_object_is_encrypted_and_not_an_executable_type(
    client, fake_db, upload_dir, alice, fake_s3
):
    _upload(client, alice)
    put = fake_s3.puts[0]
    assert put["ServerSideEncryption"] == "AES256"
    assert put["ContentType"] == "video/mp4"
    assert "html" not in put["ContentType"]


def test_object_reference_is_persisted_on_the_record(
    client, fake_db, upload_dir, alice, anyio_backend=None
):
    import anyio

    body = _upload(client, alice).json()
    record = anyio.run(lambda: _record_for(fake_db, body["video_id"]))

    assert record["s3_bucket"] == aws_resources.media_bucket()
    assert record["s3_key"] == f"sources/{body['video_id']}/source.mp4"
    assert source_store.has_durable_source(record) is True


def test_upload_does_not_leave_the_source_on_local_disk(
    client, fake_db, upload_dir, alice
):
    """The staged copy is temporary; the durable copy is the S3 object."""
    _upload(client, alice)
    assert list(upload_dir.iterdir()) == [], (
        "a local copy would recreate the Phase 4 durability problem"
    )


def test_upload_response_never_names_the_bucket(
    client, fake_db, upload_dir, alice
):
    response = _upload(client, alice)
    assert aws_resources.media_bucket() not in response.text
    assert "s3://" not in response.text
    assert "amazonaws" not in response.text


def test_job_view_never_leaks_the_object_reference(
    client, fake_db, upload_dir, alice
):
    body = _upload(client, alice).json()
    job = client.get(f"/jobs/{body['video_id']}", headers=alice)
    assert "s3_key" not in job.text
    assert "s3_bucket" not in job.text
    assert "sources/" not in job.text


# ---------------------------------------------------------------------------
# upload failure handling
# ---------------------------------------------------------------------------

def test_s3_failure_rejects_the_upload_and_creates_no_record(
    client, fake_db, upload_dir, alice, fake_s3
):
    fake_s3.fail_put = "ServiceUnavailable"

    response = _upload(client, alice)

    assert response.status_code == 503
    assert fake_db["videos"].docs == [], (
        "a record without stored bytes would be an unplayable, unanalysable job"
    )


def test_s3_timeout_rejects_the_upload_safely(
    client, fake_db, upload_dir, alice, fake_s3
):
    fake_s3.fail_put = ConnectTimeoutError(endpoint_url="https://example.invalid")

    response = _upload(client, alice)

    assert response.status_code == 503
    # No bucket name, endpoint, or botocore class reaches the client.
    assert "amazonaws" not in response.text
    assert "ConnectTimeout" not in response.text
    assert aws_resources.media_bucket() not in response.text


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Redirect `tempfile` into tmp_path so staging files are observable.

    Without this, the staging directory is created under the system temp
    directory and a test asserting on tmp_path would pass whatever happened.
    """
    import tempfile

    root = tmp_path / "systemp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_upload_failure_leaves_no_temporary_file(
    client, fake_db, upload_dir, alice, fake_s3, temp_root
):
    fake_s3.fail_put = "ServiceUnavailable"

    response = _upload(client, alice)

    assert response.status_code == 503
    assert list(temp_root.iterdir()) == [], (
        "the staged copy must be removed even when the durable write fails"
    )
    assert list(upload_dir.iterdir()) == []


def test_successful_upload_leaves_no_temporary_file(
    client, fake_db, upload_dir, alice, temp_root
):
    assert _upload(client, alice).status_code == 200
    assert list(temp_root.iterdir()) == []


def test_a_processed_job_leaves_no_temporary_file(
    client, fake_db, upload_dir, alice, no_background, temp_root
):
    """Covers the download side: the staging dir AND the download dir."""
    body = _upload(client, alice).json()
    client.post(f"/analyze/{body['video_id']}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(2)),          patch("analyzer.check_pending_frames", return_value=_clean_analysis(2)):
        pipeline.run_job(body["video_id"])

    assert list(temp_root.iterdir()) == []


def test_database_failure_after_upload_deletes_the_orphaned_object(
    client, fake_db, upload_dir, alice, fake_s3, monkeypatch
):
    """The compensating cleanup the two-step write requires.

    Without it the object would sit in the bucket with no record naming it:
    unreachable by any route, unattributable to any user, and still billed.
    """
    async def boom(**_kwargs):
        raise RuntimeError("induced ownership write failure")

    monkeypatch.setattr(video_repo, "record_video", boom)

    response = _upload(client, alice)

    assert response.status_code == 500
    assert len(fake_s3.deletes) == 1, "the orphaned object must be removed"
    assert fake_s3.objects == {}


def test_cleanup_failure_does_not_mask_the_original_error(
    client, fake_db, upload_dir, alice, fake_s3, monkeypatch
):
    """A failed cleanup must not turn a 500 into a 502 or a traceback."""
    async def boom(**_kwargs):
        raise RuntimeError("induced ownership write failure")

    monkeypatch.setattr(video_repo, "record_video", boom)
    fake_s3.fail_delete = "AccessDenied"

    response = _upload(client, alice)

    assert response.status_code == 500
    assert "AccessDenied" not in response.text


# ---------------------------------------------------------------------------
# download for processing
# ---------------------------------------------------------------------------

def test_analysis_works_on_a_host_that_never_saw_the_upload(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    """The headline Phase 4.1 property.

    The local upload directory is emptied before the worker runs, which is
    what a replaced container looks like. Phase 4 failed here with "Video
    payload not found"; the source now comes from S3.
    """
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    for leftover in upload_dir.iterdir():
        leftover.unlink()

    with patch("extractor.extract_and_store", return_value=_extraction(3)) as extract, \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(3)):
        result = pipeline.run_job(vid)

    assert result["state"] == JobState.COMPLETED.value
    # FFmpeg was handed a real local path - the downloaded temporary file.
    local_path = extract.call_args[0][0]
    assert Path(local_path).name.startswith("source")
    assert str(upload_dir) not in str(local_path)


def test_downloaded_source_is_removed_after_a_successful_run(
    client, fake_db, upload_dir, alice, no_background
):
    seen = {}

    def capture(path, video_id=None):
        seen["path"] = path
        assert Path(path).is_file(), "the source must exist while FFmpeg runs"
        return _extraction(2)

    body = _upload(client, alice).json()
    client.post(f"/analyze/{body['video_id']}", headers=alice)

    with patch("extractor.extract_and_store", side_effect=capture), \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(2)):
        pipeline.run_job(body["video_id"])

    assert not Path(seen["path"]).exists()
    assert not Path(seen["path"]).parent.exists(), (
        "the temporary directory must go too, not just the file"
    )


def test_downloaded_source_is_removed_after_a_failed_run(
    client, fake_db, upload_dir, alice, no_background
):
    from extractor import ExtractionError

    seen = {}

    def boom(path, video_id=None):
        seen["path"] = path
        raise ExtractionError("The video could not be decoded.")

    body = _upload(client, alice).json()
    client.post(f"/analyze/{body['video_id']}", headers=alice)

    with patch("extractor.extract_and_store", side_effect=boom):
        result = pipeline.run_job(body["video_id"])

    assert result["state"] == JobState.FAILED.value
    assert not Path(seen["path"]).exists()
    assert not Path(seen["path"]).parent.exists()


def test_two_jobs_never_share_a_temporary_path(
    client, fake_db, upload_dir, alice, no_background
):
    paths = []

    def capture(path, video_id=None):
        paths.append(path)
        return _extraction(1)

    first = _upload(client, alice).json()["video_id"]
    second = _upload(client, alice).json()["video_id"]
    client.post(f"/analyze/{first}", headers=alice)
    client.post(f"/analyze/{second}", headers=alice)

    with patch("extractor.extract_and_store", side_effect=capture), \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(1)):
        pipeline.run_job(first)
        pipeline.run_job(second)

    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert Path(paths[0]).parent != Path(paths[1]).parent


def test_missing_source_object_fails_the_job_truthfully(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)
    fake_s3.objects.clear()

    result = pipeline.run_job(vid)

    assert result["state"] == JobState.FAILED.value
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["error_summary"]
    assert "no longer available" in job["error_summary"].lower()


def test_s3_read_timeout_is_retried_without_leaking_detail(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    """A transport timeout is retryable; its message still leaks nothing.

    Phase 6 changed the outcome from terminal failure to a requeue with a
    backoff - a timeout is the textbook case where the same input succeeds on
    a later attempt. The redaction property is unchanged and is what this
    still guards: no endpoint, no bucket, no botocore class name.
    """
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)
    fake_s3.fail_get = ReadTimeoutError(endpoint_url="https://example.invalid")

    result = pipeline.run_job(vid)

    assert result.get("retry_scheduled") is True
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.QUEUED.value

    reason = _source_doc(fake_db, vid)["last_retry_reason"]
    assert "could not be retrieved" in reason.lower()
    assert "amazonaws" not in reason
    assert "example.invalid" not in reason
    assert aws_resources.media_bucket() not in reason
    assert "ReadTimeout" not in reason


def test_s3_read_timeout_fails_terminally_once_attempts_run_out(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    """Bounded: a timeout that never clears ends FAILED, still redacted."""
    from job_state import MAX_ATTEMPTS

    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)
    fake_s3.fail_get = ReadTimeoutError(endpoint_url="https://example.invalid")

    with patch("pipeline.retry_delay_seconds", return_value=0):
        for _ in range(MAX_ATTEMPTS + 2):
            _source_doc(fake_db, vid)["not_before"] = None
            pipeline.run_job(vid)

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["attempt"] <= MAX_ATTEMPTS
    summary = job["error_summary"]
    assert "No attempts remain" in summary
    assert "example.invalid" not in summary
    assert aws_resources.media_bucket() not in summary
    assert "ReadTimeout" not in summary


def test_download_enforces_the_upload_size_limit(
    client, fake_db, upload_dir, alice, no_background, fake_s3, monkeypatch
):
    """An object swapped out of band must not bypass the ingest ceiling."""
    import config

    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    # Replace the stored object with something past the limit.
    fake_s3.seed(
        aws_resources.media_bucket(), f"sources/{vid}/source.mp4", b"x" * 4096
    )
    settings = config.get_settings()
    monkeypatch.setattr(settings, "max_upload_bytes", 1024)

    result = pipeline.run_job(vid)

    assert result["state"] == JobState.FAILED.value
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert "larger than" in job["error_summary"].lower()


def test_empty_source_object_is_a_failure_not_an_empty_audit(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)
    fake_s3.seed(aws_resources.media_bucket(), f"sources/{vid}/source.mp4", b"")

    result = pipeline.run_job(vid)

    assert result["state"] == JobState.FAILED.value
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] != JobState.COMPLETED.value
    assert job["detections_recorded"] in (None, 0)


# ---------------------------------------------------------------------------
# the retrieved object must belong to the job
# ---------------------------------------------------------------------------

def test_a_key_from_another_video_is_refused(fake_db, upload_dir):
    """Defence in depth against a crossed or tampered record.

    The key is server-generated from the video id, so a key naming a
    different video means something is wrong. Processing it would attribute
    one user's footage to another user's job.
    """
    record = {
        "video_id": "vid_aaaaaaaa",
        "s3_bucket": "test-bucket",
        "s3_key": "sources/vid_bbbbbbbb/source.mp4",
    }
    with pytest.raises(source_store.SourceMissing):
        with source_store.materialise(record, upload_dir):
            pass


def test_a_key_outside_the_sources_prefix_is_refused(fake_db, upload_dir):
    record = {
        "video_id": "vid_aaaaaaaa",
        "s3_bucket": "test-bucket",
        "s3_key": "frames/vid_aaaaaaaa/frame_0000.jpg",
    }
    with pytest.raises(source_store.SourceMissing):
        with source_store.materialise(record, upload_dir):
            pass


def test_the_matching_key_is_accepted(fake_db, upload_dir, fake_s3):
    fake_s3.seed("test-bucket", "sources/vid_aaaaaaaa/source.mp4", b"bytes")
    record = {
        "video_id": "vid_aaaaaaaa",
        "s3_bucket": "test-bucket",
        "s3_key": "sources/vid_aaaaaaaa/source.mp4",
    }
    with source_store.materialise(record, upload_dir) as path:
        assert Path(path).read_bytes() == b"bytes"


# ---------------------------------------------------------------------------
# retry after a failed attempt
# ---------------------------------------------------------------------------

def test_retry_downloads_the_source_again_and_can_succeed(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    from extractor import ExtractionError

    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store",
               side_effect=ExtractionError("The video could not be decoded.")):
        pipeline.run_job(vid)

    assert client.get(f"/jobs/{vid}", headers=alice).json()["state"] == "failed"
    gets_before = len(fake_s3.gets)

    assert client.post(f"/analyze/{vid}/retry", headers=alice).status_code == 200
    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(2)):
        result = pipeline.run_job(vid)

    assert result["state"] == JobState.COMPLETED.value
    assert len(fake_s3.gets) > gets_before, (
        "the retry must re-fetch the source, not reuse a vanished local file"
    )


def test_the_source_object_is_never_deleted_by_the_pipeline(
    client, fake_db, upload_dir, alice, no_background, fake_s3
):
    """A retry needs the source, and the owner may still be playing it."""
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(2)):
        pipeline.run_job(vid)

    assert fake_s3.has(aws_resources.media_bucket(), f"sources/{vid}/source.mp4")
    assert fake_s3.deletes == []


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------

def test_playback_streams_the_object_to_its_owner(
    client, fake_db, upload_dir, alice
):
    body = _upload(client, alice, data=b"the-video-bytes").json()
    url = body["video_url"].split("http://localhost:8000", 1)[1]

    response = client.get(url)

    assert response.status_code == 200
    assert response.content == b"the-video-bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["accept-ranges"] == "bytes"


def test_playback_supports_range_requests_so_seeking_still_works(
    client, fake_db, upload_dir, alice
):
    """FileResponse handled Range for free; StreamingResponse does not.

    Without forwarding Range to S3, moving storage would have silently broken
    scrubbing in the player.
    """
    body = _upload(client, alice, data=b"0123456789").json()
    url = body["video_url"].split("http://localhost:8000", 1)[1]

    response = client.get(url, headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_playback_denies_a_token_for_another_video(
    client, fake_db, upload_dir, alice
):
    first = _upload(client, alice).json()
    second = _upload(client, alice).json()
    token = first["video_url"].split("access_token=", 1)[1]

    response = client.get(
        f"/uploads/{second['filename']}?access_token={token}"
    )
    assert response.status_code == 404


def test_playback_denies_another_user(client, fake_db, upload_dir, alice, mallory):
    body = _upload(client, alice).json()
    path = body["video_url"].split("/uploads/", 1)[1].split("?")[0]

    # Mallory's own media token for someone else's video id.
    stolen = auth.create_media_token("mallory", body["video_id"])
    response = client.get(f"/uploads/{path}?access_token={stolen}")
    assert response.status_code == 404


def test_playback_of_a_missing_object_is_404_not_a_server_error(
    client, fake_db, upload_dir, alice, fake_s3
):
    body = _upload(client, alice).json()
    url = body["video_url"].split("http://localhost:8000", 1)[1]
    fake_s3.objects.clear()

    response = client.get(url)
    assert response.status_code == 404


def test_playback_store_failure_reports_safely(
    client, fake_db, upload_dir, alice, fake_s3
):
    body = _upload(client, alice).json()
    url = body["video_url"].split("http://localhost:8000", 1)[1]
    fake_s3.fail_get = "InternalError"

    response = client.get(url)

    assert response.status_code == 502
    assert aws_resources.media_bucket() not in response.text
    assert "InternalError" not in response.text


# ---------------------------------------------------------------------------
# legacy records
# ---------------------------------------------------------------------------

def _make_legacy(fake_db, video_id, upload_dir, data=b"legacy-bytes"):
    """Turn a 4.1 record into what a pre-4.1 upload looked like."""
    path = upload_dir / f"{video_id}.mp4"
    path.write_bytes(data)
    for doc in fake_db["videos"].docs:
        if doc["video_id"] == video_id:
            doc["s3_key"] = None
            doc["s3_bucket"] = None
    return path


def test_a_legacy_record_still_plays_back_from_local_disk(
    client, fake_db, upload_dir, alice
):
    body = _upload(client, alice).json()
    _make_legacy(fake_db, body["video_id"], upload_dir)
    url = body["video_url"].split("http://localhost:8000", 1)[1]

    response = client.get(url)

    assert response.status_code == 200
    assert response.content == b"legacy-bytes"


def test_a_legacy_record_can_still_be_analysed_from_local_disk(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    _make_legacy(fake_db, vid, upload_dir)

    client.post(f"/analyze/{vid}", headers=alice)
    with patch("extractor.extract_and_store", return_value=_extraction(2)) as extract, \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(2)):
        result = pipeline.run_job(vid)

    assert result["state"] == JobState.COMPLETED.value
    assert str(upload_dir) in str(extract.call_args[0][0])


def test_a_legacy_local_file_is_not_deleted_after_processing(
    client, fake_db, upload_dir, alice, no_background
):
    """It is the only copy; removing it would destroy the user's upload."""
    body = _upload(client, alice).json()
    vid = body["video_id"]
    path = _make_legacy(fake_db, vid, upload_dir)

    client.post(f"/analyze/{vid}", headers=alice)
    with patch("extractor.extract_and_store", return_value=_extraction(1)), \
         patch("analyzer.check_pending_frames", return_value=_clean_analysis(1)):
        pipeline.run_job(vid)

    assert path.is_file()
    assert path.read_bytes() == b"legacy-bytes"


def test_a_legacy_filename_cannot_escape_the_upload_directory(
    fake_db, upload_dir
):
    """The database is not a trust boundary.

    `stored_filename` is server-generated, but it comes back out of storage,
    so containment is re-checked when it is used to build a path.
    """
    record = {"video_id": "vid_aaaaaaaa", "stored_filename": "../../secrets.env"}
    assert source_store.local_fallback_path(record, upload_dir) is None


def test_a_legacy_record_with_no_file_is_reported_missing(fake_db, upload_dir):
    record = {"video_id": "vid_aaaaaaaa", "stored_filename": "vid_aaaaaaaa.mp4"}
    with pytest.raises(source_store.SourceMissing):
        with source_store.materialise(record, upload_dir):
            pass


# ---------------------------------------------------------------------------
# authorization is unchanged
# ---------------------------------------------------------------------------

def test_unauthenticated_upload_is_refused(client, fake_db, upload_dir, fake_s3):
    response = client.post(
        "/upload-reel", files={"file": ("clip.mp4", b"bytes", "video/mp4")}
    )
    assert response.status_code == 401
    assert fake_s3.puts == [], "an unauthenticated request must not store anything"


def test_cross_user_analysis_is_refused_and_stores_nothing(
    client, fake_db, upload_dir, alice, mallory, no_background
):
    body = _upload(client, alice).json()
    assert client.post(
        f"/analyze/{body['video_id']}", headers=mallory
    ).status_code == 404
    assert client.post(
        f"/analyze/{body['video_id']}/retry", headers=mallory
    ).status_code == 404
