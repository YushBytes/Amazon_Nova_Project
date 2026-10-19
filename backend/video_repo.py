"""Ownership records for uploaded videos.

Phase 3. The previous implementation stored uploads as `uploads/{video_id}.{ext}`
with no record of who uploaded them, and DynamoDB's NovaFlowFrames table keys
only on `video_id`. There was therefore nothing in the system that could answer
"whose video is this?", which made every per-video route an IDOR.

This module adds the smallest ownership store that the existing architecture
allows: a `videos` collection in the MongoDB database that already holds user
accounts. It introduces no new database or storage system, and the S3 +
DynamoDB analysis pipeline is unchanged - frames still key on `video_id`, and
authorization happens before any frame is read.

Ownership is recorded at upload time, which is the only moment the uploader's
identity is known. Videos uploaded before this change have no record; see
`OwnershipUnknown` and docs/SECURITY_AUDIT.md for how those are treated.

Phase 4 extends the same record with analysis **job state**. That is a
deliberate choice over a second DynamoDB table:

* ownership and job state belong to the same aggregate, so one conditional
  update covers both and they cannot disagree;
* the record already carried a free-text `status`, so this replaces an
  existing field rather than adding a store;
* no new DynamoDB table means no new IaC and no operator provisioning step.

Frame metadata and detections stay in DynamoDB, and frame images stay in S3 —
that split is unchanged. See docs/PIPELINE_ARCHITECTURE.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from db import get_database
from job_state import (
    MAX_ATTEMPTS,
    InvalidTransition,
    JobState,
    assert_transition,
    coerce,
)

logger = logging.getLogger(__name__)

COLLECTION = "videos"

# Bumped when the pipeline's stored shape or analysis contract changes, so a
# result can be traced to the code that produced it.
PIPELINE_VERSION = "4.0"


class OwnershipUnknown(LookupError):
    """No ownership record exists for this video id.

    Raised for videos uploaded before ownership tracking existed, or for an id
    that was never issued. Callers must treat this as "deny", not "allow".
    """


async def _collection():
    database = await get_database()
    return database[COLLECTION]


async def ensure_indexes() -> None:
    """Create the indexes this collection relies on.

    Safe to call repeatedly. `video_id` is unique so a duplicate id cannot
    produce two ownership records with different owners.
    """
    col = await _collection()
    await col.create_index("video_id", unique=True)
    await col.create_index("owner")


async def record_video(
    *,
    video_id: str,
    owner: str,
    stored_filename: str,
    original_filename: str | None,
    extension: str,
    size_bytes: int,
) -> dict:
    """Persist the ownership record for a freshly uploaded video.

    `original_filename` is retained as display metadata only and is never used
    to build a filesystem path or storage key.
    """
    now = datetime.now(timezone.utc)
    document = {
        "video_id": video_id,
        "owner": owner,
        "stored_filename": stored_filename,
        "original_filename": original_filename,
        "extension": extension,
        "size_bytes": size_bytes,
        # Job state. UPLOADED means stored but not submitted for analysis.
        "status": JobState.UPLOADED.value,
        "attempt": 0,
        "frames_extracted": None,
        "frames_audited": None,
        "frames_failed": None,
        "detections_recorded": None,
        "error_summary": None,
        "model_id": None,
        "pipeline_version": PIPELINE_VERSION,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
    }
    col = await _collection()
    await col.insert_one(document)
    return document


async def get_video(video_id: str) -> dict:
    """Return the ownership record, or raise OwnershipUnknown."""
    col = await _collection()
    record = await col.find_one({"video_id": video_id})
    if not record:
        raise OwnershipUnknown(video_id)
    return record


async def is_owner(video_id: str, username: str) -> bool:
    """True only when an ownership record exists and names this user."""
    try:
        record = await get_video(video_id)
    except OwnershipUnknown:
        return False
    return record.get("owner") == username


async def list_videos_for_owner(username: str, limit: int = 100) -> list[dict]:
    """Return this user's videos, newest first. Never another user's."""
    col = await _collection()
    cursor = col.find({"owner": username}).sort("created_at", -1).limit(limit)
    return [doc async for doc in cursor]


async def transition(
    video_id: str,
    target: JobState,
    *,
    expected: JobState | None = None,
    fields: dict[str, Any] | None = None,
) -> dict:
    """Move a job to `target`, validating the transition first.

    The update is conditional on the state we believe the record is in, so two
    workers cannot both advance the same job. When `expected` is omitted the
    current state is read first and used as the condition, which still rejects
    a concurrent change (the conditional update simply matches nothing).

    Args:
        video_id: the job to move.
        target: the state to move to.
        expected: the state the record must currently be in. Defaults to
            whatever is stored at call time.
        fields: additional fields to persist in the same write.

    Returns:
        The updated record.

    Raises:
        OwnershipUnknown: no such video.
        InvalidTransition: the move is not in the state machine, or the record
            changed underneath us (a concurrent worker won).
    """
    col = await _collection()
    record = await get_video(video_id)

    current = coerce(record.get("status"))
    if expected is not None and current != expected:
        raise InvalidTransition(
            f"expected job to be {expected.value} but it is {current.value}"
        )

    assert_transition(current, target)

    now = datetime.now(timezone.utc)
    update: dict[str, Any] = {"status": target.value, "updated_at": now}

    if target is JobState.QUEUED:
        # A (re)submission starts a new attempt and clears the previous
        # outcome so a stale error cannot be read as current.
        update["attempt"] = int(record.get("attempt") or 0) + 1
        update["error_summary"] = None
        update["completed_at"] = None
    if target is JobState.EXTRACTING:
        update["started_at"] = now
    if target in (JobState.COMPLETED, JobState.FAILED):
        update["completed_at"] = now

    if fields:
        update.update(fields)

    # Conditional on the state we validated against.
    result = await col.update_one(
        {"video_id": video_id, "status": record.get("status")},
        {"$set": update},
    )
    if getattr(result, "modified_count", 0) != 1:
        raise InvalidTransition(
            f"job {video_id} changed state concurrently; transition to "
            f"{target.value} was not applied"
        )

    return await get_video(video_id)


async def record_progress(video_id: str, **fields: Any) -> None:
    """Persist counts or metadata without changing state.

    Used mid-stage so the frontend can show real progress. Never touches
    `status`, so it cannot accidentally complete a job.
    """
    if not fields:
        return
    fields.pop("status", None)
    col = await _collection()
    await col.update_one(
        {"video_id": video_id},
        {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}},
    )


def job_view(record: dict) -> dict:
    """Project a stored record into the client-facing job shape.

    Deliberately omits `owner`, `stored_filename`, and anything else that
    describes server-side storage. `error_summary` is written by the pipeline
    and is already a safe message, never an exception string.
    """
    from job_state import stage_label

    state = coerce(record.get("status"))
    created = record.get("created_at")
    updated = record.get("updated_at")
    completed = record.get("completed_at")

    def iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else None

    return {
        "video_id": record.get("video_id"),
        "state": state.value,
        "stage": stage_label(state),
        "is_terminal": state.is_terminal,
        "is_active": state.is_active,
        "attempt": int(record.get("attempt") or 0),
        "max_attempts": MAX_ATTEMPTS,
        "frames_extracted": record.get("frames_extracted"),
        "frames_audited": record.get("frames_audited"),
        "frames_failed": record.get("frames_failed"),
        "detections_recorded": record.get("detections_recorded"),
        "error_summary": record.get("error_summary"),
        "model_id": record.get("model_id"),
        "pipeline_version": record.get("pipeline_version"),
        "original_filename": record.get("original_filename"),
        "size_bytes": record.get("size_bytes"),
        "created_at": iso(created),
        "updated_at": iso(updated),
        "completed_at": iso(completed),
    }
