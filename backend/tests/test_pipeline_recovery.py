"""Phase 4.1: worker claims, leases, fencing, and crash recovery.

Everything here is mocked: no AWS, no Bedrock, no FFmpeg, no real MongoDB.
These tests prove the claim and recovery *logic* - who may write what, when -
not that a real deployment survives a real crash. The honest limits of the
recovery mechanism are documented in `pipeline`'s module docstring and in
docs/PIPELINE_OPERATIONS.md.

Worker interruption is injected deterministically rather than by killing a
process: `_abandon` rewinds a job's lease into the past, which is exactly the
state a dead worker leaves behind, and is the only signal the recovery sweep
actually reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import anyio
import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import pipeline
import video_repo
from extractor import ExtractionResult
from job_state import MAX_ATTEMPTS, InvalidTransition, JobState
from video_repo import ClaimLost


# ---------------------------------------------------------------------------
# fixtures and helpers
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
    from fastapi import BackgroundTasks

    captured = []

    def fake_add_task(self, func, *args, **kwargs):
        captured.append((func, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", fake_add_task, raising=False)
    return captured


def _upload(client, headers, filename="clip.mp4"):
    return client.post(
        "/upload-reel",
        files={"file": (filename, b"fake-video-bytes", "video/mp4")},
        headers=headers,
    )


def _queued(client, headers):
    """Upload and queue one job; return its video id."""
    body = _upload(client, headers).json()
    client.post(f"/analyze/{body['video_id']}", headers=headers)
    return body["video_id"]


def _extraction(frames=3):
    return ExtractionResult(
        video_id="vid",
        frames_stored=frames,
        duration_seconds=float(frames),
        fps=1,
        truncated=False,
        frame_timestamps=[float(i) for i in range(frames)],
    )


def _clean(frames=3, detections=0):
    return {
        "pending": frames,
        "audited": frames,
        "failed": 0,
        "detections": detections,
    }


def _run(fn, *args, **kwargs):
    return anyio.run(lambda: fn(*args, **kwargs))


def _doc(fake_db, video_id):
    """The raw stored document, including fields job_view hides."""
    for doc in fake_db["videos"].docs:
        if doc["video_id"] == video_id:
            return doc
    raise AssertionError(f"no record for {video_id}")


def _abandon(fake_db, video_id, *, seconds_ago=1):
    """Make a job look like one a dead worker left behind.

    A worker that dies stops heartbeating, so its lease simply lapses. That is
    the only thing the sweep inspects, so rewinding it is a faithful
    injection of "the process holding this job is gone".
    """
    doc = _doc(fake_db, video_id)
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    doc["lease_expires_at"] = past
    doc["updated_at"] = past
    return doc


def _claim(video_id, target=JobState.EXTRACTING, expected=JobState.QUEUED):
    return _run(video_repo.claim, video_id, target, expected=expected)


# ---------------------------------------------------------------------------
# claiming
# ---------------------------------------------------------------------------

def test_claim_moves_the_job_and_issues_a_lease(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)

    record, claim_id = _claim(vid)

    assert record["status"] == JobState.EXTRACTING.value
    assert claim_id
    assert record["claim_id"] == claim_id
    assert record["lease_expires_at"] > datetime.now(timezone.utc)


def test_a_second_worker_cannot_claim_a_live_lease(client, fake_db, upload_dir, alice, no_background):
    """The core mutual-exclusion property."""
    vid = _queued(client, alice)
    _claim(vid)

    with pytest.raises(InvalidTransition):
        _claim(vid, expected=JobState.QUEUED)


def test_claim_is_refused_when_the_job_is_not_queued(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()  # still UPLOADED

    with pytest.raises(InvalidTransition):
        _claim(body["video_id"])


def test_claim_succeeds_once_the_lease_has_expired(client, fake_db, upload_dir, alice, no_background):
    """An expired lease is the signal that lets a new worker take over."""
    vid = _queued(client, alice)
    _record, first_claim = _claim(vid)

    doc = _doc(fake_db, vid)
    doc["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    doc["status"] = JobState.QUEUED.value  # requeued by a sweep

    record, second_claim = _claim(vid)

    assert second_claim != first_claim
    assert record["claim_id"] == second_claim


def test_two_concurrent_claims_produce_exactly_one_winner(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)

    outcomes = []
    for _ in range(5):
        try:
            outcomes.append(_claim(vid)[1])
        except InvalidTransition:
            outcomes.append(None)

    assert sum(1 for o in outcomes if o) == 1, (
        "only one worker may ever hold a job"
    )


def test_a_legacy_record_with_no_lease_fields_can_be_claimed(fake_db, upload_dir):
    """Pre-4.1 records have no claim_id or lease at all."""
    collection = fake_db["videos"]
    collection.insert_raw({
        "video_id": "vid_legacy01",
        "owner": "alice",
        "stored_filename": "vid_legacy01.mp4",
        "status": "queued",
        "attempt": 1,
    })

    record, claim_id = _claim("vid_legacy01")

    assert record["status"] == JobState.EXTRACTING.value
    assert record["claim_id"] == claim_id


# ---------------------------------------------------------------------------
# heartbeats
# ---------------------------------------------------------------------------

def test_heartbeat_extends_the_lease(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _record, claim_id = _claim(vid)

    doc = _doc(fake_db, vid)
    doc["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=5)
    before = doc["lease_expires_at"]

    _run(video_repo.heartbeat, vid, claim_id)

    assert _doc(fake_db, vid)["lease_expires_at"] > before


def test_heartbeat_from_a_fenced_worker_is_refused(client, fake_db, upload_dir, alice, no_background):
    """A reclaimed worker must not be able to resurrect its lease."""
    vid = _queued(client, alice)
    _claim(vid)

    with pytest.raises(ClaimLost):
        _run(video_repo.heartbeat, vid, "a-claim-that-never-held-this-job")


def test_a_job_with_a_live_lease_is_not_considered_abandoned(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)

    assert _run(video_repo.find_expired_leases) == [], (
        "reclaiming a healthy slow job would let two workers run it at once"
    )


# ---------------------------------------------------------------------------
# fencing: a stale worker cannot corrupt results
# ---------------------------------------------------------------------------

def test_a_fenced_worker_cannot_transition_the_job(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _record, claim_id = _claim(vid)

    # A sweep reclaims it and a new worker takes over.
    _abandon(fake_db, vid)
    _run(video_repo.reclaim, _doc(fake_db, vid))
    _claim(vid)

    with pytest.raises(ClaimLost):
        _run(
            video_repo.transition,
            vid,
            JobState.ANALYZING,
            expected=JobState.EXTRACTING,
            claim_id=claim_id,
        )


def test_a_fenced_worker_cannot_complete_the_job(client, fake_db, upload_dir, alice, no_background):
    """The worst case: a zombie reporting success over a live attempt."""
    vid = _queued(client, alice)
    _record, stale_claim = _claim(vid)
    _run(
        video_repo.transition, vid, JobState.ANALYZING,
        expected=JobState.EXTRACTING, claim_id=stale_claim,
    )

    _abandon(fake_db, vid)
    _run(video_repo.reclaim, _doc(fake_db, vid))

    with pytest.raises(ClaimLost):
        _run(
            video_repo.transition, vid, JobState.COMPLETED,
            expected=JobState.ANALYZING, claim_id=stale_claim,
        )

    assert _doc(fake_db, vid)["status"] != JobState.COMPLETED.value


def test_a_fenced_worker_cannot_fail_the_job(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _record, stale_claim = _claim(vid)

    _abandon(fake_db, vid)
    _run(video_repo.reclaim, _doc(fake_db, vid))
    _claim(vid)  # the new worker is extracting

    pipeline._fail(_run, vid, stale_claim, "stale failure", stage="analysis")

    doc = _doc(fake_db, vid)
    assert doc["status"] == JobState.EXTRACTING.value, (
        "a zombie must not fail a job a live worker is running"
    )
    assert doc.get("error_summary") != "stale failure"


def test_a_fenced_workers_progress_counts_are_dropped(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _record, stale_claim = _claim(vid)

    _abandon(fake_db, vid)
    _run(video_repo.reclaim, _doc(fake_db, vid))
    _record2, live_claim = _claim(vid)

    _run(video_repo.record_progress, vid, claim_id=live_claim, frames_audited=5)
    _run(video_repo.record_progress, vid, claim_id=stale_claim, frames_audited=999)

    assert _doc(fake_db, vid)["frames_audited"] == 5


def test_an_interrupted_worker_abandons_the_run_mid_analysis(
    client, fake_db, upload_dir, alice, no_background
):
    """End-to-end fencing through `run_job`.

    The job is reclaimed while the analysis loop is running. The loop's
    heartbeat raises ClaimLost, the worker stops, and it writes nothing
    further - in particular it does not mark the job failed or completed.
    """
    vid = _queued(client, alice)

    def reclaim_midway(video_id=None, on_progress=None, attempt=None):
        # One frame's worth of work, then the job is taken away.
        _abandon(fake_db, vid)
        _run(video_repo.reclaim, _doc(fake_db, vid))
        on_progress(1, 0)  # raises ClaimLost
        raise AssertionError("the analysis loop should have aborted")

    with patch("extractor.extract_and_store", return_value=_extraction(3)), \
         patch("analyzer.check_pending_frames", side_effect=reclaim_midway):
        result = pipeline.run_job(vid)

    assert result.get("skipped") is True
    assert result["reason"] == "claim_lost"
    assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value, (
        "the reclaimed job must stay queued for the new worker"
    )


# ---------------------------------------------------------------------------
# finding abandoned jobs
# ---------------------------------------------------------------------------

def test_an_expired_lease_marks_the_job_abandoned(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    abandoned = _run(video_repo.find_expired_leases)

    assert [r["video_id"] for r in abandoned] == [vid]


def test_a_terminal_job_is_never_considered_abandoned(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=_clean(2)):
        pipeline.run_job(vid)

    doc = _doc(fake_db, vid)
    assert doc["status"] == JobState.COMPLETED.value
    doc["updated_at"] = datetime.now(timezone.utc) - timedelta(days=30)

    assert _run(video_repo.find_expired_leases) == []


def test_a_freshly_queued_job_is_not_considered_abandoned(client, fake_db, upload_dir, alice, no_background):
    """Otherwise a sweep would steal jobs from the worker about to claim them."""
    vid = _queued(client, alice)

    assert _run(video_repo.find_expired_leases) == []


def test_a_long_queued_job_with_no_worker_is_abandoned(client, fake_db, upload_dir, alice, no_background):
    """The request returned, then the process died before any worker claimed it."""
    vid = _queued(client, alice)
    doc = _doc(fake_db, vid)
    doc["updated_at"] = datetime.now(timezone.utc) - timedelta(hours=2)

    abandoned = _run(video_repo.find_expired_leases)

    assert [r["video_id"] for r in abandoned] == [vid]


def test_a_naive_legacy_timestamp_does_not_break_the_sweep(fake_db, upload_dir):
    """Comparing a naive datetime to an aware one raises.

    Old rows are exactly the ones recovery exists to rescue, so the sweep must
    tolerate them rather than crashing on the first one.
    """
    fake_db["videos"].insert_raw({
        "video_id": "vid_naive001",
        "owner": "alice",
        "status": "analyzing",
        "attempt": 1,
        # No tzinfo, as some drivers return.
        "lease_expires_at": datetime(2020, 1, 1, 0, 0, 0),
        "updated_at": datetime(2020, 1, 1, 0, 0, 0),
    })

    abandoned = _run(video_repo.find_expired_leases)

    assert [r["video_id"] for r in abandoned] == ["vid_naive001"]


# ---------------------------------------------------------------------------
# reclaiming
# ---------------------------------------------------------------------------

def test_reclaim_returns_an_abandoned_job_to_the_queue(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    updated = _run(video_repo.reclaim, _doc(fake_db, vid))

    assert updated["status"] == JobState.QUEUED.value
    assert updated["claim_id"] is None
    assert updated["lease_expires_at"] is None
    assert updated["recovered_count"] == 1


def test_reclaim_increments_the_attempt_count(client, fake_db, upload_dir, alice, no_background):
    """Recovery consumes the same retry budget as a user-requested retry.

    Otherwise a video that crashes the worker would be retried forever.
    """
    vid = _queued(client, alice)
    before = _doc(fake_db, vid)["attempt"]
    _claim(vid)
    _abandon(fake_db, vid)

    updated = _run(video_repo.reclaim, _doc(fake_db, vid))

    assert updated["attempt"] == before + 1


def test_reclaim_fails_a_job_that_has_exhausted_its_attempts(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)
    doc = _abandon(fake_db, vid)
    doc["attempt"] = MAX_ATTEMPTS

    updated = _run(video_repo.reclaim, doc)

    assert updated["status"] == JobState.FAILED.value
    assert "interrupted" in updated["error_summary"].lower()
    assert updated["failed_stage"] == "recovery"


def test_only_one_of_two_concurrent_sweeps_reclaims_a_job(client, fake_db, upload_dir, alice, no_background):
    """Both sweeps read the same record, as two schedulers would."""
    vid = _queued(client, alice)
    _claim(vid)
    snapshot = dict(_abandon(fake_db, vid))

    first = _run(video_repo.reclaim, snapshot)
    second = _run(video_repo.reclaim, snapshot)

    assert first is not None
    assert second is None, "a double reclaim would double-increment the attempt"
    assert _doc(fake_db, vid)["attempt"] == 2


def test_reclaim_does_nothing_if_the_worker_finished_first(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _record, claim_id = _claim(vid)
    snapshot = dict(_abandon(fake_db, vid))

    # The worker was alive after all and completed the job.
    _run(
        video_repo.transition, vid, JobState.ANALYZING,
        expected=JobState.EXTRACTING, claim_id=claim_id,
    )
    _run(
        video_repo.transition, vid, JobState.COMPLETED,
        expected=JobState.ANALYZING, claim_id=claim_id,
    )

    assert _run(video_repo.reclaim, snapshot) is None
    assert _doc(fake_db, vid)["status"] == JobState.COMPLETED.value


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def test_sweep_requeues_every_abandoned_job(client, fake_db, upload_dir, alice, no_background):
    ids = []
    for _ in range(3):
        vid = _queued(client, alice)
        _claim(vid)
        _abandon(fake_db, vid)
        ids.append(vid)

    counts = _run(pipeline.recover_abandoned_jobs)

    assert counts["scanned"] == 3
    assert counts["requeued"] == 3
    for vid in ids:
        assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value


def test_sweep_is_idempotent(client, fake_db, upload_dir, alice, no_background):
    """Running it repeatedly must not keep burning attempts.

    The first sweep leaves the job `queued` with a fresh `updated_at`, and a
    recently queued job is not abandoned, so the second sweep finds nothing.
    """
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    first = _run(pipeline.recover_abandoned_jobs)
    second = _run(pipeline.recover_abandoned_jobs)

    assert first["requeued"] == 1
    assert second["scanned"] == 0
    assert second["requeued"] == 0
    assert _doc(fake_db, vid)["attempt"] == 2


def test_sweep_leaves_healthy_and_terminal_jobs_alone(client, fake_db, upload_dir, alice, no_background):
    live = _queued(client, alice)
    _claim(live)

    done = _queued(client, alice)
    with patch("extractor.extract_and_store", return_value=_extraction(1)), \
         patch("analyzer.check_pending_frames", return_value=_clean(1)):
        pipeline.run_job(done)

    counts = _run(pipeline.recover_abandoned_jobs)

    assert counts["scanned"] == 0
    assert _doc(fake_db, live)["status"] == JobState.EXTRACTING.value
    assert _doc(fake_db, done)["status"] == JobState.COMPLETED.value


def test_sweep_fails_jobs_past_their_attempt_budget(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)
    doc = _abandon(fake_db, vid)
    doc["attempt"] = MAX_ATTEMPTS

    counts = _run(pipeline.recover_abandoned_jobs)

    assert counts["failed"] == 1
    assert counts["requeued"] == 0
    assert _doc(fake_db, vid)["status"] == JobState.FAILED.value


def test_sweep_dispatches_requeued_jobs_when_asked(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    dispatched = []
    _run(pipeline.recover_abandoned_jobs, dispatched.append)

    assert dispatched == [vid]


def test_sweep_without_a_dispatcher_only_requeues(client, fake_db, upload_dir, alice, no_background):
    """What the maintenance CLI does: change state, run nothing."""
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    with patch("extractor.extract_and_store") as extract:
        counts = _run(pipeline.recover_abandoned_jobs, None)

    assert counts["requeued"] == 1
    assert not extract.called


def test_a_dispatch_failure_does_not_lose_the_requeue(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    def boom(_video_id):
        raise RuntimeError("induced dispatch failure")

    counts = _run(pipeline.recover_abandoned_jobs, boom)

    assert counts["requeued"] == 1
    assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value


def test_sweep_honours_its_limit(client, fake_db, upload_dir, alice, no_background):
    for _ in range(4):
        vid = _queued(client, alice)
        _claim(vid)
        _abandon(fake_db, vid)

    counts = _run(pipeline.recover_abandoned_jobs, None, limit=2)

    assert counts["scanned"] == 4
    assert counts["requeued"] == 2


def test_sweep_survives_an_enumeration_failure(fake_db, upload_dir, monkeypatch):
    async def boom():
        raise RuntimeError("induced database failure")

    monkeypatch.setattr(video_repo, "find_expired_leases", boom)

    counts = _run(pipeline.recover_abandoned_jobs)

    assert counts == {"scanned": 0, "requeued": 0, "failed": 0, "skipped": 0}


def test_one_bad_record_does_not_abort_the_sweep(client, fake_db, upload_dir, alice, no_background):
    good = _queued(client, alice)
    _claim(good)
    _abandon(fake_db, good)

    calls = {"n": 0}
    real_reclaim = video_repo.reclaim

    async def flaky(record):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("induced reclaim failure")
        return await real_reclaim(record)

    bad = _queued(client, alice)
    _claim(bad)
    _abandon(fake_db, bad)

    with patch.object(video_repo, "reclaim", flaky):
        counts = _run(pipeline.recover_abandoned_jobs, None)

    assert counts["scanned"] == 2
    assert counts["skipped"] == 1
    assert counts["requeued"] == 1


# ---------------------------------------------------------------------------
# recovery actually re-runs the job
# ---------------------------------------------------------------------------

def test_a_recovered_job_runs_to_completion(client, fake_db, upload_dir, alice, no_background):
    """The whole point: an interrupted job finishes on the next sweep."""
    vid = _queued(client, alice)
    _claim(vid)  # a worker starts...
    _abandon(fake_db, vid)  # ...and dies

    dispatched = []
    _run(pipeline.recover_abandoned_jobs, dispatched.append)

    with patch("extractor.extract_and_store", return_value=_extraction(3)), \
         patch("analyzer.check_pending_frames", return_value=_clean(3, detections=2)):
        result = pipeline.run_job(dispatched[0])

    assert result["state"] == JobState.COMPLETED.value
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] == JobState.COMPLETED.value
    assert job["detections_recorded"] == 2
    assert job["recovered_count"] == 1


def test_recovery_is_visible_to_the_owner(client, fake_db, upload_dir, alice, no_background):
    """A user whose analysis restarted should be able to see that."""
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)
    _run(pipeline.recover_abandoned_jobs, None)

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["recovered_count"] == 1
    assert job["attempt"] == 2


def test_recovery_never_exposes_internal_lease_state(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    _claim(vid)

    response = client.get(f"/jobs/{vid}", headers=alice)

    assert "claim_id" not in response.text
    assert "lease_expires_at" not in response.text


def test_a_recovered_job_cannot_be_reached_by_another_user(
    client, fake_db, upload_dir, alice, mallory, no_background
):
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)
    _run(pipeline.recover_abandoned_jobs, None)

    assert client.get(f"/jobs/{vid}", headers=mallory).status_code == 404
    assert client.post(f"/analyze/{vid}/retry", headers=mallory).status_code == 404


# ---------------------------------------------------------------------------
# idempotency of a re-run
# ---------------------------------------------------------------------------

def test_a_rerun_replaces_counts_rather_than_summing_them(
    client, fake_db, upload_dir, alice, no_background
):
    vid = _queued(client, alice)
    with patch("extractor.extract_and_store", return_value=_extraction(3)), \
         patch("analyzer.check_pending_frames", return_value=_clean(3, detections=4)):
        pipeline.run_job(vid)

    assert client.get(f"/jobs/{vid}", headers=alice).json()["detections_recorded"] == 4

    client.post(f"/analyze/{vid}/retry", headers=alice)
    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=_clean(2, detections=1)):
        pipeline.run_job(vid)

    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["frames_audited"] == 2, "counts must be replaced, not accumulated"
    assert job["detections_recorded"] == 1


def test_a_shorter_rerun_removes_the_previous_attempts_extra_frames(
    client, fake_db, upload_dir, alice, no_background, fake_frames_table
):
    """Stale rows must not be presented as this attempt's results.

    Frame rows are keyed (video_id, frame_number), so a re-run that yields
    fewer frames overwrites 0..n-1 and leaves the earlier attempt's higher
    rows behind. /violations would return those as current findings.
    """
    vid = _queued(client, alice)
    for number in range(5):
        fake_frames_table.seed({
            "video_id": vid,
            "frame_number": number,
            "status": "audited",
            "detections": [],
        })

    with patch("extractor.extract_and_store", return_value=_extraction(5)), \
         patch("analyzer.check_pending_frames", return_value=_clean(5)):
        pipeline.run_job(vid)

    client.post(f"/analyze/{vid}/retry", headers=alice)
    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=_clean(2)):
        pipeline.run_job(vid)

    assert fake_frames_table.frame_numbers(vid) == [0, 1]


def test_a_first_attempt_never_prunes(client, fake_db, upload_dir, alice, no_background, fake_frames_table):
    """Nothing is stale on attempt 1, so no DynamoDB work is done."""
    vid = _queued(client, alice)

    with patch("extractor.extract_and_store", return_value=_extraction(2)), \
         patch("analyzer.check_pending_frames", return_value=_clean(2)):
        pipeline.run_job(vid)

    assert fake_frames_table.deleted == []


def test_a_failed_prune_does_not_report_a_successful_audit(
    client, fake_db, upload_dir, alice, no_background, fake_frames_table
):
    """Stale rows that could not be cleared must fail the job, not pass it."""
    vid = _queued(client, alice)
    with patch("extractor.extract_and_store", return_value=_extraction(3)), \
         patch("analyzer.check_pending_frames", return_value=_clean(3)):
        pipeline.run_job(vid)

    client.post(f"/analyze/{vid}/retry", headers=alice)
    fake_frames_table.fail_query = RuntimeError("induced DynamoDB failure")

    with patch("extractor.extract_and_store", return_value=_extraction(1)), \
         patch("analyzer.check_pending_frames", return_value=_clean(1)):
        result = pipeline.run_job(vid)

    assert result["state"] == JobState.FAILED.value
    job = client.get(f"/jobs/{vid}", headers=alice).json()
    assert job["state"] != JobState.COMPLETED.value
    assert "could not be cleared" in job["error_summary"].lower()


def test_a_prune_failure_message_carries_no_internal_detail(
    client, fake_db, upload_dir, alice, no_background, fake_frames_table
):
    vid = _queued(client, alice)
    with patch("extractor.extract_and_store", return_value=_extraction(3)), \
         patch("analyzer.check_pending_frames", return_value=_clean(3)):
        pipeline.run_job(vid)

    client.post(f"/analyze/{vid}/retry", headers=alice)
    fake_frames_table.fail_query = RuntimeError("table NovaFlowFrames arn:aws:dynamodb")

    with patch("extractor.extract_and_store", return_value=_extraction(1)), \
         patch("analyzer.check_pending_frames", return_value=_clean(1)):
        pipeline.run_job(vid)

    summary = client.get(f"/jobs/{vid}", headers=alice).json()["error_summary"]
    assert "arn:aws" not in summary
    assert "NovaFlowFrames" not in summary
    assert "RuntimeError" not in summary


# ---------------------------------------------------------------------------
# retry exhaustion
# ---------------------------------------------------------------------------

def test_retries_remain_bounded_across_recoveries(client, fake_db, upload_dir, alice, no_background):
    """Recovery must not become an infinite retry loop."""
    vid = _queued(client, alice)

    for _ in range(MAX_ATTEMPTS + 2):
        doc = _doc(fake_db, vid)
        if doc["status"] in (JobState.FAILED.value, JobState.COMPLETED.value):
            break
        try:
            _claim(vid)
        except InvalidTransition:
            pass
        _abandon(fake_db, vid)
        _run(pipeline.recover_abandoned_jobs, None)

    doc = _doc(fake_db, vid)
    assert doc["status"] == JobState.FAILED.value
    assert doc["attempt"] <= MAX_ATTEMPTS


def test_retry_is_refused_once_attempts_are_exhausted(client, fake_db, upload_dir, alice, no_background):
    vid = _queued(client, alice)
    doc = _doc(fake_db, vid)
    doc["status"] = JobState.FAILED.value
    doc["attempt"] = MAX_ATTEMPTS

    response = client.post(f"/analyze/{vid}/retry", headers=alice)

    assert response.status_code == 429
    assert str(MAX_ATTEMPTS) in response.json()["detail"]


# ---------------------------------------------------------------------------
# the startup sweep
# ---------------------------------------------------------------------------

def test_startup_recovery_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RECOVER_JOBS_ON_STARTUP", "false")
    assert app_module._recovery_on_startup() is False


def test_startup_recovery_is_on_by_default(monkeypatch):
    monkeypatch.delenv("RECOVER_JOBS_ON_STARTUP", raising=False)
    assert app_module._recovery_on_startup() is True


def test_startup_runs_the_sweep(client, fake_db, upload_dir, alice, monkeypatch, no_background):
    """The lifespan hook must actually call it, not just define it."""
    vid = _queued(client, alice)
    _claim(vid)
    _abandon(fake_db, vid)

    monkeypatch.setattr(app_module, "_dispatch_recovered_job", lambda _v: None)

    with TestClient(app_module.app):
        pass

    assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value


def test_a_startup_sweep_failure_does_not_stop_the_app(fake_db, upload_dir, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("induced sweep failure")

    monkeypatch.setattr(pipeline, "recover_abandoned_jobs", boom)

    with TestClient(app_module.app) as started:
        assert started.get("/status").status_code in (200, 401)
