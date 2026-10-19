"""Phase 4: the pipeline end to end, and the API that drives it.

Every external service is mocked: S3, DynamoDB, Bedrock, and FFmpeg. These
tests prove the ORCHESTRATION is correct — that state is truthful at each
stage, that a failure cannot look like success, and that retries do not
duplicate results. They are NOT evidence that live AWS or Bedrock works.

The single most important property under test: a successful audit with zero
detections must be distinguishable from an analysis that failed. Conflating
them would report unanalysed footage as compliant.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import pipeline
import video_repo
from detections import Detection, FrameAnalysis, failed_analysis
from extractor import ExtractionError, ExtractionResult
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
    return target.resolve()


def _upload(client, headers, filename="clip.mp4"):
    return client.post(
        "/upload-reel",
        files={"file": (filename, b"fake-video-bytes", "video/mp4")},
        headers=headers,
    )


def _extraction(frames=3, truncated=False):
    return ExtractionResult(
        video_id="vid",
        frames_stored=frames,
        duration_seconds=float(frames),
        fps=1,
        truncated=truncated,
        frame_timestamps=[float(i) for i in range(frames)],
    )


def _detection():
    return Detection(
        category="competitor_logo",
        description="Competitor branding visible.",
        confidence=0.9,
        bounding_box=[100.0, 200.0, 300.0, 400.0],
        risk_level="high",
    )


@pytest.fixture
def no_background(monkeypatch):
    """Stop BackgroundTasks from running the worker during route tests.

    The route's job is to queue durably and hand off; the worker is driven
    directly in the pipeline tests below.
    """
    captured = []

    def fake_add_task(self, func, *args, **kwargs):
        captured.append((func, args, kwargs))

    monkeypatch.setattr(
        "starlette.background.BackgroundTasks.add_task", fake_add_task, raising=False
    )
    from fastapi import BackgroundTasks

    monkeypatch.setattr(BackgroundTasks, "add_task", fake_add_task, raising=False)
    return captured


# ---------------------------------------------------------------------------
# upload registers a real job
# ---------------------------------------------------------------------------

def test_upload_creates_a_job_in_the_uploaded_state(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    assert body["status"] == "UPLOADED"  # the upload response's own wording

    job = client.get(f"/jobs/{body['video_id']}", headers=alice).json()
    assert job["state"] == JobState.UPLOADED.value
    assert job["is_terminal"] is False
    assert job["is_active"] is False, "an upload must not look like running work"
    assert job["attempt"] == 0
    assert job["frames_extracted"] is None


def test_job_endpoint_requires_authentication(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    assert client.get(f"/jobs/{body['video_id']}").status_code == 401


def test_job_endpoint_denies_cross_user_access(
    client, fake_db, upload_dir, alice, mallory
):
    body = _upload(client, alice).json()
    response = client.get(f"/jobs/{body['video_id']}", headers=mallory)
    assert response.status_code == 404
    # Must not confirm the id exists.
    assert "uploaded" not in response.text.lower()


def test_job_endpoint_404s_for_an_unknown_video(client, fake_db, upload_dir, alice):
    assert client.get("/jobs/vid_99999999", headers=alice).status_code == 404


# ---------------------------------------------------------------------------
# submission
# ---------------------------------------------------------------------------

def test_analyze_queues_the_job_without_claiming_completion(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    response = client.post(f"/analyze/{body['video_id']}", headers=alice)

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["state"] == JobState.QUEUED.value
    assert payload["job"]["is_terminal"] is False
    # The critical honesty property: queueing is not completion.
    assert payload["job"]["state"] != JobState.COMPLETED.value
    assert payload["job"]["attempt"] == 1


def test_duplicate_analyze_is_refused_with_409(
    client, fake_db, upload_dir, alice, no_background
):
    """A double-clicked button must not start two runs."""
    body = _upload(client, alice).json()
    first = client.post(f"/analyze/{body['video_id']}", headers=alice)
    second = client.post(f"/analyze/{body['video_id']}", headers=alice)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already running" in second.json()["detail"].lower()


def test_analyze_denies_cross_user_submission(
    client, fake_db, upload_dir, alice, mallory, no_background
):
    body = _upload(client, alice).json()
    assert client.post(f"/analyze/{body['video_id']}", headers=mallory).status_code == 404


def test_analyze_requires_the_file_to_exist(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    (upload_dir / body["filename"]).unlink()
    assert client.post(f"/analyze/{body['video_id']}", headers=alice).status_code == 404


# ---------------------------------------------------------------------------
# the worker: happy path
# ---------------------------------------------------------------------------

def _run_worker(fake_db, video_id, upload_dir, filename):
    return pipeline.run_job(video_id, str(upload_dir / filename))


def test_successful_run_reaches_completed_with_real_counts(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(3)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 3, "audited": 3, "failed": 0, "detections": 2}):
        result = _run_worker(fake_db, vid, upload_dir, body["filename"])

    assert result["state"] == JobState.COMPLETED.value

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.COMPLETED.value
    assert job["is_terminal"] is True
    assert job["frames_extracted"] == 3
    assert job["frames_audited"] == 3
    assert job["frames_failed"] == 0
    assert job["detections_recorded"] == 2
    assert job["completed_at"] is not None
    assert job["error_summary"] is None
    assert job["model_id"] == "amazon.nova-pro-v1:0"


def test_zero_detections_is_a_successful_completion(
    client, fake_db, upload_dir, alice, no_background
):
    """THE key distinction: a clean audit is not a failure.

    Every frame audited, no violations found. This must be COMPLETED with
    zero detections, so the UI can honestly say "compliant".
    """
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(4)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 4, "audited": 4, "failed": 0, "detections": 0}):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.COMPLETED.value
    assert job["frames_audited"] == 4
    assert job["detections_recorded"] == 0
    assert job["error_summary"] is None


def test_analysis_failure_is_not_a_clean_audit(
    client, fake_db, upload_dir, alice, no_background
):
    """The counterpart: no detections because nothing was analysed.

    This must be FAILED, never COMPLETED-with-zero, or the UI would report
    unanalysed footage as compliant.
    """
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(4)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 4, "audited": 0, "failed": 4, "detections": 0}):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["detections_recorded"] == 0
    assert job["error_summary"], "a failed job must explain itself"
    assert job["state"] != JobState.COMPLETED.value


def test_partial_analysis_failure_is_not_completed(
    client, fake_db, upload_dir, alice, no_background
):
    """Some frames failed: the audit is incomplete, so it is not complete.

    Reporting this as COMPLETED would let the UI show "compliant" for footage
    that was only partly analysed.
    """
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(5)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 5, "audited": 3, "failed": 2, "detections": 1}):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["frames_audited"] == 3
    assert job["frames_failed"] == 2
    assert "2 of 5" in job["error_summary"]


# ---------------------------------------------------------------------------
# the worker: failure at each stage
# ---------------------------------------------------------------------------

def test_extraction_failure_records_a_safe_terminal_state(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch(
        "extractor.extract_and_store",
        side_effect=ExtractionError("The file contains no video stream."),
    ):
        result = _run_worker(fake_db, vid, upload_dir, body["filename"])

    assert result["state"] == JobState.FAILED.value
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["error_summary"] == "The file contains no video stream."
    assert job["frames_extracted"] is None


def test_unexpected_extraction_crash_does_not_leak_internals(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch(
        "extractor.extract_and_store",
        side_effect=RuntimeError("s3://secret-bucket/path blew up"),
    ):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert "secret-bucket" not in job["error_summary"]
    assert "RuntimeError" not in job["error_summary"]


def test_frame_store_unavailable_is_reported_safely(
    client, fake_db, upload_dir, alice, no_background
):
    from analyzer import FrameAuditError

    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames",
               side_effect=FrameAuditError("Could not scan TableName: ClientError")):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["error_summary"] == "The analysis store is unavailable."
    assert "TableName" not in job["error_summary"]


def test_completion_write_failure_does_not_report_success(
    client, fake_db, upload_dir, alice, no_background, monkeypatch
):
    """If the completion cannot be stored, the job must not claim success.

    The stored state is what a client reads, so an unrecorded completion is
    not a completion.
    """
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    real_transition = video_repo.transition

    async def fail_on_completed(video_id, target, **kwargs):
        if target is JobState.COMPLETED:
            raise RuntimeError("mongo unavailable")
        return await real_transition(video_id, target, **kwargs)

    monkeypatch.setattr(video_repo, "transition", fail_on_completed)
    monkeypatch.setattr(pipeline.video_repo, "transition", fail_on_completed)

    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 2, "audited": 2, "failed": 0, "detections": 0}):
        result = _run_worker(fake_db, vid, upload_dir, body["filename"])

    assert result["state"] == JobState.FAILED.value

    monkeypatch.setattr(video_repo, "transition", real_transition)
    monkeypatch.setattr(pipeline.video_repo, "transition", real_transition)
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.FAILED.value
    assert job["error_summary"] == "Results could not be saved."


def test_worker_refuses_a_job_it_cannot_claim(client, fake_db, upload_dir, alice):
    """An un-queued job must not be executed, and must not be mutated."""
    body = _upload(client, alice).json()
    vid = body["video_id"]
    # Never submitted, so still UPLOADED.

    with patch("extractor.extract_and_store", return_value=_extraction(2)) as extract:
        result = _run_worker(fake_db, vid, upload_dir, body["filename"])

    assert result["skipped"] is True
    assert result["reason"] == "not_queued"
    assert not extract.called, "the worker ran without claiming the job"

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.UPLOADED.value


def test_worker_on_an_unknown_video_is_a_no_op(fake_db, upload_dir):
    result = pipeline.run_job("vid_00000000", str(upload_dir / "nope.mp4"))
    assert result["skipped"] is True
    assert result["reason"] in ("unknown_video", "claim_failed")


def test_concurrent_workers_cannot_both_run_one_job(
    client, fake_db, upload_dir, alice, no_background
):
    """The conditional claim is what prevents duplicate processing."""
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 2, "audited": 2, "failed": 0, "detections": 0}):
        first = _run_worker(fake_db, vid, upload_dir, body["filename"])
        # A second worker for the same job finds it no longer QUEUED.
        second = _run_worker(fake_db, vid, upload_dir, body["filename"])

    assert first["state"] == JobState.COMPLETED.value
    assert second.get("skipped") is True


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

def test_retry_of_a_failed_job_reruns_and_can_succeed(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    with patch("extractor.extract_and_store",
               side_effect=ExtractionError("The video could not be decoded.")):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    assert client.get(f"/jobs/{vid}", headers=alice).json()["state"] == "failed"

    retry = client.post(f"/analyze/{vid}/retry", headers=alice)
    assert retry.status_code == 200
    assert retry.json()["job"]["state"] == JobState.QUEUED.value
    assert retry.json()["job"]["attempt"] == 2
    # The stale error must be cleared, not left to be read as current.
    assert retry.json()["job"]["error_summary"] is None

    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames",
               return_value={"pending": 2, "audited": 2, "failed": 0, "detections": 1}):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.COMPLETED.value
    assert job["detections_recorded"] == 1


def test_retry_is_bounded_by_max_attempts(
    client, fake_db, upload_dir, alice, no_background
):
    """A persistently failing video must not be retried forever."""
    from job_state import MAX_ATTEMPTS

    body = _upload(client, alice).json()
    vid = body["video_id"]

    client.post(f"/analyze/{vid}", headers=alice)
    for _ in range(MAX_ATTEMPTS * 2):
        with patch("extractor.extract_and_store",
                   side_effect=ExtractionError("nope")):
            _run_worker(fake_db, vid, upload_dir, body["filename"])
        response = client.post(f"/analyze/{vid}/retry", headers=alice)
        if response.status_code == 429:
            break

    assert response.status_code == 429
    assert "maximum" in response.json()["detail"].lower()
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["attempt"] <= MAX_ATTEMPTS


def test_retry_while_running_is_refused(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    response = client.post(f"/analyze/{vid}/retry", headers=alice)
    assert response.status_code == 409


def test_retry_before_any_analysis_is_refused(
    client, fake_db, upload_dir, alice, no_background
):
    body = _upload(client, alice).json()
    response = client.post(f"/analyze/{body['video_id']}/retry", headers=alice)
    assert response.status_code == 409
    assert "not been analysed" in response.json()["detail"].lower()


def test_retry_denies_cross_user_access(
    client, fake_db, upload_dir, alice, mallory, no_background
):
    body = _upload(client, alice).json()
    assert (
        client.post(f"/analyze/{body['video_id']}/retry", headers=mallory).status_code
        == 404
    )


def test_rerunning_a_completed_job_does_not_duplicate_counts(
    client, fake_db, upload_dir, alice, no_background
):
    """A retry overwrites the previous outcome rather than accumulating it."""
    body = _upload(client, alice).json()
    vid = body["video_id"]
    client.post(f"/analyze/{vid}", headers=alice)

    summary = {"pending": 2, "audited": 2, "failed": 0, "detections": 1}
    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=summary):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    first = client.get(f"/jobs/{vid}", headers=alice).json()
    assert first["detections_recorded"] == 1

    client.post(f"/analyze/{vid}/retry", headers=alice)
    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=summary):
        _run_worker(fake_db, vid, upload_dir, body["filename"])

    second = client.get(f"/jobs/{vid}", headers=alice).json()
    # Replaced, not summed.
    assert second["detections_recorded"] == 1
    assert second["frames_audited"] == 2
    assert second["attempt"] == 2


# ---------------------------------------------------------------------------
# results retrieval
# ---------------------------------------------------------------------------

def _dynamo_page(items, last_key=None):
    page = {"Items": items}
    if last_key is not None:
        page["LastEvaluatedKey"] = {"video_id": "v", "frame_number": last_key}
    return page


def _frame_item(frame_number, status="audited", detections=None):
    item = {
        "video_id": "v",
        "frame_number": frame_number,
        "timestamp": str(float(frame_number)),
        "status": status,
        "detections": detections or [],
    }
    return item


def test_violations_reports_the_job_state_alongside_results(
    client, fake_db, upload_dir, alice, no_background
):
    """A client must be able to tell "clean" from "not finished"."""
    body = _upload(client, alice).json()
    vid = body["video_id"]

    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = _dynamo_page([])
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        response = client.get(f"/violations/{vid}", headers=alice)

    assert response.status_code == 200
    payload = response.json()
    assert payload["detections"] == []
    # Empty detections but the job never ran: the state says so.
    assert payload["job"]["state"] == JobState.UPLOADED.value
    assert payload["job"]["is_terminal"] is False


def test_violations_normalises_stored_detections(
    client, fake_db, upload_dir, alice
):
    from decimal import Decimal

    body = _upload(client, alice).json()
    vid = body["video_id"]

    stored = [
        {
            "category": "competitor_logo",
            "description": "visible",
            "confidence": Decimal("0.91"),
            "bounding_box": [Decimal("100"), Decimal("200"), Decimal("300"), Decimal("400")],
            "risk_level": "high",
        }
    ]
    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = _dynamo_page(
        [_frame_item(2, detections=stored)]
    )
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        payload = client.get(f"/violations/{vid}", headers=alice).json()

    detection = payload["detections"][0]
    # Decimal is not JSON serialisable; it must come back as a number.
    assert detection["confidence"] == pytest.approx(0.91)
    assert detection["bounding_box"] == [100.0, 200.0, 300.0, 400.0]
    assert detection["timestamp"] == pytest.approx(2.0)
    assert detection["frame_number"] == 2
    assert payload["audited_frames"] == 1


def test_violations_omits_absent_optional_fields(client, fake_db, upload_dir, alice):
    """A missing confidence must stay missing, not become 0.

    Otherwise the UI would render a fabricated "0%" the model never gave.
    """
    body = _upload(client, alice).json()
    stored = [{"category": "logo", "description": "no confidence given"}]
    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = _dynamo_page(
        [_frame_item(0, detections=stored)]
    )
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        payload = client.get(
            f"/violations/{body['video_id']}", headers=alice
        ).json()

    detection = payload["detections"][0]
    assert "confidence" not in detection
    assert "bounding_box" not in detection


def test_violations_paginates(client, fake_db, upload_dir, alice):
    """A long video holds more frames than one DynamoDB query returns."""
    body = _upload(client, alice).json()
    vid = body["video_id"]

    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = _dynamo_page(
        [_frame_item(i) for i in range(3)], last_key=2
    )
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        first = client.get(f"/violations/{vid}", headers=alice).json()

    assert first["has_more"] is True
    assert first["next_cursor"] == "2"

    dynamo.Table.return_value.query.return_value = _dynamo_page([_frame_item(3)])
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        second = client.get(
            f"/violations/{vid}?cursor={first['next_cursor']}", headers=alice
        ).json()

    assert second["has_more"] is False
    assert second["next_cursor"] is None
    # The cursor was passed through as an ExclusiveStartKey.
    kwargs = dynamo.Table.return_value.query.call_args.kwargs
    assert kwargs["ExclusiveStartKey"]["frame_number"] == 2


def test_violations_rejects_a_malformed_cursor(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    dynamo = MagicMock()
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        response = client.get(
            f"/violations/{body['video_id']}?cursor=not-a-number", headers=alice
        )
    assert response.status_code == 400


def test_violations_counts_failed_frames_separately(
    client, fake_db, upload_dir, alice
):
    body = _upload(client, alice).json()
    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = _dynamo_page(
        [
            _frame_item(0, status="audited"),
            _frame_item(1, status="audit_failed"),
            _frame_item(2, status="pending_audit"),
        ]
    )
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        payload = client.get(
            f"/violations/{body['video_id']}", headers=alice
        ).json()

    assert payload["audited_frames"] == 1
    assert payload["failed_frames"] == 1
    assert payload["frame_count"] == 3


def test_violations_store_failure_is_reported_safely(
    client, fake_db, upload_dir, alice
):
    from botocore.exceptions import ClientError

    body = _upload(client, alice).json()
    dynamo = MagicMock()
    dynamo.Table.return_value.query.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException",
                   "Message": "Table NovaFlowFrames not found"}},
        "Query",
    )
    with patch.object(app_module.boto3, "resource", return_value=dynamo):
        response = client.get(f"/violations/{body['video_id']}", headers=alice)

    assert response.status_code == 502
    assert "NovaFlowFrames" not in response.text
    assert "ResourceNotFound" not in response.text


# ---------------------------------------------------------------------------
# stuck-job detection
# ---------------------------------------------------------------------------

async def test_find_stuck_jobs_reports_orphans(fake_db):
    """An in-process worker cannot survive a restart; those jobs need finding."""
    from datetime import datetime, timedelta, timezone

    await video_repo.record_video(
        video_id="vid_stuck001",
        owner="alice",
        stored_filename="vid_stuck001.mp4",
        original_filename="a.mp4",
        extension="mp4",
        size_bytes=10,
    )
    await video_repo.transition("vid_stuck001", JobState.QUEUED)
    await video_repo.transition(
        "vid_stuck001", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    # Backdate the update so it looks abandoned.
    await fake_db["videos"].update_one(
        {"video_id": "vid_stuck001"},
        {"$set": {"updated_at": datetime.now(timezone.utc) - timedelta(hours=2)}},
    )

    stuck = await pipeline.find_stuck_jobs()
    assert [r["video_id"] for r in stuck] == ["vid_stuck001"]


async def test_find_stuck_jobs_ignores_fresh_and_terminal_jobs(fake_db):
    await video_repo.record_video(
        video_id="vid_fresh0001",
        owner="alice",
        stored_filename="vid_fresh0001.mp4",
        original_filename="a.mp4",
        extension="mp4",
        size_bytes=10,
    )
    await video_repo.transition("vid_fresh0001", JobState.QUEUED)

    await video_repo.record_video(
        video_id="vid_done00001",
        owner="alice",
        stored_filename="vid_done00001.mp4",
        original_filename="a.mp4",
        extension="mp4",
        size_bytes=10,
    )
    await video_repo.transition("vid_done00001", JobState.QUEUED)
    await video_repo.transition(
        "vid_done00001", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    await video_repo.transition(
        "vid_done00001", JobState.ANALYZING, expected=JobState.EXTRACTING
    )
    await video_repo.transition(
        "vid_done00001", JobState.COMPLETED, expected=JobState.ANALYZING
    )

    stuck = await pipeline.find_stuck_jobs()
    assert stuck == []
