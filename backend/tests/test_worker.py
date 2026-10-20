"""Phase 6: the durable worker, the queue read, retries, and health.

WHAT THESE TESTS DO AND DO NOT PROVE

Everything external is a deterministic local fake: no AWS, no Bedrock, no
FFmpeg, no real MongoDB. So these prove the *logic* - who may claim what, what
a retry does to the attempt budget, what the queue read returns, what the
probes report when a dependency is down. They do NOT prove that a real
deployment survives a real SIGKILL, and nothing here measures detection
accuracy.

"API restart" is injected the way the architecture actually makes it
survivable rather than by killing a process: the job record is the queue, so a
restart is indistinguishable from "a worker reads the queue it did not write".
`_fresh_worker` therefore stands in for a process that has no memory of the
submission - which is the property under test.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import anyio
import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import config
import pipeline
import video_repo
import worker as worker_module
from extractor import ExtractionResult
from job_state import JobState


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
def upload_dir(tmp_path, monkeypatch):
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", target.resolve())
    monkeypatch.setattr(pipeline, "_legacy_upload_dir", lambda: target.resolve())
    return target.resolve()


@pytest.fixture
def no_background(monkeypatch):
    """Stop inline execution so the worker is the only thing that runs jobs."""
    def fake_add_task(self, func, *args, **kwargs):
        return None

    from fastapi import BackgroundTasks

    monkeypatch.setattr(BackgroundTasks, "add_task", fake_add_task, raising=False)
    monkeypatch.setattr(
        "starlette.background.BackgroundTasks.add_task", fake_add_task, raising=False
    )


def _run(fn, *args, **kwargs):
    return anyio.run(lambda: fn(*args, **kwargs))


def _upload(client, headers):
    return client.post(
        "/upload-reel",
        files={"file": ("clip.mp4", b"x" * 2048, "video/mp4")},
        headers=headers,
    )


def _queued(client, headers):
    body = _upload(client, headers).json()
    client.post(f"/analyze/{body['video_id']}", headers=headers)
    return body["video_id"]


def _doc(fake_db, video_id):
    for doc in fake_db["videos"].docs:
        if doc["video_id"] == video_id:
            return doc
    raise AssertionError(f"no record for {video_id}")


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
    return {"pending": frames, "audited": frames, "failed": 0, "detections": detections}


def _fresh_worker(**kwargs):
    """A worker with no knowledge of how its jobs were submitted.

    Stands in for a process started after an API restart: it learns about work
    only by reading the queue.
    """
    kwargs.setdefault("poll_seconds", 1)
    kwargs.setdefault("recovery_seconds", 0)
    return worker_module.Worker(**kwargs)


def _succeeding_pipeline(frames=3, detections=1):
    """Patch the two external stages so a claimed job runs to completion."""
    return (
        patch("extractor.extract_and_store", return_value=_extraction(frames)),
        patch("analyzer.check_pending_frames", return_value=_clean(frames, detections)),
    )


def _abandon(fake_db, video_id, *, seconds_ago=1):
    doc = _doc(fake_db, video_id)
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    doc["lease_expires_at"] = past
    doc["updated_at"] = past
    return doc


# ---------------------------------------------------------------------------
# durable acceptance: the queue is the job record
# ---------------------------------------------------------------------------

def test_accepted_job_is_persisted_before_the_response(
    client, fake_db, upload_dir, alice, no_background
):
    """Acceptance must be durable at the moment the API answers.

    Nothing was handed to a background task here (`no_background`), so if the
    response is a promise held in process memory the job is simply lost. It
    must already be in the queue.
    """
    vid = _queued(client, alice)

    assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value
    assert _run(video_repo.find_queued_jobs, 10)[0]["video_id"] == vid


def test_a_worker_that_never_saw_the_request_runs_the_job(
    client, fake_db, upload_dir, alice, no_background
):
    """The API-restart property, stated as what actually makes it true.

    The worker is constructed after submission and shares no state with the
    request that accepted it. If acceptance depended on in-process handoff
    this would find nothing to do.
    """
    vid = _queued(client, alice)

    extract, analyse = _succeeding_pipeline()
    with extract, analyse:
        assert _fresh_worker().run_once() is True

    assert _doc(fake_db, vid)["status"] == JobState.COMPLETED.value


def test_queue_write_failure_is_not_reported_as_accepted(
    client, fake_db, upload_dir, alice, no_background, monkeypatch
):
    """If the job cannot be queued, the caller must not be told it was."""
    body = _upload(client, alice).json()

    async def boom(*_a, **_k):
        raise RuntimeError("queue write failed")

    monkeypatch.setattr(pipeline, "submit", boom)
    response = client.post(f"/analyze/{body['video_id']}", headers=alice)

    assert response.status_code >= 500
    assert "queue write failed" not in response.text
    assert _doc(fake_db, body["video_id"])["status"] == JobState.UPLOADED.value


def test_the_api_does_not_execute_the_job_in_worker_mode(
    client, fake_db, upload_dir, alice, monkeypatch
):
    """In worker mode nothing runs in the request process.

    Asserted by leaving BackgroundTasks REAL and patching the stage functions:
    if the API dispatched inline, extraction would have been called.
    """
    monkeypatch.setattr(app_module.settings, "job_execution_mode", config.WORKER_MODE)

    with patch("extractor.extract_and_store") as extract:
        vid = _queued(client, alice)

    assert not extract.called, "the API ran the pipeline itself in worker mode"
    assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value


def test_inline_mode_still_hands_the_job_to_a_background_task(
    client, fake_db, upload_dir, alice, monkeypatch
):
    """The development mode is preserved, and is genuinely different."""
    monkeypatch.setattr(app_module.settings, "job_execution_mode", config.INLINE_MODE)

    captured = []

    def fake_add_task(self, func, *args, **kwargs):
        captured.append((func, args))

    from fastapi import BackgroundTasks

    monkeypatch.setattr(BackgroundTasks, "add_task", fake_add_task, raising=False)
    vid = _queued(client, alice)

    assert [args for _fn, args in captured if args == (vid,)], (
        "inline mode must schedule the job in-process"
    )


def test_the_response_contract_is_unchanged_by_the_mode(
    client, fake_db, upload_dir, alice, monkeypatch
):
    """The frontend must not be able to tell which executor is configured."""
    shapes = {}
    for mode in (config.WORKER_MODE, config.INLINE_MODE):
        monkeypatch.setattr(app_module.settings, "job_execution_mode", mode)
        with patch("extractor.extract_and_store", return_value=_extraction(1)), \
             patch("analyzer.check_pending_frames", return_value=_clean(1)):
            body = _upload(client, alice).json()
            response = client.post(f"/analyze/{body['video_id']}", headers=alice)
        assert response.status_code == 200
        shapes[mode] = sorted(response.json())

    assert shapes[config.WORKER_MODE] == shapes[config.INLINE_MODE]
    assert "job" in shapes[config.WORKER_MODE]
    assert "status" in shapes[config.WORKER_MODE]


# ---------------------------------------------------------------------------
# the queue read
# ---------------------------------------------------------------------------

def test_the_queue_returns_oldest_first(client, fake_db, upload_dir, alice, no_background):
    """Oldest first, so a steady arrival rate cannot starve an early job."""
    first = _queued(client, alice)
    second = _queued(client, alice)

    older = datetime.now(timezone.utc) - timedelta(minutes=5)
    _doc(fake_db, first)["updated_at"] = older

    ready = _run(video_repo.find_queued_jobs, 10)
    assert [r["video_id"] for r in ready][:2] == [first, second]


def test_the_queue_excludes_jobs_waiting_on_a_backoff(
    client, fake_db, upload_dir, alice, no_background
):
    """A job in its retry backoff is not yet claimable."""
    vid = _queued(client, alice)
    _doc(fake_db, vid)["not_before"] = datetime.now(timezone.utc) + timedelta(minutes=10)

    assert _run(video_repo.find_queued_jobs, 10) == []


def test_the_queue_includes_a_job_whose_backoff_has_passed(
    client, fake_db, upload_dir, alice, no_background
):
    vid = _queued(client, alice)
    _doc(fake_db, vid)["not_before"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert [r["video_id"] for r in _run(video_repo.find_queued_jobs, 10)] == [vid]


def test_the_queue_excludes_non_queued_jobs(
    client, fake_db, upload_dir, alice, no_background
):
    """Only `queued` is claimable - never a running or terminal job."""
    vid = _queued(client, alice)
    for state in (
        JobState.EXTRACTING, JobState.ANALYZING, JobState.COMPLETED, JobState.FAILED,
    ):
        _doc(fake_db, vid)["status"] = state.value
        assert _run(video_repo.find_queued_jobs, 10) == [], state


def test_the_queue_read_honours_its_limit(
    client, fake_db, upload_dir, alice, no_background
):
    for _ in range(4):
        _queued(client, alice)
    assert len(_run(video_repo.find_queued_jobs, 2)) == 2


# ---------------------------------------------------------------------------
# concurrency: two workers, one job
# ---------------------------------------------------------------------------

def test_two_workers_racing_produce_exactly_one_run(
    client, fake_db, upload_dir, alice, no_background
):
    """Both read the same queue; the conditional claim resolves it."""
    vid = _queued(client, alice)
    a, b = _fresh_worker(), _fresh_worker()

    extract, analyse = _succeeding_pipeline()
    with extract as extract_mock, analyse:
        first = a.run_once()
        second = b.run_once()

    assert first is True
    assert second is False, "the loser must not re-run a claimed job"
    assert extract_mock.call_count == 1
    assert _doc(fake_db, vid)["status"] == JobState.COMPLETED.value


def test_concurrent_workers_in_threads_run_a_job_once(
    client, fake_db, upload_dir, alice, no_background
):
    """The same property under real thread interleaving rather than in turns."""
    vid = _queued(client, alice)
    results = []

    extract, analyse = _succeeding_pipeline()
    with extract as extract_mock, analyse:
        def go():
            results.append(_fresh_worker().run_once())

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert sum(1 for r in results if r) == 1, f"expected one winner, got {results}"
    assert extract_mock.call_count == 1
    assert _doc(fake_db, vid)["status"] == JobState.COMPLETED.value


def test_a_duplicate_run_of_a_completed_job_does_nothing(
    client, fake_db, upload_dir, alice, no_background
):
    """Duplicate delivery is safe: a terminal job is not re-executed.

    This is the property that makes at-least-once delivery acceptable - the
    equivalent of a queue redelivering a message whose work already finished.
    """
    vid = _queued(client, alice)
    extract, analyse = _succeeding_pipeline()
    with extract, analyse:
        _fresh_worker().run_once()

    doc_before = dict(_doc(fake_db, vid))

    with patch("extractor.extract_and_store") as extract_again:
        result = pipeline.run_job(vid)

    assert result["skipped"] is True
    assert not extract_again.called
    assert _doc(fake_db, vid)["status"] == doc_before["status"]
    assert _doc(fake_db, vid)["attempt"] == doc_before["attempt"]


def test_a_stale_worker_cannot_write_results_after_being_reclaimed(
    client, fake_db, upload_dir, alice, no_background
):
    """The fencing property, through the worker rather than the repo API."""
    vid = _queued(client, alice)
    _record, claim_id = _run(
        video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED
    )

    # A sweep decides this worker is gone and returns the job to the queue.
    _abandon(fake_db, vid)
    assert _run(video_repo.reclaim, _doc(fake_db, vid)) is not None

    from video_repo import ClaimLost

    with pytest.raises(ClaimLost):
        _run(
            video_repo.transition, vid, JobState.COMPLETED,
            expected=JobState.ANALYZING, claim_id=claim_id,
        )
    assert _doc(fake_db, vid)["status"] == JobState.QUEUED.value


# ---------------------------------------------------------------------------
# retries, backoff and idempotency
# ---------------------------------------------------------------------------

def test_retry_delay_grows_and_is_capped():
    """Exponential, then flat - never unbounded, never zero by accident."""
    settings = config.get_settings()
    first = pipeline.retry_delay_seconds(1)
    second = pipeline.retry_delay_seconds(2)

    assert first == settings.retry_backoff_seconds
    assert second == min(first * 2, settings.retry_backoff_cap_seconds)
    assert pipeline.retry_delay_seconds(50) == settings.retry_backoff_cap_seconds


def test_a_scheduled_retry_increments_the_attempt_and_sets_a_backoff(
    client, fake_db, upload_dir, alice, no_background
):
    vid = _queued(client, alice)
    _record, claim_id = _run(
        video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED
    )
    before = _doc(fake_db, vid)["attempt"]

    updated = _run(
        video_repo.schedule_retry, vid,
        claim_id=claim_id, delay_seconds=42, summary="dependency unavailable",
    )

    assert updated["status"] == JobState.QUEUED.value
    assert updated["attempt"] == before + 1
    assert _doc(fake_db, vid)["not_before"] > datetime.now(timezone.utc)
    assert _doc(fake_db, vid)["last_retry_reason"] == "dependency unavailable"


def test_a_fenced_worker_cannot_schedule_a_retry(
    client, fake_db, upload_dir, alice, no_background
):
    """A superseded attempt must not push the job back into the queue."""
    vid = _queued(client, alice)
    _record, claim_id = _run(
        video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED
    )
    _abandon(fake_db, vid)
    _run(video_repo.reclaim, _doc(fake_db, vid))
    attempt_after_reclaim = _doc(fake_db, vid)["attempt"]

    assert _run(
        video_repo.schedule_retry, vid,
        claim_id=claim_id, delay_seconds=10, summary="late",
    ) is None
    assert _doc(fake_db, vid)["attempt"] == attempt_after_reclaim


def test_a_backoff_longer_than_the_lease_is_not_treated_as_abandoned(
    client, fake_db, upload_dir, alice, no_background
):
    """The interaction that would otherwise double-count one failure.

    The backoff cap (600s) exceeds the lease period (300s), so a job waiting
    out a long retry looks "untouched for more than a lease" to the recovery
    sweep. If the sweep reclaimed it, `attempt` would increment twice for a
    single failure and the budget would burn at double rate.
    """
    vid = _queued(client, alice)
    doc = _doc(fake_db, vid)
    doc["updated_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=video_repo.lease_seconds() * 3
    )
    doc["not_before"] = datetime.now(timezone.utc) + timedelta(minutes=5)

    assert _run(video_repo.find_expired_leases) == []


def test_a_rerun_overwrites_frame_rows_rather_than_duplicating_them(
    client, fake_db, upload_dir, alice, no_background, fake_frames_table
):
    """Idempotent result persistence, which is what makes a re-run safe.

    Frame rows are keyed (video_id, frame_number), so running the same job
    twice must leave the same number of rows - not two sets of detections for
    one video.
    """
    vid = _queued(client, alice)

    def extract_real(path, video_id, **kwargs):
        for number in range(3):
            fake_frames_table.put_item(
                Item={
                    "video_id": video_id,
                    "frame_number": number,
                    "status": "pending_audit",
                }
            )
        return _extraction(3)

    with patch("extractor.extract_and_store", side_effect=extract_real), \
         patch("analyzer.check_pending_frames", return_value=_clean(3)):
        _fresh_worker().run_once()
        first = len(fake_frames_table.items)

        client.post(f"/analyze/{vid}/retry", headers=alice)
        _fresh_worker().run_once()

    assert len(fake_frames_table.items) == first, (
        "a re-run must overwrite frame rows, not append a second set"
    )


# ---------------------------------------------------------------------------
# recovery through the worker
# ---------------------------------------------------------------------------

def test_the_worker_sweep_requeues_an_abandoned_job(
    client, fake_db, upload_dir, alice, no_background
):
    """Recovery is continuous while a worker runs, not startup-only."""
    vid = _queued(client, alice)
    _run(video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED)
    _abandon(fake_db, vid)

    extract, analyse = _succeeding_pipeline()
    with extract, analyse:
        # recovery_seconds>0 so the sweep runs on this very first iteration.
        _fresh_worker(recovery_seconds=1).run_once()

    assert _doc(fake_db, vid)["status"] in {
        JobState.QUEUED.value, JobState.COMPLETED.value
    }
    assert _doc(fake_db, vid)["recovered_count"] >= 1


def test_the_sweep_does_not_disturb_a_healthy_running_job(
    client, fake_db, upload_dir, alice, no_background
):
    """A live lease must survive a sweep racing with it."""
    vid = _queued(client, alice)
    _record, claim_id = _run(
        video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED
    )

    _fresh_worker(recovery_seconds=1).run_once()

    doc = _doc(fake_db, vid)
    assert doc["status"] == JobState.EXTRACTING.value
    assert doc["claim_id"] == claim_id, "a healthy worker was fenced off by a sweep"


def test_a_disabled_sweep_does_not_run(
    client, fake_db, upload_dir, alice, no_background
):
    vid = _queued(client, alice)
    _run(video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED)
    _abandon(fake_db, vid)

    _fresh_worker(recovery_seconds=0).run_once()

    assert _doc(fake_db, vid)["status"] == JobState.EXTRACTING.value


# ---------------------------------------------------------------------------
# worker loop behaviour
# ---------------------------------------------------------------------------

def test_an_empty_queue_is_not_an_error(fake_db, upload_dir):
    w = _fresh_worker()
    assert w.run_once() is False
    assert w.state.snapshot()["consecutive_poll_errors"] == 0


def test_a_queue_read_failure_is_recorded_and_does_not_raise(
    fake_db, upload_dir, monkeypatch
):
    """A database blip must not kill the loop."""
    async def boom(*_a, **_k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(video_repo, "find_queued_jobs", boom)
    w = _fresh_worker()

    assert w.run_once() is False
    assert w.state.snapshot()["consecutive_poll_errors"] == 1


def test_an_unexpected_job_error_does_not_kill_the_loop(
    client, fake_db, upload_dir, alice, no_background, monkeypatch
):
    """run_job is documented never to raise; a breach must still be contained."""
    _queued(client, alice)
    monkeypatch.setattr(
        pipeline, "run_job", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    w = _fresh_worker()

    assert w.run_once() is True, "the iteration must complete, not propagate"
    assert w.state.snapshot()["jobs_failed"] == 1


def test_stop_ends_run_forever(client, fake_db, upload_dir, alice, no_background):
    """Graceful shutdown: the loop exits rather than being killed."""
    _queued(client, alice)
    w = _fresh_worker(poll_seconds=1)

    extract, analyse = _succeeding_pipeline()
    with extract, analyse:
        def stop_soon():
            # Let one iteration happen, then ask for a stop.
            w.stop()

        timer = threading.Timer(0.2, stop_soon)
        timer.start()
        final = w.run_forever()
        timer.cancel()

    assert final["stopping"] is True
    assert w.stopping is True


def test_stopping_prevents_claiming_further_jobs(
    client, fake_db, upload_dir, alice, no_background
):
    """A stopping worker must not pick up new work it cannot finish."""
    _queued(client, alice)
    w = _fresh_worker()
    w.stop()

    with patch("extractor.extract_and_store") as extract:
        assert w.run_once() is False
    assert not extract.called


def test_worker_state_exposes_counts_not_user_data(
    client, fake_db, upload_dir, alice, no_background
):
    """The snapshot is safe to log: counters, timestamps, and an opaque id."""
    vid = _queued(client, alice)
    extract, analyse = _succeeding_pipeline()
    with extract, analyse:
        _fresh_worker().run_once()

    snapshot = _fresh_worker().state.snapshot()
    rendered = repr(snapshot)
    assert "alice" not in rendered
    assert "clip.mp4" not in rendered
    assert snapshot["current_video_id"] is None
    assert set(snapshot) >= {"jobs_started", "jobs_completed", "jobs_failed", "polls"}
    assert vid  # the job ran; its id is opaque and server-generated


# ---------------------------------------------------------------------------
# health and readiness
# ---------------------------------------------------------------------------

def test_liveness_does_not_depend_on_the_database(client, upload_dir, monkeypatch):
    """A dependency outage must not make every replica fail liveness.

    If it did, a brief MongoDB blip would get every pod killed and restarted,
    converting degradation into guaranteed downtime.
    """
    async def boom():
        raise RuntimeError("mongo down")

    monkeypatch.setattr(app_module, "_check_mongodb", boom)
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_is_ready_when_the_database_answers(client, fake_db, upload_dir):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert all(check["ok"] for check in body["checks"])


def test_readiness_reports_not_ready_when_the_database_is_down(
    client, fake_db, upload_dir
):
    """Induced through the database itself, so the real check runs.

    `_check_mongodb` is not patched here: the fake's ping raises, exactly as
    an unreachable server would, so this exercises the production code path
    rather than a stub standing in for it.
    """
    fake_db.fail_command = RuntimeError("connection refused")
    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    mongo = [c for c in body["checks"] if c["name"] == "mongodb"][0]
    assert mongo["ok"] is False


def test_readiness_times_out_rather_than_hanging(
    client, fake_db, upload_dir, monkeypatch
):
    """A hung dependency must still produce a bounded response."""
    async def hang():
        await anyio.sleep(30)

    monkeypatch.setattr(app_module, "_check_mongodb", hang)
    monkeypatch.setattr(app_module.settings, "readiness_timeout_seconds", 1)

    response = client.get("/health/ready")

    assert response.status_code == 503
    mongo = [c for c in response.json()["checks"] if c["name"] == "mongodb"][0]
    assert mongo["detail"] == "timed out"


def test_readiness_never_leaks_dependency_detail(
    client, fake_db, upload_dir, monkeypatch
):
    """Probe endpoints are usually unauthenticated, so they must say little."""
    secretish = "mongodb://user:hunter2@cluster.example.net/novaflow"

    async def boom():
        raise RuntimeError(f"connection to {secretish} refused")

    monkeypatch.setattr(app_module, "_check_mongodb", boom)
    response = client.get("/health/ready")

    assert "hunter2" not in response.text
    assert "cluster.example.net" not in response.text
    assert "RuntimeError" not in response.text


def test_worker_health_reports_queue_counts(
    client, fake_db, upload_dir, alice, no_background
):
    _queued(client, alice)
    response = client.get("/health/worker")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"][JobState.QUEUED.value] == 1
    assert body["abandoned"] == 0
    assert body["status"] == "ok"


def test_worker_health_is_degraded_when_jobs_are_abandoned(
    client, fake_db, upload_dir, alice, no_background
):
    """Abandoned jobs are the signal that a worker died."""
    vid = _queued(client, alice)
    _run(video_repo.claim, vid, JobState.EXTRACTING, expected=JobState.QUEUED)
    _abandon(fake_db, vid)

    response = client.get("/health/worker")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["abandoned"] == 1


def test_worker_health_reports_unknown_when_the_store_is_unreachable(
    client, fake_db, upload_dir, monkeypatch
):
    async def boom():
        raise RuntimeError("mongo down")

    monkeypatch.setattr(video_repo, "count_by_state", boom)
    response = client.get("/health/worker")

    assert response.status_code == 503
    assert response.json()["status"] == "unknown"
    assert "mongo down" not in response.text


def test_worker_health_exposes_no_user_data(
    client, fake_db, upload_dir, alice, no_background
):
    _queued(client, alice)
    response = client.get("/health/worker")

    assert "alice" not in response.text
    assert "clip.mp4" not in response.text
    assert "claim_id" not in response.text


# ---------------------------------------------------------------------------
# the job-state count helper
# ---------------------------------------------------------------------------

def test_count_by_state_covers_every_state(
    client, fake_db, upload_dir, alice, no_background
):
    _queued(client, alice)
    counts = _run(video_repo.count_by_state)

    assert set(counts) == {state.value for state in JobState}
    assert counts[JobState.QUEUED.value] == 1
    assert counts[JobState.COMPLETED.value] == 0


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def test_job_execution_mode_defaults_by_environment(monkeypatch):
    """Production must not silently depend on in-process execution."""
    config.reset_settings_cache()
    monkeypatch.delenv("JOB_EXECUTION_MODE", raising=False)

    monkeypatch.setenv("NOVAFLOW_ENV", "development")
    assert config.load_job_execution_mode() == config.INLINE_MODE

    monkeypatch.setenv("NOVAFLOW_ENV", "production")
    assert config.load_job_execution_mode() == config.WORKER_MODE


def test_an_unknown_job_execution_mode_is_rejected(monkeypatch):
    config.reset_settings_cache()
    monkeypatch.setenv("JOB_EXECUTION_MODE", "sqs")
    with pytest.raises(config.ConfigurationError):
        config.load_job_execution_mode()


def test_aws_clients_are_built_with_finite_timeouts():
    """No client may be constructed without a bound (Phase 6)."""
    import aws_clients

    cfg = aws_clients.client_config()
    assert cfg.connect_timeout and cfg.connect_timeout > 0
    assert cfg.read_timeout and cfg.read_timeout > 0
    assert cfg.retries["max_attempts"] >= 1
    assert cfg.retries["mode"] == "standard"


def test_no_module_builds_an_unconfigured_boto3_client():
    """Every client must come through aws_clients.

    A direct `boto3.client(...)` elsewhere would reintroduce the unbounded
    call that makes a lease meaningless.
    """
    import re
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parent.parent
    allowed = {"aws_clients.py", "safety.py"}
    offenders = []
    for path in sorted(backend_dir.glob("*.py")):
        if path.name in allowed:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"boto3\.(client|resource)\(", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"unconfigured boto3 clients: {offenders}"
