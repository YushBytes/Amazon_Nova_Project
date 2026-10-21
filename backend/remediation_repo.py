"""Durable storage for remediation records.

Phase 8.

WHY MONGODB, AND WHY A NEW COLLECTION
-------------------------------------
MongoDB `remediations`, alongside `videos` and `users` in the same database.

Not DynamoDB: a remediation's lifecycle needs a conditional multi-field write
keyed on its CURRENT state, which is exactly the pattern `video_repo` already
implements with `update_one(filter_including_state, {"$set": ...})` and a
`modified_count` check. Reusing it means one concurrency idiom in the backend
rather than two. CLAUDE.md §9 q2 (consolidate onto DynamoDB?) is still open,
so this deliberately does NOT add a third storage pattern to that decision -
it follows the one the ownership and job-state aggregate already uses, and
moves with it if q2 is ever resolved.

Not a field on the `videos` document: a video has many findings and may have
many proposals, with their own independent lifecycles. Embedding an unbounded,
individually-transitioning array inside the document the worker conditionally
updates on every heartbeat would make two unrelated writers contend on one
document, and `video_repo`'s state conditions would start matching for
reasons that have nothing to do with job state.

MIGRATION AND COMPATIBILITY
---------------------------
Purely additive. The collection is created on first insert, `ensure_indexes`
is idempotent, and no existing document is read or written by this module.
A deployment that never calls a remediation route never creates the
collection, and rolling back to a build without this module leaves the
collection unread - no `videos` or `users` document is touched either way.

OWNERSHIP
---------
Every record stores `owner`, copied from the ownership record of the video at
creation time and never from a request body. Every read is filtered by it as
well as by id, so an authorization bug in a route cannot turn into a
cross-tenant read at this layer. A record whose owner does not match is
reported as absent, never as forbidden.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from db import get_database
from remediation_state import (
    InvalidRemediationTransition,
    RemediationState,
    assert_transition,
    coerce,
)

logger = logging.getLogger(__name__)

COLLECTION = "remediations"

# "rem_" + 12 hex. Server-generated; nothing client-supplied is used.
ID_PREFIX = "rem_"

# Phase 9. Bounded attempts for one remediation's reconstruction, mirroring
# `job_state.MAX_ATTEMPTS` for video jobs. Reconstruction costs FFmpeg time and
# the re-analysis it leads to costs Bedrock per frame, so a persistently
# failing remediation must not be retried forever.
MAX_EXECUTION_ATTEMPTS = 3


def execution_lease_seconds() -> int:
    """How long a worker's claim on a reconstruction stays valid.

    Separate from `JOB_LEASE_SECONDS` because the work is different: a
    reconstruction is one FFmpeg pass over one clip, not a per-frame model
    loop. It must still comfortably exceed the longest plausible run, or a
    live worker gets its work reclaimed underneath it.
    """
    raw = (os.getenv("REMEDIATION_LEASE_SECONDS") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 600
    return value if value >= 30 else 600


class ClaimLost(InvalidRemediationTransition):
    """A write was refused because this worker no longer holds the claim.

    The fencing token doing its job: a worker whose work was reclaimed by a
    recovery sweep cannot write anything further, so it cannot overwrite a
    newer attempt's output or verification.
    """


class RemediationNotFound(LookupError):
    """No such remediation for this owner.

    One exception for "does not exist" and "not yours", deliberately. A route
    turns it into 404 either way, which is what stops the endpoint being used
    to discover which ids exist - the same rule the video routes follow.
    """


class RemediationConflict(RuntimeError):
    """The record was not in the state the caller believed.

    Raised when a conditional update matches nothing: another request already
    advanced or terminated this record. The caller answers 409.
    """


def new_remediation_id() -> str:
    return f"{ID_PREFIX}{uuid.uuid4().hex[:12]}"


async def _collection():
    database = await get_database()
    return database[COLLECTION]


async def ensure_indexes() -> None:
    """Create the indexes this collection relies on. Safe to call repeatedly.

    `remediation_id` is unique so a generated id collision cannot produce two
    records. `owner` and `video_id` are indexed because every list query
    filters on them, and the state index serves the per-video status rollup.
    """
    col = await _collection()
    await col.create_index("remediation_id", unique=True)
    await col.create_index("owner")
    await col.create_index("video_id")
    await col.create_index("state")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

async def count_for_video(video_id: str, owner: str) -> int:
    """How many records exist for this video, for the per-video cap."""
    col = await _collection()
    return await col.count_documents({"video_id": video_id, "owner": owner})


async def find_open_for_finding(
    video_id: str,
    owner: str,
    frame_number: int,
    detection_index: int,
) -> dict | None:
    """An existing non-terminal record for the same finding, if any.

    This is what makes proposal creation idempotent in the way that matters:
    a double-clicked "propose" must not produce two records competing to
    remediate one finding. A TERMINAL record is not returned - a rejected
    proposal should not block proposing again after a re-analysis, and a
    superseded one certainly should not.
    """
    col = await _collection()
    open_states = sorted(
        state.value for state in RemediationState if not state.is_terminal
    )
    return await col.find_one(
        {
            "video_id": video_id,
            "owner": owner,
            "frame_number": int(frame_number),
            "detection_index": int(detection_index),
            "state": {"$in": open_states},
        }
    )


async def create(
    *,
    video_id: str,
    owner: str,
    frame_number: int,
    detection_index: int,
    finding_fingerprint: str,
    finding: dict[str, Any],
    proposal: dict[str, Any],
) -> dict:
    """Insert a new proposal in the `proposed` state.

    The state is written by this function, never taken from a caller: a record
    can only ever begin life as `proposed`, so there is no argument through
    which something could be created already approved.
    """
    now = _now()
    document = {
        "remediation_id": new_remediation_id(),
        "video_id": video_id,
        # Copied from the video's ownership record by the caller, which has
        # already authorized this user for this video.
        "owner": owner,
        "frame_number": int(frame_number),
        "detection_index": int(detection_index),
        "finding_fingerprint": finding_fingerprint,
        "finding": finding,
        "proposal": proposal,
        "state": RemediationState.PROPOSED.value,
        # Approval identity is recorded only when an approval happens, and
        # only from the authenticated token's subject.
        "approved_by": None,
        "approved_at": None,
        "decided_by": None,
        "decided_at": None,
        "decision_note": None,
        # Provider and asset fields stay null until something real exists.
        # They are present from the start so a reader never has to distinguish
        # "absent because nothing happened" from "absent because the schema
        # changed".
        "provider": None,
        "provider_model_id": None,
        "asset_key": None,
        "asset_bytes": None,
        "asset_content_type": None,
        "output_video_id": None,
        "verification_video_id": None,
        "verified_at": None,
        "error_code": None,
        "error_summary": None,
        "attempt": 0,
        # --- Phase 9 -----------------------------------------------------
        # Where the asset came from: "upload" for an owner-supplied file, or
        # a provider name once one exists. Recorded, never inferred.
        "asset_source": None,
        "asset_supplied_by": None,
        "asset_supplied_at": None,
        # The output video's storage key. Server-side only; `view` never
        # emits it.
        "output_key": None,
        "output_bytes": None,
        "output_duration_seconds": None,
        "output_has_audio": None,
        # Audit trail of the reconstruction that produced the output.
        "reconstruction": None,
        # The analysis job for the OUTPUT video. `output_video_id` doubles as
        # the job id because the output is a first-class video record, which
        # is what lets the existing pipeline analyse it unchanged.
        "analysis_job_video_id": None,
        "analysis_submitted_at": None,
        # Verification evidence. Written only by `verification.py`.
        "verification": None,
        # Worker claim and lease, mirroring the video record. This is the
        # fencing token for reconstruction.
        "claim_id": None,
        "lease_expires_at": None,
        "not_before": None,
        "recovered_count": 0,
        # Phase 9. An asset existing is NOT a request to build a video: the
        # owner asks for that separately, and this flag is the durable record
        # of having asked. Set by POST /remediations/{id}/execute in a
        # conditional write BEFORE that route responds, so an accepted request
        # survives an API restart - the worker finds it on its next poll.
        "execution_requested": False,
        "execution_requested_at": None,
        "created_at": now,
        "updated_at": now,
    }
    col = await _collection()
    await col.insert_one(document)
    logger.info(
        "Remediation proposed for frame %s of a video (state=proposed)",
        frame_number,
    )
    return await get(document["remediation_id"], owner)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def get(remediation_id: str, owner: str) -> dict:
    """Load one record owned by this user.

    Raises:
        RemediationNotFound: no such record, or it belongs to someone else.
    """
    col = await _collection()
    record = await col.find_one(
        {"remediation_id": remediation_id, "owner": owner}
    )
    if record is None:
        raise RemediationNotFound(remediation_id)
    return record


async def list_for_video(
    video_id: str, owner: str, limit: int = 200
) -> list[dict]:
    """This user's records for one video, newest first."""
    col = await _collection()
    cursor = (
        col.find({"video_id": video_id, "owner": owner})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

async def transition(
    remediation_id: str,
    owner: str,
    target: RemediationState,
    *,
    expected: RemediationState | None = None,
    fields: dict[str, Any] | None = None,
    claim_id: str | None = None,
) -> dict:
    """Move a record to `target`, validating the move first.

    The write is conditional on the state the record is believed to be in, so
    two concurrent requests cannot both apply a transition: the second matches
    zero documents and raises RemediationConflict. This is the same mechanism
    `video_repo.transition` uses, and it is what makes approve/reject/execute
    safe without a lock.

    The filter also includes `owner`, so even a caller that skipped the route
    authorization could not transition another user's record.

    Args:
        remediation_id: the record to move.
        owner: the authenticated user; part of the write condition.
        target: the state to move to.
        expected: the state the record must currently be in. Defaults to
            whatever is stored at call time, which still rejects a concurrent
            change because the conditional update then matches nothing.
        fields: additional fields to persist in the same write, so a state
            change and the facts that justify it are one atomic write.
        claim_id: when given, the write also requires this claim to still
            hold the lease (Phase 9). This is the fencing token - a worker
            whose work was reclaimed cannot write anything further, so it
            cannot attach its output or its verification to a newer attempt.

    Raises:
        RemediationNotFound: no such record for this owner.
        InvalidRemediationTransition: the move is not in the state machine.
        ClaimLost: `claim_id` was given and no longer matches.
        RemediationConflict: the record changed underneath us.
    """
    record = await get(remediation_id, owner)
    current = coerce(record.get("state"))

    # Checked before the state, deliberately, and for the reason
    # `video_repo.transition` gives: when a sweep reclaims work it changes
    # both the claim and the state, and "you no longer hold this" is the
    # specific, actionable truth. Reporting the state mismatch first would
    # make a fenced worker look like an ordinary lost race.
    if claim_id is not None and record.get("claim_id") != claim_id:
        raise ClaimLost(
            f"remediation {remediation_id} is no longer held by this worker; "
            f"the move to {coerce(target).value} was not applied"
        )

    if expected is not None and current is not coerce(expected):
        raise RemediationConflict(
            f"remediation is {current.value}, not {coerce(expected).value}"
        )

    # Validated BEFORE the write is attempted, so an illegal move is reported
    # as illegal rather than as a lost race.
    target_state = assert_transition(current, target)

    update: dict[str, Any] = {
        "state": target_state.value,
        "updated_at": _now(),
    }
    if target_state.is_terminal or target_state is RemediationState.APPLIED:
        # A terminal record holds no lease: there is nothing left to fence,
        # and leaving one would make it look claimed to an operator. APPLIED
        # releases it too - reconstruction is over at that point, and the
        # analysis that follows is fenced by the OUTPUT VIDEO's own job claim.
        update["claim_id"] = None
        update["lease_expires_at"] = None
    if fields:
        update.update(fields)

    condition: dict[str, Any] = {
        "remediation_id": remediation_id,
        "owner": owner,
        "state": current.value,
    }
    if claim_id is not None:
        condition["claim_id"] = claim_id

    col = await _collection()
    result = await col.update_one(condition, {"$set": update})
    if getattr(result, "modified_count", 0) != 1:
        if claim_id is not None:
            raise ClaimLost(
                f"remediation {remediation_id} is no longer held by this "
                f"worker; the move to {target_state.value} was not applied"
            )
        raise RemediationConflict(
            f"remediation changed state concurrently; the move to "
            f"{target_state.value} was not applied"
        )

    return await get(remediation_id, owner)


async def mark_superseded(
    remediation_id: str, owner: str, reason: str
) -> dict | None:
    """Move a record to `superseded`, tolerating a record already past it.

    Called when the underlying finding no longer matches the fingerprint. The
    caller is in the middle of answering a request and must not fail because
    the record happened to be in a state from which `superseded` is not
    reachable (an asset already exists, or it is already terminal) - that is
    not an error, it just means there is nothing to supersede. Returns the
    updated record, or None when no move was made.
    """
    try:
        return await transition(
            remediation_id,
            owner,
            RemediationState.SUPERSEDED,
            fields={
                "error_code": "finding_changed",
                "error_summary": reason,
            },
        )
    except (InvalidRemediationTransition, RemediationConflict):
        logger.info(
            "Remediation %s could not be marked superseded; leaving as is",
            remediation_id,
        )
        return None


# ---------------------------------------------------------------------------
# Worker claims and leases (Phase 9)
# ---------------------------------------------------------------------------
#
# Reconstruction is real work - an FFmpeg pass plus two S3 transfers - so it
# runs in the same worker process that already drains the video queue, not in
# a request handler. The mechanism is deliberately the one `video_repo`
# already uses rather than a new one:
#
#     claim_id          a uuid4 minted by the worker that won the claim
#     lease_expires_at  UTC instant after which that claim is considered dead
#     not_before        earliest instant this record may be claimed again
#     attempt           bounded by MAX_EXECUTION_ATTEMPTS
#
# Every subsequent write by that worker passes `claim_id` to `transition`,
# which makes the update conditional on it still matching. That is the fencing
# token: if a recovery sweep reclaims the record and another worker takes it,
# the original worker's next write matches zero documents and it stops. A
# stale worker therefore cannot attach ITS output video or ITS verification to
# a newer attempt - which, unlike a lost video job, would otherwise associate
# the wrong file with a compliance claim.


async def claim_for_execution(remediation_id: str, owner: str) -> tuple[dict, str]:
    """Take exclusive ownership of one remediation's reconstruction.

    Moves ASSET_READY -> RECONSTRUCTING, conditional on the lease being free,
    and returns the record with the minted claim id. Two workers racing on one
    record cannot both win: the second's update matches nothing.

    Raises:
        RemediationNotFound: no such record for this owner.
        RemediationConflict: it is held by another worker, or changed state.
        InvalidRemediationTransition: it is not in a claimable state.
    """
    record = await get(remediation_id, owner)
    current = coerce(record.get("state"))
    # Validated before the write so "not claimable" is distinguishable from
    # "lost the race".
    target = assert_transition(current, RemediationState.RECONSTRUCTING)

    now = _now()
    claim_id = uuid.uuid4().hex
    attempt = int(record.get("attempt") or 0) + 1

    col = await _collection()
    result = await col.update_one(
        {
            "remediation_id": remediation_id,
            "owner": owner,
            "state": current.value,
            # "Lease is free" means nobody holds it, or whoever did let it
            # expire. `{"claim_id": None}` also matches a document where the
            # field is absent, which is what a Phase 8 record looks like.
            "$or": [
                {"claim_id": None},
                {"lease_expires_at": None},
                {"lease_expires_at": {"$lt": now}},
            ],
        },
        {
            "$set": {
                "state": target.value,
                "claim_id": claim_id,
                "lease_expires_at": now + timedelta(seconds=execution_lease_seconds()),
                "attempt": attempt,
                # A new attempt must not inherit the previous one's failure, or
                # a stale summary would be read as current.
                "error_code": None,
                "error_summary": None,
                "updated_at": now,
            }
        },
    )
    if getattr(result, "modified_count", 0) != 1:
        raise RemediationConflict(
            f"remediation {remediation_id} could not be claimed; it is held "
            f"by another worker or changed state concurrently"
        )

    return await get(remediation_id, owner), claim_id


async def request_execution(remediation_id: str, owner: str) -> dict | None:
    """Durably record that the owner asked for the video to be built.

    A conditional write, so it is both idempotent and race-safe: the filter
    requires the record to be ASSET_READY, and setting an already-set flag
    matches the document but changes nothing, which `matched_count` tells
    apart from "no such claimable record".

    This write happens BEFORE the route responds, which is what makes an
    accepted request durable: if the API restarts, the worker still finds the
    flag on its next poll. Nothing is handed to an in-process task.

    Returns the updated record, or None when the record is not in a state
    that can accept the request.
    """
    col = await _collection()
    now = _now()
    result = await col.update_one(
        {
            "remediation_id": remediation_id,
            "owner": owner,
            "state": RemediationState.ASSET_READY.value,
        },
        {
            "$set": {
                "execution_requested": True,
                "execution_requested_at": now,
                # A fresh request clears any backoff from a previous retryable
                # failure: the owner asking again is an explicit instruction
                # to try now.
                "not_before": None,
                "updated_at": now,
            }
        },
    )
    if getattr(result, "matched_count", 0) != 1:
        return None
    return await get(remediation_id, owner)


async def heartbeat(remediation_id: str, owner: str, claim_id: str) -> None:
    """Extend the lease while reconstruction is making progress.

    Conditional on the claim, so a worker that has been fenced cannot revive
    its own lease. Silent when it no longer holds the claim: the next write it
    attempts raises ClaimLost, which is where it should stop.
    """
    col = await _collection()
    await col.update_one(
        {"remediation_id": remediation_id, "owner": owner, "claim_id": claim_id},
        {
            "$set": {
                "lease_expires_at": _now()
                + timedelta(seconds=execution_lease_seconds()),
                "updated_at": _now(),
            }
        },
    )


async def holds_claim(remediation_id: str, owner: str, claim_id: str) -> bool:
    """Whether this claim still holds the record."""
    try:
        record = await get(remediation_id, owner)
    except RemediationNotFound:
        return False
    return record.get("claim_id") == claim_id


async def find_claimable(limit: int = 5) -> list[dict]:
    """Records whose reconstruction is ready to run, oldest first.

    Across ALL owners: this is the worker's queue poll, and the worker is not
    acting for a user. Ownership is still carried on each record and every
    subsequent write is filtered by it, so the work stays attributed.

    Excludes records whose `not_before` is in the future (a retry backoff) and
    those that have exhausted their attempts.
    """
    col = await _collection()
    now = _now()
    cursor = (
        col.find(
            {
                "state": RemediationState.ASSET_READY.value,
                # An asset alone is not a request to build a video.
                "execution_requested": True,
                "attempt": {"$lt": MAX_EXECUTION_ATTEMPTS},
                "$or": [{"not_before": None}, {"not_before": {"$lte": now}}],
            }
        )
        .sort("updated_at", 1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def find_awaiting_verification(limit: int = 25) -> list[dict]:
    """Records whose output analysis may have finished.

    VERIFYING means an analysis job exists for the output video. Nothing here
    decides the outcome - `verification.py` reads the persisted results and
    does that. This is only the list of records worth looking at.
    """
    col = await _collection()
    cursor = (
        col.find({"state": RemediationState.VERIFYING.value})
        .sort("updated_at", 1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def find_expired_execution_leases(limit: int = 25) -> list[dict]:
    """Reconstructions abandoned by a worker that died mid-run."""
    col = await _collection()
    now = _now()
    cursor = (
        col.find(
            {
                "state": RemediationState.RECONSTRUCTING.value,
                "lease_expires_at": {"$lt": now},
            }
        )
        .sort("updated_at", 1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def reclaim(record: dict) -> dict | None:
    """Return one abandoned reconstruction to the claimable state.

    RECONSTRUCTING -> ASSET_READY, conditional on the DEAD worker's claim id
    still being the one stored. That condition is what stops a sweep taking a
    record away from a worker that is actually alive: if the worker
    heartbeated between the sweep's read and this write, the lease is no
    longer expired and the claim may have changed, so the update matches
    nothing.

    Returns the updated record, or None when the reclaim did not apply.
    """
    remediation_id = record.get("remediation_id")
    owner = record.get("owner")
    dead_claim = record.get("claim_id")
    if not remediation_id or not owner:
        return None

    attempt = int(record.get("attempt") or 0)
    if attempt >= MAX_EXECUTION_ATTEMPTS:
        # Out of attempts: fail it terminally rather than requeueing forever.
        try:
            return await transition(
                remediation_id,
                owner,
                RemediationState.FAILED,
                fields={
                    "error_code": "execution_abandoned",
                    "error_summary": (
                        "The reconstruction was interrupted and has used all "
                        "of its attempts. Supply the asset again to retry."
                    ),
                },
            )
        except (InvalidRemediationTransition, RemediationConflict):
            return None

    now = _now()
    col = await _collection()
    result = await col.update_one(
        {
            "remediation_id": remediation_id,
            "owner": owner,
            "state": RemediationState.RECONSTRUCTING.value,
            "claim_id": dead_claim,
            "lease_expires_at": {"$lt": now},
        },
        {
            "$set": {
                "state": RemediationState.ASSET_READY.value,
                "claim_id": None,
                "lease_expires_at": None,
                # Visible to the owner: an interrupted run that was restarted
                # automatically should not be invisible.
                "recovered_count": int(record.get("recovered_count") or 0) + 1,
                "updated_at": now,
            }
        },
    )
    if getattr(result, "modified_count", 0) != 1:
        return None
    logger.info(
        "Reclaimed an abandoned reconstruction (attempt %d)", attempt
    )
    return await get(remediation_id, owner)


async def schedule_execution_retry(
    remediation_id: str,
    owner: str,
    claim_id: str,
    *,
    delay_seconds: int,
    error_code: str,
    error_summary: str,
) -> dict | None:
    """Return a record to ASSET_READY after a RETRYABLE failure, with backoff.

    Without `not_before` the worker would reclaim the record on its next poll
    and spend the whole attempt budget within seconds of a transient blip.

    Only for failures that could plausibly succeed later. A deterministic
    failure - a malformed box, an unreadable asset, a source that cannot be
    probed - must fail terminally instead, because retrying bills the same
    answer three times.
    """
    now = _now()
    col = await _collection()
    result = await col.update_one(
        {
            "remediation_id": remediation_id,
            "owner": owner,
            "state": RemediationState.RECONSTRUCTING.value,
            "claim_id": claim_id,
        },
        {
            "$set": {
                "state": RemediationState.ASSET_READY.value,
                "claim_id": None,
                "lease_expires_at": None,
                "not_before": now + timedelta(seconds=max(1, delay_seconds)),
                "error_code": error_code,
                "error_summary": error_summary,
                "updated_at": now,
            }
        },
    )
    if getattr(result, "modified_count", 0) != 1:
        return None
    return await get(remediation_id, owner)


# ---------------------------------------------------------------------------
# The client-facing projection
# ---------------------------------------------------------------------------

def view(record: dict) -> dict:
    """Project a stored record into the shape a client receives.

    Omits `owner` and `asset_key`. The key is server-side storage detail; a
    client is told only WHETHER an asset exists and its size and type, and
    reaches it through an authorized route rather than by naming an object.

    `describes_modified_source` and `is_compliance_claim` are computed from the
    state here rather than left to the client, so no caller has to re-derive
    the one rule that matters.
    """
    from remediation_state import describes_modified_source, stage_label

    state = coerce(record.get("state"))

    def iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else None

    return {
        "remediation_id": record.get("remediation_id"),
        "video_id": record.get("video_id"),
        "frame_number": record.get("frame_number"),
        "detection_index": record.get("detection_index"),
        "state": state.value,
        "stage": stage_label(state),
        "is_terminal": state.is_terminal,
        "is_active": state.is_active,
        # The single authoritative answer to "may this be called compliant?".
        "is_compliance_claim": state.is_compliance_claim,
        "source_video_modified": describes_modified_source(state),
        "finding": record.get("finding"),
        "proposal": record.get("proposal"),
        "approved_by": record.get("approved_by"),
        "approved_at": iso(record.get("approved_at")),
        "decided_by": record.get("decided_by"),
        "decided_at": iso(record.get("decided_at")),
        "decision_note": record.get("decision_note"),
        "provider": record.get("provider"),
        "provider_model_id": record.get("provider_model_id"),
        # Presence and shape only - never the key.
        "has_asset": bool(record.get("asset_key")),
        "asset_bytes": record.get("asset_bytes"),
        "asset_content_type": record.get("asset_content_type"),
        "asset_source": record.get("asset_source"),
        "asset_supplied_by": record.get("asset_supplied_by"),
        "asset_supplied_at": iso(record.get("asset_supplied_at")),
        # The output video's ID is safe to publish: it is how the client asks
        # for playback through the normal authorized media route, which
        # re-checks ownership. Its storage KEY is not published.
        "output_video_id": record.get("output_video_id"),
        "has_output": bool(record.get("output_key")),
        "output_bytes": record.get("output_bytes"),
        "output_duration_seconds": record.get("output_duration_seconds"),
        "output_has_audio": record.get("output_has_audio"),
        "reconstruction": record.get("reconstruction"),
        "analysis_job_video_id": record.get("analysis_job_video_id"),
        "analysis_submitted_at": iso(record.get("analysis_submitted_at")),
        # Written only by `verification.py`, from persisted analysis results.
        "verification": record.get("verification"),
        "verification_video_id": record.get("verification_video_id"),
        "verified_at": iso(record.get("verified_at")),
        "error_code": record.get("error_code"),
        "error_summary": record.get("error_summary"),
        "attempt": int(record.get("attempt") or 0),
        "max_attempts": MAX_EXECUTION_ATTEMPTS,
        "recovered_count": int(record.get("recovered_count") or 0),
        "created_at": iso(record.get("created_at")),
        "updated_at": iso(record.get("updated_at")),
    }


__all__ = (
    "COLLECTION",
    "ClaimLost",
    "ID_PREFIX",
    "MAX_EXECUTION_ATTEMPTS",
    "claim_for_execution",
    "execution_lease_seconds",
    "find_awaiting_verification",
    "find_claimable",
    "find_expired_execution_leases",
    "heartbeat",
    "request_execution",
    "holds_claim",
    "reclaim",
    "schedule_execution_retry",
    "RemediationConflict",
    "RemediationNotFound",
    "count_for_video",
    "create",
    "ensure_indexes",
    "find_open_for_finding",
    "get",
    "list_for_video",
    "mark_superseded",
    "new_remediation_id",
    "transition",
    "view",
)
