"""Remediation API: propose, review, decide, and (not yet) execute.

Phase 8. Conventions followed from `projects.py` and `app.py`: an APIRouter
with a prefix included into the one app, Pydantic request/response schemas,
`Depends(get_current_user)` for authentication, the shared ownership helper
for authorization, and generic 502s for storage failures so no botocore text,
bucket name or key reaches a client.

AUTHORIZATION, ONCE
-------------------
Every route here resolves the caller's identity from the bearer token and the
video's owner from its ownership record. A request body never carries an
owner, a role, or an approver - `approved_by` is written from `user["sub"]`
and nothing else. The repository layer additionally filters every read and
every conditional write by `owner`, so a mistake in a route cannot become a
cross-tenant read.

A record that does not exist and a record belonging to someone else both
answer 404, so these endpoints cannot be used to discover which ids exist.

WHAT `execute` DOES TODAY
-------------------------
It refuses, with 503 and a machine-readable reason, because no image provider
is implemented (see `image_provider.py` for the evidence). It does NOT mark
the record failed: nothing about the remediation failed, the deployment simply
has no generation capability, so the record stays `approved` and executable if
one is ever configured. That distinction is the point of the state machine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

import aws_clients
import aws_resources
import config
import image_provider
import remediation
import remediation_repo
from auth import get_current_user
from job_state import JobState, coerce as coerce_job_state
from ownership import require_video_owner
from remediation import (
    FindingNotFound,
    FindingNotRemediable,
    build_proposal,
    finding_from_frame_item,
)
from remediation_repo import RemediationConflict, RemediationNotFound
from remediation_state import (
    InvalidRemediationTransition,
    RemediationState,
    coerce,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/remediations", tags=["remediation"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProposalCreate(BaseModel):
    """Address of the finding to propose a remediation for.

    `video_id` is validated against the server-generated id shape by the
    ownership helper. `frame_number` and `detection_index` are bounded here:
    an unbounded index would let a caller probe the frame table with arbitrary
    integers, and a negative one is meaningless.
    """

    video_id: str = Field(min_length=1, max_length=64)
    frame_number: int = Field(ge=0, le=100_000)
    detection_index: int = Field(ge=0, le=1_000)


class DecisionRequest(BaseModel):
    """An optional note recorded with an approve/reject/cancel decision.

    Stored verbatim and shown back only to the record's owner. Bounded, and
    never interpolated into a path, a prompt, or a log line.
    """

    note: Optional[str] = Field(default=None, max_length=1000)


class CapabilityResponse(BaseModel):
    """What this deployment can actually do, for the UI to render honestly."""

    remediation_enabled: bool
    proposals_supported: bool
    generation_available: bool
    generation_provider: str
    generation_unavailable_reason: Optional[str] = None
    min_confidence: float
    applies_assets_to_video: bool
    verifies_compliance: bool
    notes: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_enabled() -> None:
    """Refuse every route when the workflow is switched off.

    404 rather than 503: a disabled feature should not advertise its own
    existence on a deployment that has turned it off.
    """
    if not config.load_remediation_enabled():
        raise HTTPException(status_code=404, detail="Not found")


def _frames_table():
    """The shared, timed DynamoDB table handle.

    Resolved per request through `aws_clients` (Phase 6) rather than bound at
    module scope, which is what `app.get_violations` does - so the finite
    timeouts and the bounded retry budget apply, and tests patch one place.
    """
    return aws_clients.dynamodb_table(aws_resources.frames_table_name())


def _read_frame(video_id: str, frame_number: int) -> dict | None:
    """Load one frame row, or None.

    Raises:
        HTTPException: 502 when the analysis store cannot be read. Deliberately
            distinct from "no such frame": a storage outage must never be
            reported as a missing finding, or a proposal would be refused for
            the wrong reason.
    """
    try:
        response = _frames_table().get_item(
            Key={"video_id": video_id, "frame_number": frame_number}
        )
    except (BotoCoreError, ClientError):
        # No botocore detail to the client: it names tables and ARNs.
        logger.exception("DynamoDB read failed for a frame of video %s", video_id)
        raise HTTPException(status_code=502, detail="Analysis store unavailable")
    return response.get("Item")


def _load_finding(video_id: str, frame_number: int, detection_index: int):
    """Load and validate the addressed finding, mapping failures to HTTP.

    404 for "no such finding" and 422 for "exists but is not remediable" are
    different answers on purpose: the first means the address is wrong, the
    second means the stored data cannot support a review.
    """
    item = _read_frame(video_id, frame_number)
    if item is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    try:
        return finding_from_frame_item(item, detection_index, video_id)
    except FindingNotFound:
        raise HTTPException(status_code=404, detail="Finding not found")
    except FindingNotRemediable as exc:
        raise HTTPException(status_code=422, detail=exc.reason)


def _finding_payload(finding: remediation.Finding) -> dict[str, Any]:
    """The evidence snapshot stored on the record.

    A copy, deliberately: the frame row can be overwritten by a re-analysis,
    and a reviewer must be able to see what they were shown even after that
    happens. The live row is still re-read before any consequential action, so
    this snapshot is evidence, never the authority.
    """
    return {
        "frame_number": finding.frame_number,
        "detection_index": finding.detection_index,
        "category": finding.category,
        "description": finding.description,
        "risk_level": finding.risk_level,
        "confidence": finding.confidence,
        "bounding_box": list(finding.bounding_box) if finding.bounding_box else None,
        "timestamp_seconds": finding.timestamp_seconds,
    }


def _proposal_payload(proposal: remediation.Proposal) -> dict[str, Any]:
    return {
        "action": proposal.action,
        "summary": proposal.summary,
        "rationale": proposal.rationale,
        "model_claim": proposal.model_claim,
        "target_description": proposal.target_description,
        "limitations": proposal.limitations,
        "requires_approval": proposal.requires_approval,
        "confidence_gate": proposal.confidence_gate,
        "gate_explanation": proposal.gate_explanation,
    }


async def _supersede_if_finding_changed(record: dict, owner: str) -> dict:
    """Re-read the finding and supersede the record if it no longer matches.

    This is the staleness check that makes an approval mean something. A
    proposal addresses a finding positionally, and a re-analysis overwrites the
    frame row - so between proposal and action the finding at that index may
    have changed or vanished. Acting on a changed finding would apply a
    decision the owner never made.

    Returns the record unchanged when the fingerprint still matches; otherwise
    returns the superseded record (or the original, if it could not be moved).
    """
    video_id = record["video_id"]
    frame_number = int(record["frame_number"])
    detection_index = int(record["detection_index"])

    item = _read_frame(video_id, frame_number)
    current_fingerprint = None
    if item is not None:
        try:
            finding = finding_from_frame_item(item, detection_index, video_id)
            current_fingerprint = finding.fingerprint
        except (FindingNotFound, FindingNotRemediable):
            current_fingerprint = None

    if current_fingerprint == record.get("finding_fingerprint"):
        return record

    reason = (
        "The video was re-analysed and this finding is no longer the same, so "
        "the proposal no longer describes it."
    )
    updated = await remediation_repo.mark_superseded(
        record["remediation_id"], owner, reason
    )
    return updated or record


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
#
# Registered before the parameterised routes so "/remediations/capabilities"
# cannot be captured by "/remediations/{remediation_id}".

@router.get("/capabilities", response_model=CapabilityResponse)
async def get_capabilities(user: dict = Depends(get_current_user)):
    """What this deployment can and cannot do.

    Exists so the UI never has to guess. Every field is read from real
    configuration and from the provider's own answer; nothing here is
    aspirational.
    """
    _require_enabled()
    status_info = image_provider.provider_status()
    return CapabilityResponse(
        remediation_enabled=True,
        proposals_supported=True,
        generation_available=bool(status_info["available"]),
        generation_provider=str(status_info["provider"]),
        generation_unavailable_reason=status_info["reason"],
        min_confidence=config.load_remediation_min_confidence(),
        # Both false, and hardcoded false rather than computed, because no
        # code exists that could make either true. A later phase changes these
        # by implementing the capability, not by flipping a flag.
        applies_assets_to_video=False,
        verifies_compliance=False,
        notes=(
            "This deployment supports proposing and reviewing remediations. "
            "It cannot generate images, cannot modify a video, and cannot "
            "verify that a change resolved a finding. Approving a proposal "
            "records your decision and changes no video."
        ),
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proposal(
    body: ProposalCreate,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """Propose a remediation for one persisted finding.

    Writes nothing to the video and calls no provider. Requires the video's
    analysis to have COMPLETED: a finding is only a settled fact once the job
    reached the state that means analysis finished and its results were
    persisted. Proposing against a running job would let a reviewer approve a
    finding that the same job is about to overwrite.

    Idempotent per finding: a repeat while a record for the same finding is
    still open returns that record with 200 rather than creating a second.
    """
    _require_enabled()
    owner = user["sub"]
    record = await require_video_owner(body.video_id, user)

    job_state = coerce_job_state(record.get("status"))
    if job_state is not JobState.COMPLETED:
        raise _conflict(
            "This video's analysis has not completed, so its findings are not "
            "final. Wait for the analysis to finish before proposing a "
            "remediation."
        )

    finding = _load_finding(
        body.video_id, body.frame_number, body.detection_index
    )

    existing = await remediation_repo.find_open_for_finding(
        body.video_id, owner, body.frame_number, body.detection_index
    )
    if existing is not None:
        # Not an error: the caller asked for a proposal for this finding and
        # there is one. Returning it keeps a double-click from producing two
        # competing records. 200 rather than 201, because nothing was created -
        # a client that distinguishes the two is told the truth.
        response.status_code = status.HTTP_200_OK
        return remediation_repo.view(existing)

    if (
        await remediation_repo.count_for_video(body.video_id, owner)
        >= config.load_max_remediations_per_video()
    ):
        raise _conflict(
            "This video already has the maximum number of remediation records."
        )

    proposal = build_proposal(finding)
    created = await remediation_repo.create(
        video_id=body.video_id,
        owner=owner,
        frame_number=finding.frame_number,
        detection_index=finding.detection_index,
        finding_fingerprint=finding.fingerprint,
        finding=_finding_payload(finding),
        proposal=_proposal_payload(proposal),
    )
    return remediation_repo.view(created)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@router.get("")
async def list_proposals(
    video_id: str = Query(min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """List the caller's remediation records for one video.

    The ownership check runs first, so a request for someone else's video id
    answers 404 without the remediation collection being consulted at all.
    """
    _require_enabled()
    await require_video_owner(video_id, user)
    records = await remediation_repo.list_for_video(video_id, user["sub"])
    return {
        "video_id": video_id,
        "remediations": [remediation_repo.view(r) for r in records],
    }


@router.get("/{remediation_id}")
async def get_proposal(
    remediation_id: str, user: dict = Depends(get_current_user)
):
    """One remediation record owned by the caller."""
    _require_enabled()
    try:
        record = await remediation_repo.get(remediation_id, user["sub"])
    except RemediationNotFound:
        raise HTTPException(status_code=404, detail="Remediation not found")
    return remediation_repo.view(record)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

async def _load_owned(remediation_id: str, owner: str) -> dict:
    try:
        return await remediation_repo.get(remediation_id, owner)
    except RemediationNotFound:
        raise HTTPException(status_code=404, detail="Remediation not found")


async def _decide(
    remediation_id: str,
    user: dict,
    target: RemediationState,
    note: str | None,
    extra: dict[str, Any] | None = None,
) -> dict:
    """Apply one owner decision, idempotently and under a conditional write."""
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)
    current = coerce(record.get("state"))

    # Idempotent repeat: the record is already where the caller is asking for
    # it to be. Returned as success rather than as a conflict, so a retried
    # request after a dropped response does not look like a failure.
    if current is target:
        return remediation_repo.view(record)

    fields: dict[str, Any] = {
        "decided_by": owner,
        "decided_at": datetime.now(timezone.utc),
        "decision_note": note,
    }
    if extra:
        fields.update(extra)

    try:
        updated = await remediation_repo.transition(
            remediation_id, owner, target, expected=current, fields=fields
        )
    except InvalidRemediationTransition:
        raise _conflict(
            f"A remediation that is {current.value} cannot be moved to "
            f"{target.value}."
        )
    except RemediationConflict:
        # Another request got there first. 409 with the real current state, so
        # the client can re-render rather than guess.
        raise _conflict(
            "This remediation was changed by another request; reload it and "
            "try again."
        )
    except RemediationNotFound:
        raise HTTPException(status_code=404, detail="Remediation not found")

    return remediation_repo.view(updated)


@router.post("/{remediation_id}/approve")
async def approve_proposal(
    remediation_id: str,
    body: DecisionRequest | None = None,
    user: dict = Depends(get_current_user),
):
    """Record the owner's approval of a proposal.

    Approval is a decision, not an action: it authorises a later attempt and
    changes no video. The approving identity comes from the authenticated
    token and from nowhere else.

    The finding is re-read first. If the video has been re-analysed and the
    finding changed, the record is superseded and this answers 409 - a stale
    proposal must not be approvable.
    """
    _require_enabled()
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)

    if coerce(record.get("state")) is RemediationState.PROPOSED:
        record = await _supersede_if_finding_changed(record, owner)
        if coerce(record.get("state")) is RemediationState.SUPERSEDED:
            raise _conflict(
                record.get("error_summary")
                or "This proposal no longer matches the stored finding."
            )

    now = datetime.now(timezone.utc)
    return await _decide(
        remediation_id,
        user,
        RemediationState.APPROVED,
        (body.note if body else None),
        extra={"approved_by": owner, "approved_at": now},
    )


@router.post("/{remediation_id}/reject")
async def reject_proposal(
    remediation_id: str,
    body: DecisionRequest | None = None,
    user: dict = Depends(get_current_user),
):
    """Record the owner declining a proposal. Terminal."""
    _require_enabled()
    return await _decide(
        remediation_id,
        user,
        RemediationState.REJECTED,
        (body.note if body else None),
    )


@router.post("/{remediation_id}/cancel")
async def cancel_proposal(
    remediation_id: str,
    body: DecisionRequest | None = None,
    user: dict = Depends(get_current_user),
):
    """Withdraw a proposal, including one already approved.

    Exists so approval is not irreversible: an owner who approves and then
    changes their mind has a way back before anything consequential happens.
    Legal from `proposed` and from `approved`, and not after - once an asset
    exists it is real whatever the owner now wants, and a state that hid it
    would orphan the object.
    """
    _require_enabled()
    return await _decide(
        remediation_id,
        user,
        RemediationState.CANCELLED,
        (body.note if body else None),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@router.post("/{remediation_id}/execute")
async def execute_proposal(
    remediation_id: str, user: dict = Depends(get_current_user)
):
    """Attempt the approved remediation.

    Preconditions, checked in this order and all of them server-side:

    1. the caller owns the record (404 otherwise);
    2. the record is `approved` (409 otherwise) - an unapproved proposal can
       never be executed, and hiding the button is not what enforces that;
    3. the stored finding still matches (superseded + 409 otherwise);
    4. the finding met the configured confidence threshold (409 otherwise) -
       this is what `REMEDIATION_MIN_CONFIDENCE` actually gates;
    5. an image provider is available.

    (5) currently always fails. The response is 503 with the provider's own
    reason and the record is left `approved`, because the deployment lacking a
    capability is not a failure of the remediation. Nothing is written.
    """
    _require_enabled()
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)
    current = coerce(record.get("state"))

    if current is not RemediationState.APPROVED:
        raise _conflict(
            f"Only an approved remediation can be executed; this one is "
            f"{current.value}."
        )

    record = await _supersede_if_finding_changed(record, owner)
    if coerce(record.get("state")) is RemediationState.SUPERSEDED:
        raise _conflict(
            record.get("error_summary")
            or "This remediation no longer matches the stored finding."
        )

    gate = (record.get("proposal") or {}).get("confidence_gate")
    if gate != remediation.GATE_PASS:
        raise _conflict(
            "This finding did not meet the configured confidence threshold, "
            "so automated generation is refused for it. Review it manually."
        )

    provider = image_provider.get_provider()
    if not provider.is_available():
        # No state change: see the module docstring. Nothing has been
        # attempted, so nothing has failed.
        logger.info(
            "Remediation execution refused: no image provider (provider=%s)",
            provider.name,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                provider.unavailable_reason()
                or "Automated image generation is not available."
            ),
        )

    # Unreachable while NullImageProvider is the only implementation. Left as
    # an explicit refusal rather than a half-written generation path, so no
    # reader can mistake this for a working pipeline: a provider that reports
    # itself available but has no execution path wired must fail loudly.
    logger.error(
        "Image provider %s reports itself available but no execution path is "
        "implemented",
        provider.name,
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Automated image generation is not available on this deployment."
        ),
    )


__all__ = ("router",)
