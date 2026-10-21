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
import uuid
from datetime import datetime, timezone
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

    Raises:
        RemediationNotFound: no such record for this owner.
        InvalidRemediationTransition: the move is not in the state machine.
        RemediationConflict: the record changed underneath us.
    """
    record = await get(remediation_id, owner)
    current = coerce(record.get("state"))

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
    if fields:
        update.update(fields)

    col = await _collection()
    result = await col.update_one(
        {
            "remediation_id": remediation_id,
            "owner": owner,
            "state": current.value,
        },
        {"$set": update},
    )
    if getattr(result, "modified_count", 0) != 1:
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
        "output_video_id": record.get("output_video_id"),
        "verification_video_id": record.get("verification_video_id"),
        "verified_at": iso(record.get("verified_at")),
        "error_code": record.get("error_code"),
        "error_summary": record.get("error_summary"),
        "attempt": int(record.get("attempt") or 0),
        "created_at": iso(record.get("created_at")),
        "updated_at": iso(record.get("updated_at")),
    }


__all__ = (
    "COLLECTION",
    "ID_PREFIX",
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
