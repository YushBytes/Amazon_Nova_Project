"""Pipeline orchestration: upload -> extract -> analyse -> persisted result.

Phase 4. Previously `/analyze` scheduled a FastAPI background task and
immediately returned `status: "ANALYZING"`; the task logged its own failures and
nothing ever recorded a terminal outcome. A caller could not distinguish
"running", "finished", and "died".

This module owns the state machine's execution. Every real milestone is
persisted before and after the work it describes, so a client always reads a
truthful state.

STAGES

    submit()                    UPLOADED/COMPLETED/FAILED -> QUEUED
    run_job()  stage 1          QUEUED     -> EXTRACTING   (extractor)
               stage 2          EXTRACTING -> ANALYZING    (analyzer)
               stage 3          ANALYZING  -> COMPLETED | FAILED

ARCHITECTURAL LIMITATION, stated plainly
----------------------------------------
Execution uses FastAPI's in-process `BackgroundTasks`, which is what this
deployment has. That means:

* **No crash recovery.** If the process dies mid-run, the job stays in
  EXTRACTING or ANALYZING forever. Nothing resumes it. This module does not
  pretend otherwise - it provides `find_stuck_jobs()` so an operator (or a
  future scheduled sweep) can *detect* those jobs, and `submit()` can retry
  them. It is not a durable queue and is not described as one.
* **Process-local concurrency control.** The duplicate-run guard is a
  conditional state transition in MongoDB, which does hold across processes
  for the QUEUED -> EXTRACTING move. But a second process could still begin
  work between the read and the write for other races; the conditional update
  is the mitigation, not a lock.
* **No backpressure.** Submitting many videos at once runs many background
  tasks in the server's threadpool.

Moving to a durable queue (SQS, Celery, Step Functions) is the real fix and is
deliberately out of Phase 4 scope. See docs/PIPELINE_OPERATIONS.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import video_repo
from job_state import MAX_ATTEMPTS, InvalidTransition, JobState, coerce
from video_repo import OwnershipUnknown

logger = logging.getLogger(__name__)

# A job in a non-terminal state with no update for this long is very likely
# orphaned by a process restart. Used only for *reporting*; nothing fails a
# job automatically, because a long legitimate run must not be killed.
STUCK_AFTER = timedelta(minutes=30)


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
    * re-queuing bumps `attempt` and clears the previous `error_summary`, so a
      stale failure is never read as current.

    Returns:
        The updated record.
    """
    record = await video_repo.get_video(video_id)
    state = coerce(record.get("status"))

    if state.is_active:
        raise AlreadyRunning(
            "Analysis is already running for this video."
        )

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
        raise AlreadyRunning(
            "Analysis was started by another request."
        ) from exc


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_job(video_id: str, file_path: str) -> dict:
    """Execute the pipeline for one queued video.

    Synchronous by design: it is handed to `BackgroundTasks`, which runs sync
    callables in a worker thread. The database calls inside are async, so each
    is driven with `anyio.from_thread`-free `anyio.run` on this thread.

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

    # --- claim the job ----------------------------------------------------
    try:
        sync(
            video_repo.transition,
            video_id,
            JobState.EXTRACTING,
            expected=JobState.QUEUED,
        )
    except InvalidTransition:
        # Not ours to run: either another worker claimed it or it is not
        # queued. Do not touch its state.
        logger.warning("Pipeline skipped for %s: job was not claimable", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "not_queued"}
    except OwnershipUnknown:
        logger.warning("Pipeline skipped for %s: no such video", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "unknown_video"}
    except Exception:
        logger.exception("Pipeline could not claim %s", video_id)
        return {"video_id": video_id, "skipped": True, "reason": "claim_failed"}

    # --- stage 1: extraction ---------------------------------------------
    from extractor import ExtractionError, extract_and_store

    try:
        extraction = extract_and_store(file_path, video_id)
    except ExtractionError as exc:
        # ExtractionError messages are written to be shown to the owner.
        _fail(sync, video_id, str(exc), stage="extraction")
        return {"video_id": video_id, "state": JobState.FAILED.value}
    except Exception:
        logger.exception("Unexpected extraction failure for %s", video_id)
        _fail(sync, video_id, "Frame extraction failed.", stage="extraction")
        return {"video_id": video_id, "state": JobState.FAILED.value}

    try:
        sync(
            video_repo.transition,
            video_id,
            JobState.ANALYZING,
            expected=JobState.EXTRACTING,
            fields={
                "frames_extracted": extraction.frames_stored,
                "frames_truncated": extraction.truncated,
                "duration_seconds": round(extraction.duration_seconds, 3),
                "sample_fps": extraction.fps,
            },
        )
    except Exception:
        logger.exception("Could not record extraction result for %s", video_id)
        _fail(sync, video_id, "Analysis could not be started.", stage="extraction")
        return {"video_id": video_id, "state": JobState.FAILED.value}

    # --- stage 2: analysis ------------------------------------------------
    from analyzer import MODEL_ID, FrameAuditError, check_pending_frames

    try:
        summary = check_pending_frames(video_id=video_id)
    except FrameAuditError:
        logger.exception("Frame enumeration failed for %s", video_id)
        _fail(sync, video_id, "The analysis store is unavailable.", stage="analysis")
        return {"video_id": video_id, "state": JobState.FAILED.value}
    except Exception:
        logger.exception("Unexpected analysis failure for %s", video_id)
        _fail(sync, video_id, "Frame analysis failed.", stage="analysis")
        return {"video_id": video_id, "state": JobState.FAILED.value}

    audited = int(summary.get("audited") or 0)
    failed = int(summary.get("failed") or 0)
    detections = int(summary.get("detections") or 0)

    result_fields = {
        "frames_audited": audited,
        "frames_failed": failed,
        "detections_recorded": detections,
        "model_id": MODEL_ID,
    }

    # --- stage 3: terminal state -----------------------------------------
    # COMPLETED requires that at least one frame was audited and that no frame
    # failed. A partial audit is a failure: reporting it as complete would let
    # the UI show "compliant" for footage that was only partly analysed.
    if audited == 0:
        _fail(
            sync,
            video_id,
            "No frames could be analysed.",
            stage="analysis",
            fields=result_fields,
        )
        return {"video_id": video_id, "state": JobState.FAILED.value, **result_fields}

    if failed:
        _fail(
            sync,
            video_id,
            f"{failed} of {audited + failed} frames could not be analysed.",
            stage="analysis",
            fields=result_fields,
        )
        return {"video_id": video_id, "state": JobState.FAILED.value, **result_fields}

    try:
        sync(
            video_repo.transition,
            video_id,
            JobState.COMPLETED,
            expected=JobState.ANALYZING,
            fields=result_fields,
        )
    except Exception:
        # The work succeeded but the completion could not be recorded. Do NOT
        # report success: the stored state is what a client reads.
        logger.exception("Could not record completion for %s", video_id)
        _fail(
            sync,
            video_id,
            "Results could not be saved.",
            stage="persistence",
            fields=result_fields,
        )
        return {"video_id": video_id, "state": JobState.FAILED.value, **result_fields}

    logger.info(
        "Pipeline complete for %s: %d frame(s) audited, %d detection(s)",
        video_id, audited, detections,
    )
    return {"video_id": video_id, "state": JobState.COMPLETED.value, **result_fields}


def _fail(sync, video_id: str, summary: str, *, stage: str, fields: dict | None = None):
    """Persist a terminal FAILED state with a safe summary.

    Best effort: if even this write fails there is nothing further to try, and
    the job will show up in `find_stuck_jobs()`.
    """
    payload = {"error_summary": summary, "failed_stage": stage}
    if fields:
        payload.update(fields)
    try:
        sync(video_repo.transition, video_id, JobState.FAILED, fields=payload)
        logger.warning("Job %s failed during %s: %s", video_id, stage, summary)
    except Exception:
        logger.exception(
            "Could not record the failure for %s; it will appear as stuck", video_id
        )


# ---------------------------------------------------------------------------
# Stuck-job detection
# ---------------------------------------------------------------------------

async def find_stuck_jobs(older_than: timedelta = STUCK_AFTER) -> list[dict]:
    """Jobs left non-terminal with no recent update.

    An in-process background task cannot survive a restart, so this is how an
    operator finds the casualties. It only *reports*: nothing is failed
    automatically, because a slow legitimate run must not be killed.
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
    "PipelineError",
    "RetryLimitReached",
    "STUCK_AFTER",
    "find_stuck_jobs",
    "run_job",
    "submit",
)
