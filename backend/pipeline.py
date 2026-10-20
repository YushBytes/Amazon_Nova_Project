"""Pipeline orchestration: upload -> extract -> analyse -> persisted result.

Phase 4 built the state machine. Phase 4.1 made its execution survivable:

* the source video is fetched from S3 rather than assumed to be on this host's
  local disk, so any worker on any host can run any job;
* a worker takes a **lease** on the job and every later write is fenced on the
  claim it won, so a worker that was reclaimed cannot corrupt a newer attempt;
* `recover_abandoned_jobs()` finds jobs whose lease lapsed and returns them to
  the queue (or fails them once attempts are exhausted).

STAGES

    submit()                    UPLOADED/COMPLETED/FAILED -> QUEUED
    run_job()  claim            QUEUED     -> EXTRACTING   (+ lease)
               stage 1          download source, extract frames
               stage 2          EXTRACTING -> ANALYZING    (fenced)
               stage 3          ANALYZING  -> COMPLETED | FAILED (fenced)

WHAT CRASH RECOVERY DOES AND DOES NOT DO
----------------------------------------
Execution still uses FastAPI's in-process `BackgroundTasks`, which is what
this deployment has. The honest scope of the recovery mechanism:

* **It is not continuous.** `recover_abandoned_jobs()` runs once at
  application startup and whenever an operator invokes
  `python backend/recover_jobs.py`. A job abandoned while the process stays up
  - a killed thread, say - is not noticed until the next sweep. Continuous
  recovery needs a recurring execution mechanism; the required deployment
  configuration is in docs/PIPELINE_OPERATIONS.md, and it is NOT claimed to be
  running.
* **A single-instance restart recovers on boot.** Because the source video is
  in S3 and the lease is in the database, the recovered run is a genuine
  re-run, not a resume: it re-extracts and re-analyses from the start.
* **It never resets a terminal job**, and it never touches a job whose lease
  is still live, so a slow-but-healthy run is left alone.
* **No backpressure.** Submitting many videos at once runs many background
  tasks in the server's threadpool.

Moving to a durable queue (SQS, Celery, Step Functions) remains the real fix
for throughput and is deliberately out of scope here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import video_repo
from job_state import MAX_ATTEMPTS, InvalidTransition, JobState, coerce
from video_repo import ClaimLost, OwnershipUnknown

logger = logging.getLogger(__name__)

# A job in a non-terminal state with no update for this long is very likely
# orphaned. Used by the reporting helper `find_stuck_jobs`; the recovery sweep
# uses the lease instead, which is a far better signal.
STUCK_AFTER = timedelta(minutes=30)

# Upper bound on how many jobs one recovery sweep will touch, so a database
# full of abandoned jobs cannot make startup take unbounded time.
MAX_RECOVERIES_PER_SWEEP = 25


class PipelineError(RuntimeError):
    """A pipeline operation could not be performed.

    The message is safe for a client: it never carries an exception string,
    credential, bucket name, or path.
    """


class AlreadyRunning(PipelineError):
    """Analysis is already in flight for this video."""


class RetryLimitReached(PipelineError):
    """The job has used its attempt budget."""


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

async def submit(video_id: str, *, allow_retry: bool = True) -> dict:
    """Move a job to QUEUED so a worker may pick it up.

    Idempotency and duplicate protection:

    * a job already QUEUED/EXTRACTING/ANALYZING raises AlreadyRunning, so a
      double-clicked button cannot start two runs;
    * a COMPLETED or FAILED job is re-queued only when `allow_retry` is set,
      and only while attempts remain;
    * re-queuing bumps `attempt`, clears the previous `error_summary`, and
      releases any lease, so a stale failure is never read as current.

    Returns:
        The updated record.
    """
    record = await video_repo.get_video(video_id)
    state = coerce(record.get("status"))

    if state.is_active:
        raise AlreadyRunning("Analysis is already running for this video.")

    if state.is_terminal:
        if not allow_retry:
            raise PipelineError("This analysis has already finished.")
        attempt = int(record.get("attempt") or 0)
        if attempt >= MAX_ATTEMPTS:
            raise RetryLimitReached(
                f"This video has already been analysed {attempt} time(s), "
                f"which is the maximum of {MAX_ATTEMPTS}."
            )

    try:
        return await video_repo.transition(video_id, JobState.QUEUED, expected=state)
    except InvalidTransition as exc:
        # Another request won the race between our read and our write.
        raise AlreadyRunning("Analysis was started by another request.") from exc


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_job(video_id: str, file_path: str | None = None) -> dict:
    """Execute the pipeline for one queued video.

    Synchronous by design: it is handed to `BackgroundTasks`, which runs sync
    callables in a worker thread. The database calls inside are async, so each
    is driven with `anyio.run` on this thread.

    Args:
        video_id: the queued job to run.
        file_path: an already-local source file. Normally omitted - the source
            is fetched from S3 using the record's stored reference, which is
            what makes a job runnable on a host that never saw the upload.
            Supplying a path is used by the legacy local-disk path and by
            tests that provide their own fixture file.

    Never raises. Every failure path persists FAILED with a safe summary,
    because an exception escaping a background task would leave the job
    non-terminal forever.

    Returns:
        A summary dict for logging and tests.
    """
    import anyio

    def sync(coro_fn, *args, **kwargs):
        return anyio.run(lambda: coro_fn(*args, **kwargs))

    logger.info("Pipeline starting for %s", video_id)

    # --- claim the job, taking a lease -----------------------------------
    try:
        record, claim_id = sync(
            video_repo.claim,
            video_id,
            JobState.EXTRACTING,
            expected=JobState.QUEUED,
        )
    except InvalidTransition:
        # Not ours to run: another worker holds a live lease, or the job is
        # not queued. Do not touch its state.
        logger.warning("Pipeline skipped for %s: job was not claimable", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "not_claimable"}
    except OwnershipUnknown:
        logger.warning("Pipeline skipped for %s: no such video", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "unknown_video"}
    except Exception:
        logger.exception("Pipeline could not claim %s", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "claim_failed"}

    try:
        return _run_claimed(sync, video_id, record, claim_id, file_path)
    except Exception:
        # Belt and braces: nothing below should escape, but a background task
        # that raises would strand the job non-terminal.
        logger.exception("Unhandled pipeline failure for %s", video_id)
        _fail(sync, video_id, claim_id, "Analysis failed.", stage="unknown")
        return {"video_id": video_id, "state": JobState.FAILED.value}


def _run_claimed(sync, video_id, record, claim_id, file_path):
    """Stages 1-3, with the lease already held."""
    import source_store
    from extractor import ExtractionError, extract_and_store

    failed = {"video_id": video_id, "state": JobState.FAILED.value}
    # The attempt this run represents. Read before stage 1 because a retryable
    # failure in stage 1 needs it to decide between requeueing and failing.
    attempt = int(record.get("attempt") or 0)

    # --- stage 1: fetch the source and extract ---------------------------
    # `materialise` yields a local path - downloaded from S3 into a unique
    # temporary directory, or the legacy local file - and removes anything it
    # created when the block exits, on success or failure.
    def extract_with_source():
        if file_path is not None:
            return extract_and_store(file_path, video_id)
        upload_dir = _legacy_upload_dir()
        with source_store.materialise(record, upload_dir) as local_path:
            return extract_and_store(local_path, video_id)

    try:
        extraction = extract_with_source()
    except source_store.SourceMissing as exc:
        # Permanent: no amount of retrying conjures the object back.
        _fail(sync, video_id, claim_id, str(exc), stage="source")
        return failed
    except source_store.SourceStoreError as exc:
        # `retryable` is set at the raise site: True for transport failures,
        # False for deterministic ones like an object that breaches the size
        # ceiling, which would fail identically on every attempt.
        if getattr(exc, "retryable", False):
            return _retry_or_fail(
                sync, video_id, claim_id, attempt, str(exc), stage="source"
            )
        _fail(sync, video_id, claim_id, str(exc), stage="source")
        return failed
    except ExtractionError as exc:
        # ExtractionError messages are written to be shown to the owner.
        _fail(sync, video_id, claim_id, str(exc), stage="extraction")
        return failed
    except Exception:
        logger.exception("Unexpected extraction failure for %s", video_id)
        _fail(sync, video_id, claim_id, "Frame extraction failed.", stage="extraction")
        return failed

    # Drop frame rows left by a longer previous attempt.
    #
    # Only on a re-run: on attempt 1 there is nothing stale by construction,
    # so pruning would be a pointless pair of DynamoDB calls on the common
    # path. Done only now that the new extraction has succeeded, so a failed
    # retry never destroys the results a previous attempt established.
    if attempt > 1:
        try:
            removed = _prune_stale_frames(video_id, extraction.frames_stored)
            if removed:
                logger.info(
                    "Removed %d stale frame row(s) from an earlier attempt of %s",
                    removed, video_id,
                )
        except Exception:
            # Stale rows beyond the current frame count would be returned as
            # part of this attempt's results, so this is not cosmetic.
            logger.exception("Could not prune stale frames for %s", video_id)
            _fail(
                sync, video_id, claim_id,
                "Previous results could not be cleared before re-analysing.",
                stage="extraction",
            )
            return failed

    try:
        sync(
            video_repo.transition,
            video_id,
            JobState.ANALYZING,
            expected=JobState.EXTRACTING,
            claim_id=claim_id,
            fields={
                "frames_extracted": extraction.frames_stored,
                "frames_truncated": extraction.truncated,
                "duration_seconds": round(extraction.duration_seconds, 3),
                "sample_fps": extraction.fps,
            },
        )
    except ClaimLost:
        # Reclaimed while we were extracting. A newer attempt owns this job;
        # writing anything now would corrupt it.
        logger.warning("Abandoning %s: the lease was taken over", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "claim_lost"}
    except Exception:
        logger.exception("Could not record extraction result for %s", video_id)
        _fail(sync, video_id, claim_id, "Analysis could not be started.", stage="extraction")
        return failed

    # --- stage 2: analysis ------------------------------------------------
    from analyzer import MODEL_ID, FrameAuditError, check_pending_frames

    def on_frame_progress(audited: int, analysis_failed: int) -> None:
        """Extend the lease and verify we still hold it.

        Raising from here aborts the analysis loop, which is exactly what a
        fenced-off worker should do: stop before writing more frame results
        that belong to a superseded attempt.
        """
        sync(video_repo.heartbeat, video_id, claim_id)
        sync(
            video_repo.record_progress,
            video_id,
            claim_id=claim_id,
            frames_audited=audited,
            frames_failed=analysis_failed,
        )

    try:
        summary = check_pending_frames(
            video_id=video_id, on_progress=on_frame_progress, attempt=attempt
        )
    except ClaimLost:
        logger.warning("Abandoning %s mid-analysis: the lease was taken over", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "claim_lost"}
    except FrameAuditError:
        # The frame table could not be read, so the batch is unknown rather
        # than empty. That is a dependency failure, not a verdict on the
        # video, so it backs off and retries instead of failing the job.
        logger.exception("Frame enumeration failed for %s", video_id)
        return _retry_or_fail(
            sync, video_id, claim_id, attempt,
            "The analysis store was unavailable.",
            stage="analysis",
        )
    except Exception:
        logger.exception("Unexpected analysis failure for %s", video_id)
        _fail(sync, video_id, claim_id, "Frame analysis failed.", stage="analysis")
        return failed

    audited = int(summary.get("audited") or 0)
    analysis_failed = int(summary.get("failed") or 0)
    detections = int(summary.get("detections") or 0)

    result_fields = {
        "frames_audited": audited,
        "frames_failed": analysis_failed,
        "detections_recorded": detections,
        "model_id": MODEL_ID,
    }

    # --- stage 3: terminal state -----------------------------------------
    # COMPLETED requires that at least one frame was audited and that no frame
    # failed. A partial audit is a failure: reporting it as complete would let
    # the UI show "compliant" for footage that was only partly analysed.
    if audited == 0:
        _fail(
            sync, video_id, claim_id,
            "No frames could be analysed.",
            stage="analysis", fields=result_fields,
        )
        return {**failed, **result_fields}

    if analysis_failed:
        _fail(
            sync, video_id, claim_id,
            f"{analysis_failed} of {audited + analysis_failed} frames could not "
            f"be analysed.",
            stage="analysis", fields=result_fields,
        )
        return {**failed, **result_fields}

    try:
        sync(
            video_repo.transition,
            video_id,
            JobState.COMPLETED,
            expected=JobState.ANALYZING,
            claim_id=claim_id,
            fields=result_fields,
        )
    except ClaimLost:
        logger.warning(
            "Not recording completion for %s: the lease was taken over", video_id
        )
        return {"video_id": video_id, "skipped": True, "reason": "claim_lost"}
    except Exception:
        # The work succeeded but the completion could not be recorded. Do NOT
        # report success: the stored state is what a client reads.
        logger.exception("Could not record completion for %s", video_id)
        _fail(
            sync, video_id, claim_id,
            "Results could not be saved.",
            stage="persistence", fields=result_fields,
        )
        return {**failed, **result_fields}

    logger.info(
        "Pipeline complete for %s: %d frame(s) audited, %d detection(s)",
        video_id, audited, detections,
    )
    return {"video_id": video_id, "state": JobState.COMPLETED.value, **result_fields}


def _legacy_upload_dir():
    """The local upload directory, for records that predate S3 storage."""
    from pathlib import Path

    return Path("uploads").resolve()


def _prune_stale_frames(video_id: str, keep_count: int) -> int:
    """Delete frame rows at or beyond `keep_count` for this video.

    A retry that extracts fewer frames than a previous attempt - a shorter
    re-upload, or a lower sample rate - would otherwise leave the earlier
    attempt's higher-numbered rows in place, and `/violations` would return
    them as part of the current result. Frame rows are keyed
    (video_id, frame_number), so the current attempt's rows are exactly
    0..keep_count-1 and anything else is stale.
    """
    import analyzer

    return analyzer.delete_frames_from(video_id, keep_count)


def retry_delay_seconds(attempt: int) -> int:
    """Backoff before a retried attempt may run, in seconds.

    Exponential in the attempt number and capped. The point is not politeness
    to the dependency - it is that without a delay the worker reclaims the job
    on its next poll and spends the whole MAX_ATTEMPTS budget within seconds
    of a dependency blip, turning a recoverable outage into a terminal
    failure.
    """
    from config import get_settings

    settings = get_settings()
    base = settings.retry_backoff_seconds
    if base <= 0:
        return 0
    delay = base * (2 ** max(0, attempt - 1))
    return int(min(delay, settings.retry_backoff_cap_seconds))


def _retry_or_fail(sync, video_id, claim_id, attempt, summary, *, stage):
    """Requeue a job after a RETRYABLE failure, or fail it terminally.

    Retryable means "the same input could succeed later": a throttle, a
    timeout, a dependency briefly unreachable. Permanent failures do not come
    here at all - they go straight to `_fail`, because retrying a missing
    object or an unreadable video only delays the same answer three times over
    while billing for it.

    When the attempt budget is spent the job becomes FAILED with a summary
    that says so, so an exhausted retry is never reported as anything other
    than a failure.

    Returns:
        The result dict to return from the pipeline.
    """
    if attempt >= MAX_ATTEMPTS:
        _fail(
            sync, video_id, claim_id,
            f"{summary} No attempts remain.",
            stage=stage,
        )
        return {"video_id": video_id, "state": JobState.FAILED.value}

    delay = retry_delay_seconds(attempt)
    updated = sync(
        video_repo.schedule_retry,
        video_id,
        claim_id=claim_id,
        delay_seconds=delay,
        summary=summary,
    )
    if updated is None:
        # Fenced: a newer attempt owns the job, so there is nothing to retry.
        return {"video_id": video_id, "skipped": True, "reason": "claim_lost"}

    logger.warning(
        "Job %s hit a retryable failure during %s; requeued for attempt %d "
        "of %d in %ds", video_id, stage, attempt + 1, MAX_ATTEMPTS, delay,
    )
    return {
        "video_id": video_id,
        "state": JobState.QUEUED.value,
        "retry_scheduled": True,
        "retry_in_seconds": delay,
    }


def _fail(sync, video_id, claim_id, summary: str, *, stage: str, fields: dict | None = None):
    """Persist a terminal FAILED state with a safe summary.

    Fenced on the claim: a worker that has already been reclaimed must not
    fail a job that a newer attempt is now running. Best effort otherwise - if
    the write itself fails there is nothing further to try, and the job will
    be picked up by the next recovery sweep once its lease lapses.
    """
    payload = {"error_summary": summary, "failed_stage": stage}
    if fields:
        payload.update(fields)
    try:
        sync(
            video_repo.transition,
            video_id,
            JobState.FAILED,
            claim_id=claim_id,
            fields=payload,
        )
        logger.warning("Job %s failed during %s: %s", video_id, stage, summary)
    except ClaimLost:
        logger.warning(
            "Not recording a failure for %s: the lease was taken over", video_id
        )
    except Exception:
        logger.exception(
            "Could not record the failure for %s; the recovery sweep will "
            "pick it up", video_id
        )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

async def recover_abandoned_jobs(dispatch=None, *, limit: int = MAX_RECOVERIES_PER_SWEEP) -> dict:
    """Return abandoned jobs to the queue, and optionally re-dispatch them.

    "Abandoned" means the lease lapsed - a live worker heartbeats, so a lapsed
    lease means the process holding it is gone. Terminal jobs are never
    touched, and a job with a live lease is never touched.

    Idempotent. Running it twice in a row recovers nothing the second time,
    because the first run moved each job to `queued` with no lease, and
    `queued` is only treated as abandoned once it has sat untouched for a full
    lease period.

    Args:
        dispatch: optional callable taking `video_id`, invoked for each job
            returned to the queue. Omit it to requeue without running - which
            is what the maintenance CLI does when a worker will pick the jobs
            up, and what a test does when it only wants the state change.
        limit: maximum jobs to touch in one sweep.

    Returns:
        Counts: {"scanned", "requeued", "failed", "skipped"}.
    """
    counts = {"scanned": 0, "requeued": 0, "failed": 0, "skipped": 0}

    try:
        abandoned = await video_repo.find_expired_leases()
    except Exception:
        logger.exception("Recovery sweep could not enumerate abandoned jobs")
        return counts

    counts["scanned"] = len(abandoned)

    for record in abandoned[:limit]:
        video_id = record.get("video_id")
        try:
            updated = await video_repo.reclaim(record)
        except Exception:
            # One bad record must not abort the sweep.
            logger.exception("Could not reclaim job %s", video_id)
            counts["skipped"] += 1
            continue

        if updated is None:
            # Another sweep, or the worker itself, got there first.
            counts["skipped"] += 1
            continue

        if coerce(updated.get("status")) is JobState.FAILED:
            counts["failed"] += 1
            continue

        counts["requeued"] += 1

        if dispatch is not None:
            try:
                dispatch(video_id)
            except Exception:
                # The job is queued and durable; a dispatch failure only means
                # it waits for the next sweep or an explicit retry.
                logger.exception("Could not dispatch recovered job %s", video_id)

    if counts["scanned"]:
        logger.info(
            "Recovery sweep: %d abandoned, %d requeued, %d failed, %d skipped",
            counts["scanned"], counts["requeued"], counts["failed"], counts["skipped"],
        )
    return counts


async def find_stuck_jobs(older_than: timedelta = STUCK_AFTER) -> list[dict]:
    """Jobs left non-terminal with no recent update.

    Retained from Phase 4 as an operator *reporting* helper, and deliberately
    distinct from `find_expired_leases`: this one answers "what looks wrong?"
    over a long horizon, including jobs whose lease is live but which have
    made no progress for half an hour. Nothing is failed automatically.
    """
    cutoff = datetime.now(timezone.utc) - older_than
    collection = await video_repo._collection()  # noqa: SLF001 - same package

    stuck = []
    cursor = collection.find(
        {"status": {"$in": [s.value for s in (
            JobState.QUEUED, JobState.EXTRACTING, JobState.ANALYZING
        )]}}
    )
    async for record in cursor:
        updated = record.get("updated_at")
        if updated is None:
            stuck.append(record)
            continue
        # Stored values are timezone-aware UTC; tolerate a naive legacy value.
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated < cutoff:
            stuck.append(record)
    return stuck


__all__ = (
    "AlreadyRunning",
    "MAX_RECOVERIES_PER_SWEEP",
    "PipelineError",
    "RetryLimitReached",
    "STUCK_AFTER",
    "find_stuck_jobs",
    "recover_abandoned_jobs",
    "run_job",
    "submit",
)
