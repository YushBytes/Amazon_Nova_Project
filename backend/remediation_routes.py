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
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import aws_clients
import aws_resources
import config
import image_provider
import remediation
import remediation_assets
import remediation_executor
import remediation_repo
import verification
from auth import create_media_token, get_current_user
from job_state import JobState, coerce as coerce_job_state
from ownership import require_video_owner
from remediation import (
    FindingNotFound,
    FindingNotRemediable,
    build_proposal,
    finding_from_frame_item,
)
from remediation_assets import AssetRejected, AssetStoreError
from remediation_executor import ExecutionError, NotExecutable
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
    # --- Phase 9 --------------------------------------------------------
    # Whether an owner may supply the replacement image themselves. This is
    # what makes an end-to-end correction possible with no image provider.
    asset_upload_supported: bool = False
    max_asset_bytes: int = 0
    accepted_asset_types: list[str] = Field(default_factory=list)
    # Whether an approved remediation can actually be built into a video.
    reconstruction_available: bool = False
    reconstruction_unavailable_reason: Optional[str] = None
    reconstruction_method: Optional[str] = None
    verification_policy: Optional[str] = None


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
    reconstruction_enabled = config.load_reconstruction_enabled()

    return CapabilityResponse(
        remediation_enabled=True,
        proposals_supported=True,
        generation_available=bool(status_info["available"]),
        generation_provider=str(status_info["provider"]),
        generation_unavailable_reason=status_info["reason"],
        min_confidence=config.load_remediation_min_confidence(),
        # Phase 9 made both of these true, through the supplied-asset path.
        # They are read from configuration rather than hardcoded now, because
        # a deployment without the FFmpeg executable genuinely cannot do it.
        applies_assets_to_video=reconstruction_enabled,
        verifies_compliance=reconstruction_enabled,
        asset_upload_supported=True,
        max_asset_bytes=config.load_max_asset_bytes(),
        accepted_asset_types=["image/png", "image/jpeg"],
        reconstruction_available=reconstruction_enabled,
        reconstruction_unavailable_reason=(
            None
            if reconstruction_enabled
            else (
                "Building corrected videos is not enabled on this deployment."
            )
        ),
        # Named plainly so nothing downstream can read more into it. The
        # replacement is scaled and drawn over the approved region with hard
        # edges; it conceals that region and does not inpaint, match grain or
        # track motion.
        reconstruction_method="scaled_overlay",
        verification_policy=config.load_verification_policy(),
        notes=(
            "This deployment supports proposing and reviewing remediations, "
            "and building a corrected video from a replacement image you "
            "supply. It cannot generate that image: no image provider is "
            "configured. A correction is only reported as verified after the "
            "corrected video has been analysed in its own right and the "
            "configured policy passes. Your original video is never modified."
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
# Supplying the replacement asset (Phase 9)
# ---------------------------------------------------------------------------

@router.post("/{remediation_id}/asset")
async def upload_asset(
    remediation_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Attach an owner-supplied replacement image to an approved remediation.

    This is the path that makes an end-to-end correction possible without an
    image provider: the owner provides the bytes a provider would otherwise
    have generated. It is NOT a way around review - the remediation must
    already be `approved`, by this caller, and the finding is re-checked
    first.

    Nothing the client says about the file is trusted. The size limit is
    enforced on the bytes actually received, and the media type is determined
    by sniffing them; the declared filename and content type are ignored for
    every decision, and no part of the filename reaches a storage key.

    Moves `approved` -> `asset_ready`. It does NOT start a reconstruction:
    POST /{id}/execute is a separate, explicit request.
    """
    _require_enabled()
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)
    current = coerce(record.get("state"))

    if current is RemediationState.ASSET_READY and record.get("asset_key"):
        # Idempotent-ish: an asset is already attached. Replacing it would
        # change what was reviewed, so the caller is told to withdraw and
        # start again rather than silently swapping the file.
        raise _conflict(
            "A replacement image is already attached to this remediation. "
            "Withdraw it and propose again to supply a different one."
        )
    if current is not RemediationState.APPROVED:
        raise _conflict(
            f"A replacement image can only be attached to an approved "
            f"remediation; this one is {current.value}."
        )

    # The finding must still be the one that was approved. Attaching an asset
    # is consequential - it is what makes the record claimable - so the
    # staleness check belongs here too, not only at execution.
    record = await _supersede_if_finding_changed(record, owner)
    if coerce(record.get("state")) is RemediationState.SUPERSEDED:
        raise _conflict(
            record.get("error_summary")
            or "This remediation no longer matches the stored finding."
        )

    # The confidence gate, checked before the bytes are read: there is no
    # point making a user upload a file for a finding whose execution will be
    # refused. `remediation_executor.prepare` checks it again server-side, so
    # this is a courtesy rather than the enforcement point.
    gate = (record.get("proposal") or {}).get("confidence_gate")
    if gate != remediation.GATE_PASS:
        raise _conflict(
            "This finding did not meet the configured confidence threshold, "
            "so building a correction for it is refused. Review it manually."
        )

    limit = config.load_max_asset_bytes()
    data = await _read_bounded(file, limit)

    try:
        stored = await run_in_threadpool(
            remediation_assets.store_asset,
            remediation_id=remediation_id,
            data=data,
            source=remediation_assets.SOURCE_UPLOAD,
        )
    except AssetRejected as exc:
        # The file is the problem and the message says how. 422: the request
        # was well-formed, its content was not acceptable.
        raise HTTPException(
            status_code=422, detail=str(exc)
        )
    except AssetStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )

    try:
        updated = await remediation_repo.transition(
            remediation_id,
            owner,
            RemediationState.ASSET_READY,
            expected=RemediationState.APPROVED,
            fields={
                **stored,
                # From the authenticated principal, never a request field.
                "asset_supplied_by": owner,
                "asset_supplied_at": datetime.now(timezone.utc),
            },
        )
    except (InvalidRemediationTransition, RemediationConflict):
        # Another request moved the record between our check and this write.
        # Compensating cleanup: an object no record names is an orphan.
        await run_in_threadpool(
            remediation_assets.delete_asset, stored.get("asset_key")
        )
        raise _conflict(
            "This remediation was changed by another request; reload it and "
            "try again."
        )

    return remediation_repo.view(updated)


async def _read_bounded(file: UploadFile, limit: int) -> bytes:
    """Read an upload into memory, enforcing the limit as it goes.

    Streamed in chunks and checked on the way, so an oversized body is
    refused after `limit` bytes rather than after the whole thing has been
    buffered. A replacement image is small by nature, so holding it in memory
    is reasonable where a video is not.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"The replacement image exceeds the maximum size of "
                    f"{limit} bytes."
                ),
            )
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(
            status_code=422,
            detail="The uploaded file is empty.",
        )
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _dispatch_execution(
    background_tasks: BackgroundTasks, remediation_id: str, owner: str
) -> str:
    """Hand a durably-requested execution to an executor, per the mode.

    The request is ALREADY persisted (`execution_requested`) before this is
    called, so this function cannot lose work: in `worker` mode it
    deliberately does nothing and `python backend/worker.py` claims the
    record on its next poll. That is what lets the API restart without
    stranding an accepted request.

    `inline` mode runs it in a FastAPI background thread, which is convenient
    for single-process local development and for the test suite but is not
    durable - exactly the arrangement `app._dispatch_analysis` already has for
    video jobs, and for the same reasons.
    """
    mode = config.get_settings().job_execution_mode
    if mode == config.INLINE_MODE:
        background_tasks.add_task(_run_execution_sync, remediation_id, owner)
    return mode


def _run_execution_sync(remediation_id: str, owner: str) -> None:
    """Run one execution to completion on a worker thread."""
    import anyio

    try:
        anyio.run(remediation_executor.execute, remediation_id, owner)
    except Exception:
        # The executor records its own terminal failure; anything reaching
        # here must not take the thread's caller down with it.
        logger.exception(
            "Inline remediation execution failed for %s", remediation_id
        )


@router.post("/{remediation_id}/execute")
async def execute_remediation(
    remediation_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Build the corrected video for an approved remediation with an asset.

    Returns once the request is durably recorded - not once the video exists.
    The work is a claimed, leased job run by the worker; poll
    GET /{id}/status for progress and the eventual verdict.

    Preconditions, all enforced server-side:

    1. the caller owns the record (404 otherwise);
    2. reconstruction is enabled on this deployment (503 otherwise);
    3. the record is `asset_ready` with an asset attached (409 otherwise) -
       an `approved` record with no asset is told to supply one, and is also
       told that automated generation is unavailable;
    4. the stored finding still matches (superseded + 409 otherwise);
    5. attempts remain (409 otherwise).

    Idempotent: a repeat while the work is requested, running, or already
    finished returns the current record rather than starting a second run.
    """
    _require_enabled()
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)
    current = coerce(record.get("state"))

    # Already past this point: return where it actually is. A client that
    # retries a dropped response must not start a second reconstruction.
    if current in {
        RemediationState.RECONSTRUCTING,
        RemediationState.APPLIED,
        RemediationState.VERIFYING,
        RemediationState.VERIFIED,
        RemediationState.VERIFICATION_FAILED,
    }:
        return remediation_repo.view(record)

    if current is RemediationState.APPROVED:
        # The gate is checked before the provider branch so a low-confidence
        # finding gets the specific answer rather than the generic one.
        gate = (record.get("proposal") or {}).get("confidence_gate")
        if gate != remediation.GATE_PASS:
            raise _conflict(
                "This finding did not meet the configured confidence "
                "threshold, so building a correction for it is refused. "
                "Review it manually."
            )

        provider = image_provider.get_provider()
        if not provider.is_available():
            # Phase 8's behaviour, kept: no state change, because nothing was
            # attempted and nothing failed. The difference in Phase 9 is that
            # there is now a supported alternative, and the message names it.
            logger.info(
                "Execution refused: no asset and no image provider (provider=%s)",
                provider.name,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "No replacement image is attached, and automated image "
                    "generation is not configured on this deployment. Upload "
                    "a replacement image for this remediation first."
                ),
            )
        # Unreachable while NullImageProvider is the only implementation.
        logger.error(
            "Image provider %s reports itself available but no generation "
            "path is implemented",
            provider.name,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Automated image generation is not available.",
        )

    try:
        await remediation_executor.prepare(remediation_id, owner)
    except NotExecutable as exc:
        raise _conflict(str(exc))
    except ExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    except RemediationNotFound:
        raise HTTPException(status_code=404, detail="Remediation not found")

    # Durable acceptance: persisted BEFORE the response, so a restart cannot
    # lose the request.
    updated = await remediation_repo.request_execution(remediation_id, owner)
    if updated is None:
        raise _conflict(
            "This remediation was changed by another request; reload it and "
            "try again."
        )

    mode = _dispatch_execution(background_tasks, remediation_id, owner)
    logger.info(
        "Remediation %s accepted for reconstruction (execution mode %s)",
        remediation_id,
        mode,
    )
    return remediation_repo.view(updated)


# ---------------------------------------------------------------------------
# Status and verification
# ---------------------------------------------------------------------------

@router.get("/{remediation_id}/status")
async def get_execution_status(
    remediation_id: str, user: dict = Depends(get_current_user)
):
    """Current state, plus the output's analysis progress and any verdict.

    Calling this also ADVANCES verification when the output's analysis has
    finished: `verification.advance` reads the persisted per-frame results and
    records the verdict through the normal conditional transition. That is a
    trusted server-side evaluation, not a client assertion - the request body
    is empty and the caller cannot influence the outcome. The worker's sweep
    does the same thing on a timer, so a verdict is not dependent on anyone
    asking.
    """
    _require_enabled()
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)

    if coerce(record.get("state")) is RemediationState.VERIFYING:
        record = await verification.advance(remediation_id, owner)

    view = remediation_repo.view(record)

    # The output's own job, so the UI can show real analysis progress rather
    # than a fabricated percentage.
    output_video_id = record.get("analysis_job_video_id") or record.get(
        "output_video_id"
    )
    analysis = None
    if output_video_id:
        try:
            import video_repo

            job = await video_repo.get_video(str(output_video_id))
            if job.get("owner") == owner:
                analysis = video_repo.job_view(job)
        except Exception:
            # Progress is a nicety; its absence must not fail the status read.
            logger.debug("Could not read the output video's job record")

    return {**view, "output_analysis": analysis}


@router.get("/{remediation_id}/output")
async def get_output_playback(
    remediation_id: str, user: dict = Depends(get_current_user)
):
    """A short-lived, scoped playback URL for the corrected video.

    The storage key is never published. This mints the SAME kind of media
    token the upload route does - bound to one video id, short-lived - and
    returns a URL against the existing authorized playback route, which
    re-checks ownership and the token's video binding on every request.

    404 until an output actually exists. There is deliberately no placeholder
    URL: a download action must never appear before there is something real
    behind it.
    """
    _require_enabled()
    owner = user["sub"]
    record = await _load_owned(remediation_id, owner)

    output_video_id = record.get("output_video_id")
    if not output_video_id or not record.get("output_key"):
        raise HTTPException(
            status_code=404,
            detail="No corrected video exists for this remediation yet.",
        )

    # The output is an ordinary video record owned by this user; confirm that
    # rather than trusting the remediation's copy of the id.
    try:
        await require_video_owner(str(output_video_id), user)
    except HTTPException:
        logger.error(
            "Remediation %s names an output video this caller does not own",
            remediation_id,
        )
        raise HTTPException(
            status_code=404,
            detail="No corrected video exists for this remediation yet.",
        )

    import os as _os

    base_url = _os.getenv("API_URL", "http://localhost:8000")
    token = create_media_token(owner, str(output_video_id))
    stored_filename = (
        f"{output_video_id}.{remediation_executor.OUTPUT_EXTENSION}"
    )

    return {
        "remediation_id": remediation_id,
        "output_video_id": output_video_id,
        # Carries a short-lived token in its query string. Never logged: see
        # `logging_setup.install_log_redaction`.
        "playback_url": (
            f"{base_url}/uploads/{stored_filename}?access_token={token}"
        ),
        "duration_seconds": record.get("output_duration_seconds"),
        "has_audio": record.get("output_has_audio"),
        "size_bytes": record.get("output_bytes"),
    }


__all__ = ("router",)
