"""The video aggregate: identity, ownership, job state, and worker lease.

AUTHORITATIVE STORAGE, STATED ONCE
----------------------------------
MongoDB `videos`     video identity, owner, source-object reference, job
                     state, attempt count, worker claim and lease.
MongoDB `users`      accounts and credentials (see auth.py).
S3 `sources/`        the uploaded source video bytes.
S3 `frames/`         extracted frame JPEGs.
DynamoDB frames      per-frame metadata, audit status, and detections.

There is exactly one writer for each of those. In particular **nothing writes
a job-level status to DynamoDB**: the per-frame `status` attribute there is
frame-scoped and is owned by the analyzer. See docs/PIPELINE_ARCHITECTURE.md
for why job state stayed here rather than moving to DynamoDB in Phase 4.1,
and for the migration plan if that choice is revisited.

HISTORY
-------
Phase 3 added this collection because nothing in the system could answer
"whose video is this?", which made every per-video route an IDOR. Phase 4
added job state to the same record. Phase 4.1 added the source-object
reference and the worker lease below, and removed the local-disk dependency.

WORKER CLAIMS AND LEASES (Phase 4.1)
------------------------------------
Phase 4 claimed a job with a conditional state transition, which stopped two
workers from both *starting* one job but did nothing once a worker died: the
job sat in `extracting` or `analyzing` forever, and `find_stuck_jobs` could
only report it.

A claim now carries two extra fields:

    claim_id          a uuid4 minted by the worker that won the claim
    lease_expires_at  UTC instant after which the claim is considered dead

Every subsequent write by that worker is conditional on `claim_id` still
matching (`transition(..., claim_id=...)`). That is the fencing token: if a
recovery sweep reclaims the job and a new worker takes it, the original
worker's next write matches zero documents and it stops. A stale worker
therefore cannot overwrite a newer attempt's state or results.

`lease_expires_at` is extended by `heartbeat()` while a stage is making
progress, so a legitimately slow job is never reclaimed out from under a
worker that is still alive.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
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
# result can be traced to the code that produced it. 4.1 adds the source
# reference and lease fields.
PIPELINE_VERSION = "4.1"


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer; using %d", name, default)
        return default
    return max(minimum, value)


def lease_seconds() -> int:
    """How long a worker's claim stays valid without a heartbeat.

    Must comfortably exceed the longest gap between heartbeats, or a live
    worker gets reclaimed. The pipeline heartbeats at every stage boundary and
    after each analysed frame, and one frame is bounded by the Bedrock retry
    budget, so five minutes is generous.
    """
    return _int_env("JOB_LEASE_SECONDS", 300, minimum=30)


class OwnershipUnknown(LookupError):
    """No ownership record exists for this video id.

    Raised for videos uploaded before ownership tracking existed, or for an id
    that was never issued. Callers must treat this as "deny", not "allow".
    """


class ClaimLost(InvalidTransition):
    """This worker no longer holds the job's lease.

    A subclass of InvalidTransition so existing handlers keep working, but
    distinguishable for the stale-worker tests and for logging: it means a
    recovery sweep or another worker took the job over.
    """


async def _collection():
    database = await get_database()
    return database[COLLECTION]


async def ensure_indexes() -> None:
    """Create the indexes this collection relies on.

    Safe to call repeatedly. `video_id` is unique so a duplicate id cannot
    produce two ownership records with different owners. `status` is indexed
    because the recovery sweep queries on it.
    """
    col = await _collection()
    await col.create_index("video_id", unique=True)
    await col.create_index("owner")
    await col.create_index("status")
    # The worker's queue poll is find({status}).sort(updated_at), and the
    # recovery sweep filters the same states by time. One compound index
    # serves both, so neither degrades into a collection scan as the table
    # grows (Phase 6).
    await col.create_index([("status", 1), ("updated_at", 1)])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    """Interpret a stored datetime as UTC.

    Some drivers and some legacy rows return naive datetimes. Comparing a
    naive value against an aware one raises, which would break the recovery
    sweep on exactly the old records it exists to rescue.
    """
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def record_video(
    *,
    video_id: str,
    owner: str,
    stored_filename: str,
    original_filename: str | None,
    extension: str,
    size_bytes: int,
    s3_bucket: str | None = None,
    s3_key: str | None = None,
) -> dict:
    """Persist the record for a freshly uploaded video.

    `original_filename` is retained as display metadata only and is never used
    to build a filesystem path or storage key.

    `s3_bucket`/`s3_key` point at the durable source object. They are optional
    only so this function can still describe a legacy record in tests; the
    upload route always supplies them.
    """
    now = _now()
    document = {
        "video_id": video_id,
        "owner": owner,
        # Retained for legacy playback and for the operator backfill; the
        # pipeline no longer reads it when an s3_key is present.
        "stored_filename": stored_filename,
        "original_filename": original_filename,
        "extension": extension,
        "size_bytes": size_bytes,
        # Durable source location (Phase 4.1).
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        # Job state. UPLOADED means stored but not submitted for analysis.
        "status": JobState.UPLOADED.value,
        "attempt": 0,
        "recovered_count": 0,
        "claim_id": None,
        "lease_expires_at": None,
        # Earliest instant a worker may claim this job. Set when a retryable
        # failure schedules a backoff (Phase 6); None means "claimable now".
        "not_before": None,
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


async def delete_video(video_id: str) -> bool:
    """Remove one video record.

    Compensating cleanup for an upload that could not be completed. Scoped to
    a single server-issued id and never exposed through a route, so it cannot
    be used to delete another user's record.
    """
    col = await _collection()
    result = await col.delete_one({"video_id": video_id})
    return getattr(result, "deleted_count", 0) == 1


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def get_video(video_id: str) -> dict:
    """Return the record, or raise OwnershipUnknown."""
    col = await _collection()
    record = await col.find_one({"video_id": video_id})
    if not record:
        raise OwnershipUnknown(video_id)
    return record


async def is_owner(video_id: str, username: str) -> bool:
    """True only when a record exists and names this user."""
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


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

async def transition(
    video_id: str,
    target: JobState,
    *,
    expected: JobState | None = None,
    claim_id: str | None = None,
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
        claim_id: when given, the update also requires this claim to still
            hold the lease. This is the fencing token - a worker whose job was
            reclaimed cannot write anything further.
        fields: additional fields to persist in the same write.

    Returns:
        The updated record.

    Raises:
        OwnershipUnknown: no such video.
        ClaimLost: `claim_id` was given and no longer matches.
        InvalidTransition: the move is not in the state machine, or the record
            changed underneath us.
    """
    col = await _collection()
    record = await get_video(video_id)

    # Checked before the state, deliberately. When a sweep reclaims a job it
    # changes both the claim and the state, and "you no longer hold this job"
    # is the specific, actionable truth - it tells the caller to stop rather
    # than to record a failure. Reporting the state mismatch first would make
    # a fenced worker look like an ordinary lost race.
    if claim_id is not None and record.get("claim_id") != claim_id:
        raise ClaimLost(
            f"job {video_id} is no longer held by this worker; "
            f"transition to {coerce(target).value} was not applied"
        )

    current = coerce(record.get("status"))
    if expected is not None and current != expected:
        raise InvalidTransition(
            f"expected job to be {expected.value} but it is {current.value}"
        )

    assert_transition(current, target)

    now = _now()
    update: dict[str, Any] = {"status": target.value, "updated_at": now}

    if target is JobState.QUEUED:
        # A (re)submission starts a new attempt and clears the previous
        # outcome so a stale error cannot be read as current. The lease is
        # released so the next worker can claim it.
        update["attempt"] = int(record.get("attempt") or 0) + 1
        update["error_summary"] = None
        update["completed_at"] = None
        update["claim_id"] = None
        update["lease_expires_at"] = None
        # Claimable immediately unless the caller passes a `not_before` in
        # `fields` - which is how a retry backoff is applied. A user-requested
        # retry deliberately has no delay.
        update["not_before"] = None
    if target is JobState.EXTRACTING:
        update["started_at"] = now
    if target in (JobState.COMPLETED, JobState.FAILED):
        update["completed_at"] = now
        # Terminal states hold no lease: there is nothing left to fence, and
        # leaving one would make the job look claimed to an operator.
        update["claim_id"] = None
        update["lease_expires_at"] = None

    if fields:
        update.update(fields)

    condition: dict[str, Any] = {
        "video_id": video_id,
        "status": record.get("status"),
    }
    if claim_id is not None:
        condition["claim_id"] = claim_id

    result = await col.update_one(condition, {"$set": update})
    if getattr(result, "modified_count", 0) != 1:
        if claim_id is not None:
            raise ClaimLost(
                f"job {video_id} is no longer held by this worker; "
                f"transition to {target.value} was not applied"
            )
        raise InvalidTransition(
            f"job {video_id} changed state concurrently; transition to "
            f"{target.value} was not applied"
        )

    return await get_video(video_id)


# ---------------------------------------------------------------------------
# Claims and leases
# ---------------------------------------------------------------------------

async def claim(
    video_id: str,
    target: JobState,
    *,
    expected: JobState,
    fields: dict[str, Any] | None = None,
) -> tuple[dict, str]:
    """Take ownership of a job and move it to `target` in one write.

    The update matches only when the job is in `expected` **and** its lease is
    free - unclaimed, or claimed with an expired lease. Both conditions are in
    a single `update_one` filter, so two workers racing on the same job cannot
    both match: MongoDB applies one update to the document and the loser sees
    `modified_count == 0`.

    Returns:
        (updated record, claim_id). Pass that claim_id to every later write
        for this job so a reclaim can fence this worker off.

    Raises:
        OwnershipUnknown: no such video.
        InvalidTransition: the job is not in `expected`, the move is illegal,
            or another worker holds a live lease.
    """
    col = await _collection()
    record = await get_video(video_id)

    current = coerce(record.get("status"))
    if current != expected:
        raise InvalidTransition(
            f"expected job to be {expected.value} but it is {current.value}"
        )
    assert_transition(current, target)

    now = _now()
    claim_id = uuid.uuid4().hex
    update: dict[str, Any] = {
        "status": target.value,
        "updated_at": now,
        "claim_id": claim_id,
        "lease_expires_at": now + timedelta(seconds=lease_seconds()),
    }
    if target is JobState.EXTRACTING:
        update["started_at"] = now
    if fields:
        update.update(fields)

    # "Lease is free" means: nobody holds it, or whoever did has let it
    # expire. `{"claim_id": None}` also matches a document where the field is
    # absent, which is what a pre-Phase-4.1 record looks like.
    result = await col.update_one(
        {
            "video_id": video_id,
            "status": record.get("status"),
            "$or": [
                {"claim_id": None},
                {"lease_expires_at": None},
                {"lease_expires_at": {"$lt": now}},
            ],
        },
        {"$set": update},
    )
    if getattr(result, "modified_count", 0) != 1:
        raise InvalidTransition(
            f"job {video_id} could not be claimed; it is held by another "
            f"worker or changed state concurrently"
        )

    return await get_video(video_id), claim_id


async def heartbeat(video_id: str, claim_id: str) -> None:
    """Extend this worker's lease.

    Conditional on the claim still matching, so a worker that has already been
    fenced off cannot resurrect its lease and start writing again.

    Raises:
        ClaimLost: the claim no longer holds the job.
    """
    col = await _collection()
    now = _now()
    result = await col.update_one(
        {"video_id": video_id, "claim_id": claim_id},
        {
            "$set": {
                "lease_expires_at": now + timedelta(seconds=lease_seconds()),
                "updated_at": now,
            }
        },
    )
    if getattr(result, "modified_count", 0) != 1:
        raise ClaimLost(f"job {video_id} is no longer held by this worker")


async def holds_claim(video_id: str, claim_id: str) -> bool:
    """True if this claim still owns the job. Read-only; never raises."""
    try:
        record = await get_video(video_id)
    except OwnershipUnknown:
        return False
    return record.get("claim_id") == claim_id


async def record_progress(
    video_id: str, *, claim_id: str | None = None, **fields: Any
) -> None:
    """Persist counts or metadata without changing state.

    Used mid-stage so the frontend can show real progress. Never touches
    `status`, so it cannot accidentally complete a job. When `claim_id` is
    given the write is fenced: a stale worker's progress counts are silently
    dropped rather than overwriting the live attempt's.
    """
    fields.pop("status", None)
    if not fields:
        return
    col = await _collection()
    condition: dict[str, Any] = {"video_id": video_id}
    if claim_id is not None:
        condition["claim_id"] = claim_id
    await col.update_one(
        condition,
        {"$set": {**fields, "updated_at": _now()}},
    )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

async def find_queued_jobs(limit: int = 10) -> list[dict]:
    """Jobs ready for a worker to claim, oldest first.

    Phase 6. This is the read that makes the `videos` collection usable as a
    durable queue: `submit()` persists QUEUED before the API answers, and a
    worker polls here. Nothing is held in process memory, so the API and the
    worker restart independently and an accepted job survives either.

    Oldest first (`updated_at` ascending) so a job cannot be starved by newer
    arrivals. Jobs with a future `not_before` are skipped - that is the retry
    backoff, and without honouring it a worker would reclaim a just-failed job
    instantly and spend the whole attempt budget in a few seconds.

    This does not claim anything. The caller claims with `claim()`, whose
    conditional update is what actually resolves a race between two workers,
    so two workers reading the same list is expected and safe.
    """
    col = await _collection()
    now = _now()

    ready: list[dict] = []
    cursor = col.find({"status": JobState.QUEUED.value}).sort("updated_at", 1)
    async for record in cursor:
        not_before = _aware(record.get("not_before"))
        if not_before is not None and not_before > now:
            continue
        ready.append(record)
        if len(ready) >= limit:
            break
    return ready


async def schedule_retry(
    video_id: str,
    *,
    claim_id: str,
    delay_seconds: int,
    summary: str | None = None,
) -> dict | None:
    """Return a job to the queue after a retryable failure, with a backoff.

    Distinct from `transition(..., FAILED)`: this is for a failure that may
    succeed later (a throttle, a timeout, a dependency briefly unreachable),
    where terminal failure would be a lie about what happened. The attempt
    counter still increments via the QUEUED transition, so retries stay
    bounded by MAX_ATTEMPTS and this cannot loop forever.

    Fenced on `claim_id`, so a worker that has already been reclaimed cannot
    push a newer attempt back into the queue.

    Returns:
        The updated record, or None if the claim no longer holds.
    """
    not_before = _now() + timedelta(seconds=max(0, int(delay_seconds)))
    fields: dict[str, Any] = {"not_before": not_before}
    if summary:
        # Kept visible to the owner while the retry is pending, so a job that
        # is waiting does not look idle for no reason.
        fields["last_retry_reason"] = summary

    try:
        updated = await transition(
            video_id,
            JobState.QUEUED,
            claim_id=claim_id,
            fields=fields,
        )
    except ClaimLost:
        logger.warning(
            "Not scheduling a retry for %s: the lease was taken over", video_id
        )
        return None

    logger.info(
        "Retry scheduled for %s in %ss (attempt %s of %s)",
        video_id, delay_seconds, updated.get("attempt"), MAX_ATTEMPTS,
    )
    return updated


async def count_by_state() -> dict[str, int]:
    """How many jobs sit in each state. For the readiness probe and operators.

    Counts only - never ids, owners or filenames - so it is safe to log and
    safe to expose on an operational endpoint.
    """
    col = await _collection()
    counts: dict[str, int] = {}
    for state in JobState:
        counts[state.value] = await col.count_documents({"status": state.value})
    return counts


async def find_expired_leases() -> list[dict]:
    """Jobs in a non-terminal worked state whose lease has lapsed.

    These are the genuinely abandoned ones: a live worker heartbeats, so a
    lapsed lease means the process holding it is gone.

    `queued` is included because a job can be queued and then lose its
    process before any worker claims it - the request returned 202 and
    nothing ever picked the job up. Such a record has no lease at all, so it
    is only treated as abandoned once `updated_at` is older than one lease
    period; otherwise a job queued half a second ago would be "recovered" out
    from under the worker that is about to claim it.
    """
    now = _now()
    grace = timedelta(seconds=lease_seconds())
    col = await _collection()

    worked = [JobState.EXTRACTING.value, JobState.ANALYZING.value]
    abandoned: list[dict] = []

    cursor = col.find({"status": {"$in": worked}})
    async for record in cursor:
        expires = _aware(record.get("lease_expires_at"))
        if expires is None or expires < now:
            abandoned.append(record)

    cursor = col.find({"status": JobState.QUEUED.value})
    async for record in cursor:
        # A job deliberately waiting out a retry backoff is NOT abandoned.
        # Without this, a backoff longer than one lease period (the cap is
        # 600s against a 300s lease) would be reclaimed as if its worker had
        # died, incrementing `attempt` a second time for one failure and
        # burning the budget at twice the intended rate.
        not_before = _aware(record.get("not_before"))
        if not_before is not None and not_before > now:
            continue
        updated = _aware(record.get("updated_at"))
        if updated is None or updated < now - grace:
            abandoned.append(record)

    return abandoned


async def reclaim(record: dict) -> dict | None:
    """Return one abandoned job to `queued`, or fail it if out of attempts.

    Conditional on the job still being in the state and under the claim we
    read, so:

    * two recovery sweeps running at once cannot both reclaim it - the loser
      matches nothing and gets None;
    * the original worker, if it is somehow still alive, is fenced off the
      moment this succeeds, because its `claim_id` no longer matches.

    `attempt` is incremented by the QUEUED transition, so retry limits apply
    to recovered jobs exactly as they do to user-requested retries. A job that
    has exhausted its attempts is moved to FAILED instead of being requeued
    forever.

    Returns:
        The updated record, or None if another sweep got there first.
    """
    video_id = record.get("video_id")
    if not video_id:
        return None

    current = coerce(record.get("status"))
    attempt = int(record.get("attempt") or 0)
    stale_claim = record.get("claim_id")

    col = await _collection()
    now = _now()

    if attempt >= MAX_ATTEMPTS:
        update = {
            "status": JobState.FAILED.value,
            "updated_at": now,
            "completed_at": now,
            "claim_id": None,
            "lease_expires_at": None,
            "error_summary": (
                "Analysis was interrupted and has used all of its attempts."
            ),
            "failed_stage": "recovery",
        }
        target = JobState.FAILED
    else:
        update = {
            "status": JobState.QUEUED.value,
            "updated_at": now,
            "attempt": attempt + 1,
            "recovered_count": int(record.get("recovered_count") or 0) + 1,
            "claim_id": None,
            "lease_expires_at": None,
            "error_summary": None,
            "completed_at": None,
        }
        target = JobState.QUEUED

    assert_transition(current, target)

    result = await col.update_one(
        {
            "video_id": video_id,
            "status": record.get("status"),
            "claim_id": stale_claim,
        },
        {"$set": update},
    )
    if getattr(result, "modified_count", 0) != 1:
        # Someone else reclaimed it, or the worker came back and moved it on.
        # Either way there is nothing for this sweep to do.
        return None

    logger.info(
        "Reclaimed abandoned job %s from %s to %s",
        video_id, current.value, target.value,
    )
    return await get_video(video_id)


# ---------------------------------------------------------------------------
# Client projection
# ---------------------------------------------------------------------------

def job_view(record: dict) -> dict:
    """Project a stored record into the client-facing job shape.

    Deliberately omits `owner`, `stored_filename`, `s3_bucket`, `s3_key`,
    `claim_id`, `lease_expires_at`, and `not_before` - everything that
    describes server-side storage or internal worker bookkeeping. `error_summary` is written by the
    pipeline and is already a safe message, never an exception string.

    `recovered_count` IS exposed: a user whose analysis was interrupted and
    automatically restarted should be able to see that happened.
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
        "recovered_count": int(record.get("recovered_count") or 0),
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
