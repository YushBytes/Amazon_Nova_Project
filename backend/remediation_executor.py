"""Execute one approved remediation: build an output video, then analyse it.

Phase 9. The service layer the routes and the worker both call, rather than a
large route handler. Nothing here decides a verdict - `verification.py` does
that, from the output's persisted analysis.

WHAT ONE EXECUTION DOES
-----------------------
    1. claim the remediation         ASSET_READY -> RECONSTRUCTING, under a
                                     lease, with a fencing claim id
    2. re-read the source video      ownership record + its S3 reference
    3. re-check the finding          re-fingerprint; supersede if it changed
    4. download source and asset     into a unique temporary workspace
    5. reconstruct                   one FFmpeg overlay pass
    6. validate the output           ffprobe: real stream, audio, duration
    7. upload the output             a NEW object under remediated/
    8. register the output           a first-class video record, its own id
    9. submit it for analysis        the existing pipeline's own queue
   10. RECONSTRUCTING -> APPLIED -> VERIFYING

Steps 4-7 run outside any database transaction and hold no lock: the lease is
what protects the work, and it is heartbeated at each stage boundary.

ORDERING, AND WHAT A CRASH LEAVES BEHIND
----------------------------------------
The output object is uploaded BEFORE its video record is written, and the
record is written BEFORE the remediation is moved to APPLIED. So:

* a crash before the upload leaves nothing;
* a crash between the upload and the record write leaves an S3 object that no
  record names. The upload is compensated immediately on that failure path,
  and the recovery sweep returns the remediation to ASSET_READY for a fresh
  attempt with a NEW output id - so a retry never writes over the previous
  attempt's object;
* a crash after APPLIED but before the analysis submission leaves a record in
  APPLIED with an output video in UPLOADED. `resume_applied` finishes that,
  which is why APPLIED is a distinct state rather than a moment inside a
  larger write.

Execution is at-least-once, NOT exactly-once. An interrupted attempt is
retried from the beginning and produces a new output video; the previous
attempt's object is retained rather than deleted, because it is real and an
operator may need it. This is stated here and in docs/REMEDIATION.md rather
than claimed otherwise.

THE SOURCE IS NEVER TOUCHED
---------------------------
The source object is read through `source_store.materialise`, which downloads
to a temporary file. The output is written to a different temporary file and
uploaded under `remediated/{new video id}/`, a prefix nothing else writes.
`reconstruction.reconstruct` additionally refuses if asked to write over its
own input. Three independent reasons the original cannot change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

import aws_resources
import config
import reconstruction
import remediation_assets
import remediation_repo
import source_store
import video_repo
from remediation_repo import ClaimLost, RemediationConflict, RemediationNotFound
from remediation_state import (
    InvalidRemediationTransition,
    RemediationState,
    coerce,
)

logger = logging.getLogger(__name__)

# The output is always written as MP4: the overlay pass re-encodes the video
# anyway, and one container means the playback route's media type is always
# right.
OUTPUT_EXTENSION = "mp4"

# Backoff before a retryable failure becomes claimable again, doubling per
# attempt. Without it the worker reclaims the record on its next poll and
# spends the whole attempt budget within seconds of a transient blip.
RETRY_BACKOFF_SECONDS = 30
RETRY_BACKOFF_CAP_SECONDS = 600


class ExecutionError(RuntimeError):
    """Execution could not proceed. Carries a safe, user-facing message."""

    retryable = False

    def __init__(self, *args, retryable: bool = False):
        super().__init__(*args)
        self.retryable = retryable


class NotExecutable(ExecutionError):
    """The remediation is not in a state from which execution may begin."""

    def __init__(self, *args):
        super().__init__(*args, retryable=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retry_delay_seconds(attempt: int) -> int:
    """Exponential backoff, capped. Same shape as `pipeline.retry_delay_seconds`."""
    exponent = max(0, int(attempt) - 1)
    delay = RETRY_BACKOFF_SECONDS * (2**exponent)
    return min(delay, RETRY_BACKOFF_CAP_SECONDS)


def remediation_gate_pass() -> str:
    """The gate value that permits execution.

    A function rather than a module-level import so `remediation` is not
    imported at module scope: `remediation_executor` is imported by the route
    module, and keeping the heavier domain import lazy mirrors how the rest of
    this module defers its imports.
    """
    import remediation

    return remediation.GATE_PASS


def new_output_video_id() -> str:
    """A fresh video id for an output.

    Deliberately the same shape as an uploaded video's id
    (`ownership.VIDEO_ID_RE`), because the output IS a video record: the
    playback route, the ownership check and the analysis pipeline all accept
    it with no special case.
    """
    return f"vid_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

async def _assert_finding_unchanged(record: dict, owner: str) -> dict:
    """Re-fingerprint the finding; supersede the record if it moved.

    Uses the Phase 8 mechanism rather than a second one: a proposal addresses
    a finding positionally, and a re-analysis of the SOURCE overwrites that
    frame's row, so between approval and execution the finding may have
    changed or vanished. Building a video on the strength of a decision the
    owner never made is exactly what this prevents.

    Returns the record when it still matches. Raises when it does not.
    """
    import aws_clients
    from fastapi.concurrency import run_in_threadpool
    from remediation import (
        FindingNotFound,
        FindingNotRemediable,
        finding_from_frame_item,
    )

    video_id = record["video_id"]
    frame_number = int(record["frame_number"])
    detection_index = int(record["detection_index"])

    def _read():
        table = aws_clients.dynamodb_table(aws_resources.frames_table_name())
        return table.get_item(
            Key={"video_id": video_id, "frame_number": frame_number}
        ).get("Item")

    try:
        item = await run_in_threadpool(_read)
    except (BotoCoreError, ClientError) as exc:
        # A storage outage is NOT a changed finding. Retryable, and the record
        # stays where it is.
        raise ExecutionError(
            "The analysis store could not be read; the correction was not "
            "started.",
            retryable=True,
        ) from exc

    current = None
    if item is not None:
        try:
            current = finding_from_frame_item(item, detection_index, video_id).fingerprint
        except (FindingNotFound, FindingNotRemediable):
            current = None

    if current == record.get("finding_fingerprint"):
        return record

    reason = (
        "The video was re-analysed and this finding is no longer the same, so "
        "the approved correction no longer describes it."
    )
    await remediation_repo.mark_superseded(
        record["remediation_id"], owner, reason
    )
    raise NotExecutable(reason)


async def prepare(remediation_id: str, owner: str) -> dict:
    """Check a remediation is executable and return its record. No writes.

    Called by the route before it accepts a request, so a caller gets a
    precise 409 rather than a job that fails a second later. The worker
    re-checks everything under the claim, because this answer can go stale.

    Raises:
        RemediationNotFound: no such record for this owner.
        NotExecutable: it is not in an executable state, or its finding moved.
        ExecutionError: reconstruction is not available on this deployment.
    """
    if not config.load_reconstruction_enabled():
        raise ExecutionError(
            "Building corrected videos is not enabled on this deployment."
        )

    record = await remediation_repo.get(remediation_id, owner)
    state = coerce(record.get("state"))

    if state is not RemediationState.ASSET_READY:
        raise NotExecutable(
            f"A correction can only be built from an approved remediation "
            f"with a replacement image ready; this one is {state.value}."
        )
    if not record.get("asset_key"):
        raise NotExecutable(
            "No replacement image is attached to this remediation."
        )
    if int(record.get("attempt") or 0) >= remediation_repo.MAX_EXECUTION_ATTEMPTS:
        raise NotExecutable(
            f"This correction has already been attempted "
            f"{remediation_repo.MAX_EXECUTION_ATTEMPTS} time(s), which is the "
            f"maximum."
        )

    # The confidence gate, enforced here as well as at asset upload. Phase 8
    # introduced it to stop spending a provider's budget on a finding the
    # model itself was unsure about; in Phase 9 the spend it protects is
    # larger - an FFmpeg pass plus a full re-analysis of the output, billed
    # per frame. This is the server-side backstop: the upload route refuses
    # earlier for a better message, but a record that reached ASSET_READY by
    # any other route is still refused here.
    gate = (record.get("proposal") or {}).get("confidence_gate")
    if gate != remediation_gate_pass():
        raise NotExecutable(
            "This finding did not meet the configured confidence threshold, "
            "so building a correction for it is refused. Review it manually."
        )

    await _assert_finding_unchanged(record, owner)
    return record


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

async def execute(remediation_id: str, owner: str) -> dict:
    """Claim one remediation and run it through to VERIFYING.

    Returns the final record view for this attempt. Never raises for an
    ordinary failure: a failure is recorded ON the record - terminally for a
    deterministic one, with a backoff for a retryable one - so the owner can
    see what happened. Only a lost claim and a missing record propagate,
    because those mean this caller must simply stop.

    Raises:
        RemediationNotFound: the record vanished.
        RemediationConflict: it could not be claimed.
        InvalidRemediationTransition: it was not in a claimable state.
    """
    record, claim_id = await remediation_repo.claim_for_execution(
        remediation_id, owner
    )
    attempt = int(record.get("attempt") or 1)
    logger.info(
        "Executing remediation %s (attempt %d)", remediation_id, attempt
    )

    try:
        return await _run_claimed(record, owner, claim_id, attempt)
    except ClaimLost:
        # Fenced: a sweep reclaimed this and another attempt owns it now.
        # Writing anything further would attach our output to their attempt.
        logger.warning(
            "Remediation %s was reclaimed mid-execution; stopping",
            remediation_id,
        )
        raise
    except Exception:
        # _run_claimed records its own failures. Reaching here means something
        # it did not anticipate, which must still leave a terminal state
        # rather than a record stuck in RECONSTRUCTING until a sweep.
        logger.exception("Unhandled failure executing %s", remediation_id)
        await _fail(
            remediation_id,
            owner,
            claim_id,
            code="execution_error",
            summary="The correction could not be built.",
        )
        return await remediation_repo.get(remediation_id, owner)


async def _run_claimed(record: dict, owner: str, claim_id: str, attempt: int) -> dict:
    """The body of one claimed execution."""
    from fastapi.concurrency import run_in_threadpool

    remediation_id = record["remediation_id"]
    source_video_id = record["video_id"]

    # --- the finding must still be the one that was approved --------------
    try:
        await _assert_finding_unchanged(record, owner)
    except NotExecutable as exc:
        # Already superseded by the check itself; nothing more to record.
        logger.info("Execution refused for %s: %s", remediation_id, exc)
        return await remediation_repo.get(remediation_id, owner)
    except ExecutionError as exc:
        return await _retry_or_fail(
            remediation_id, owner, claim_id, attempt, exc,
            code="finding_unreadable",
        )

    # --- the source video must still exist and belong to this owner -------
    try:
        video = await video_repo.get_video(source_video_id)
    except Exception:
        return await _fail(
            remediation_id, owner, claim_id,
            code="source_missing",
            summary="The original video is no longer available.",
        )
    if video.get("owner") != owner:
        # Should be impossible - the proposal's owner came from the video's
        # ownership record. Refuse loudly rather than build one user's video
        # into another's output.
        logger.error(
            "Refusing to execute %s: the source video has a different owner",
            remediation_id,
        )
        return await _fail(
            remediation_id, owner, claim_id,
            code="ownership_mismatch",
            summary="The original video could not be verified.",
        )

    output_video_id = new_output_video_id()
    uploaded_key: str | None = None

    try:
        with reconstruction.Workspace() as workspace:
            # --- fetch the inputs -----------------------------------------
            asset_path = workspace.file("asset.img")
            await run_in_threadpool(
                remediation_assets.download_asset, record, asset_path
            )
            await remediation_repo.heartbeat(remediation_id, owner, claim_id)

            output_path = workspace.file(f"output.{OUTPUT_EXTENSION}")

            def _build() -> reconstruction.ReconstructionResult:
                # `materialise` downloads the source to its OWN temporary
                # directory and removes it afterwards; the original S3 object
                # is only ever read.
                with source_store.materialise(video) as source_path:
                    return reconstruction.reconstruct(
                        source_path=source_path,
                        asset_path=asset_path,
                        bounding_box=(record.get("finding") or {}).get(
                            "bounding_box"
                        ),
                        timestamp_seconds=(record.get("finding") or {}).get(
                            "timestamp_seconds"
                        ),
                        output_path=output_path,
                        window_seconds=config.load_remediation_window_seconds(),
                        timeout_seconds=config.load_reconstruction_timeout_seconds(),
                    )

            result = await run_in_threadpool(_build)
            await remediation_repo.heartbeat(remediation_id, owner, claim_id)

            # --- upload the output as a NEW object -------------------------
            bucket = aws_resources.media_bucket()
            key = aws_resources.remediated_video_key(
                output_video_id, OUTPUT_EXTENSION
            )

            def _upload() -> None:
                with open(result.output_path, "rb") as handle:
                    source_store.s3_client().put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=handle,
                        ContentType="video/mp4",
                        ServerSideEncryption="AES256",
                    )

            await run_in_threadpool(_upload)
            uploaded_key = key
            await remediation_repo.heartbeat(remediation_id, owner, claim_id)

    except reconstruction.UnsafeRegion as exc:
        # The finding does not describe an editable region. Deterministic:
        # retrying would reach the same conclusion three times.
        return await _fail(
            remediation_id, owner, claim_id,
            code="unsafe_region",
            summary=str(exc),
        )
    except reconstruction.ReconstructionError as exc:
        return await _retry_or_fail(
            remediation_id, owner, claim_id, attempt, exc,
            code="reconstruction_failed",
        )
    except remediation_assets.AssetError as exc:
        return await _retry_or_fail(
            remediation_id, owner, claim_id, attempt, exc,
            code="asset_unreadable",
        )
    except source_store.SourceStoreError as exc:
        return await _retry_or_fail(
            remediation_id, owner, claim_id, attempt, exc,
            code="source_unreadable",
        )
    except (BotoCoreError, ClientError):
        logger.exception("Could not store the corrected video")
        return await _retry_or_fail(
            remediation_id, owner, claim_id, attempt,
            ExecutionError(
                "The corrected video could not be stored.", retryable=True
            ),
            code="output_store_failed",
        )

    # --- register the output as a first-class video -----------------------
    #
    # From here the output is an ordinary video owned by this user: the
    # playback route serves it, the ownership check protects it, and the
    # analysis pipeline accepts it with no special case. That is the whole
    # reason it gets its own id rather than being a field on the source.
    try:
        await video_repo.record_video(
            video_id=output_video_id,
            owner=owner,
            stored_filename=f"{output_video_id}.{OUTPUT_EXTENSION}",
            original_filename=_output_display_name(video),
            extension=OUTPUT_EXTENSION,
            size_bytes=result.size_bytes,
            s3_bucket=bucket,
            s3_key=uploaded_key,
        )
    except Exception:
        # Compensating cleanup: an object with no record is unattributable and
        # unreachable, so leaving it would be an orphan nobody can find.
        logger.exception("Could not register the corrected video; rolling back")
        await run_in_threadpool(_delete_object, bucket, uploaded_key)
        return await _retry_or_fail(
            remediation_id, owner, claim_id, attempt,
            ExecutionError(
                "The corrected video could not be registered.", retryable=True
            ),
            code="output_register_failed",
        )

    # Provenance ON the output record, so an operator looking at a video can
    # see it was derived rather than uploaded.
    try:
        await video_repo.set_fields(
            output_video_id,
            {
                "derived_from_video_id": source_video_id,
                "derived_from_remediation_id": remediation_id,
                "is_remediated_output": True,
            },
        )
    except Exception:
        # Provenance is an audit nicety; losing it must not fail the run, and
        # the remediation record carries the same link authoritatively.
        logger.warning("Could not annotate the corrected video's provenance")

    # --- RECONSTRUCTING -> APPLIED ---------------------------------------
    #
    # "An output video exists." Emphatically not "the video is compliant":
    # nothing has analysed it yet.
    try:
        await remediation_repo.transition(
            remediation_id,
            owner,
            RemediationState.APPLIED,
            expected=RemediationState.RECONSTRUCTING,
            claim_id=claim_id,
            fields={
                "output_video_id": output_video_id,
                "output_key": uploaded_key,
                "output_bytes": result.size_bytes,
                "output_duration_seconds": round(result.duration_seconds, 3),
                "output_has_audio": result.has_audio,
                "reconstruction": result.as_audit_fields(),
            },
        )
    except ClaimLost:
        # Fenced between the upload and this write. The object and its video
        # record are retained - they are real - but this attempt must not
        # claim them.
        logger.warning(
            "Remediation %s was reclaimed before its output was recorded",
            remediation_id,
        )
        raise

    return await submit_for_verification(remediation_id, owner)


def _delete_object(bucket: str, key: str | None) -> None:
    """Best-effort removal of an orphaned output object."""
    if not key:
        return
    try:
        source_store.s3_client().delete_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError):
        logger.warning("Could not remove an orphaned corrected video")


def _output_display_name(video: dict) -> str:
    """A display name for the output, derived from the original's.

    Display metadata only, never a path or a key - the key is built from the
    server-minted output id.
    """
    original = video.get("original_filename") or video.get("video_id") or "video"
    stem = Path(str(original)).stem[:180]
    return f"{stem} (corrected).{OUTPUT_EXTENSION}"


# ---------------------------------------------------------------------------
# Submitting the output for its own analysis
# ---------------------------------------------------------------------------

async def submit_for_verification(remediation_id: str, owner: str) -> dict:
    """Queue the output video for analysis and move APPLIED -> VERIFYING.

    Separate and idempotent, so a crash between APPLIED and the submission is
    recoverable: `resume_applied` calls exactly this.

    The submission goes through `pipeline.submit`, which is the SAME durable
    queue the uploaded videos use - a conditional state write on the output's
    own video record, consumed by the same worker. No second queue, and no
    second analysis engine.
    """
    import pipeline

    record = await remediation_repo.get(remediation_id, owner)
    state = coerce(record.get("state"))
    if state is RemediationState.VERIFYING:
        # Already submitted; nothing to do.
        return record
    if state is not RemediationState.APPLIED:
        return record

    output_video_id = record.get("output_video_id")
    if not output_video_id:
        return await _fail(
            remediation_id, owner, None,
            code="output_missing",
            summary="The corrected video could not be found for analysis.",
        )

    try:
        await pipeline.submit(str(output_video_id), allow_retry=False)
    except pipeline.AlreadyRunning:
        # The output's analysis is already queued or running - exactly what we
        # want. Fall through and record VERIFYING.
        logger.info(
            "The corrected video %s is already queued for analysis",
            output_video_id,
        )
    except Exception:
        # The output is real and retained; it simply has no verdict. Terminal
        # FAILED rather than VERIFICATION_FAILED: the latter asserts that a
        # re-analysis ran and disagreed, which did not happen.
        logger.exception("Could not queue the corrected video for analysis")
        return await _fail(
            remediation_id, owner, None,
            code="verification_not_queued",
            summary=(
                "The corrected video was built but could not be queued for "
                "analysis, so it has not been verified."
            ),
        )

    try:
        return await remediation_repo.transition(
            remediation_id,
            owner,
            RemediationState.VERIFYING,
            expected=RemediationState.APPLIED,
            fields={
                "analysis_job_video_id": str(output_video_id),
                "analysis_submitted_at": _now(),
            },
        )
    except (InvalidRemediationTransition, RemediationConflict):
        # Another caller advanced it. Return what is stored.
        return await remediation_repo.get(remediation_id, owner)


async def resume_applied(limit: int = 25) -> int:
    """Finish remediations stranded in APPLIED with no analysis queued.

    The window a crash can land in: the output exists and its video record is
    written, but `pipeline.submit` had not run. Called from the worker's sweep
    so it heals without an operator.

    Returns how many were advanced.
    """
    col = await remediation_repo._collection()  # noqa: SLF001 - same package
    cursor = (
        col.find({"state": RemediationState.APPLIED.value})
        .sort("updated_at", 1)
        .limit(limit)
    )
    advanced = 0
    async for record in cursor:
        remediation_id = record.get("remediation_id")
        owner = record.get("owner")
        if not remediation_id or not owner:
            continue
        try:
            await submit_for_verification(remediation_id, owner)
            advanced += 1
        except Exception:
            logger.exception(
                "Could not resume remediation %s from applied", remediation_id
            )
    return advanced


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

async def _retry_or_fail(
    remediation_id: str,
    owner: str,
    claim_id: str | None,
    attempt: int,
    error: Exception,
    *,
    code: str,
) -> dict:
    """Schedule a retry for a retryable failure, else fail terminally.

    Retryability is read from the exception's own flag, set at its raise site -
    never inferred from the message. A deterministic failure retried three
    times costs three FFmpeg passes for one answer.
    """
    summary = str(error) or "The correction could not be built."
    retryable = bool(getattr(error, "retryable", False))

    if retryable and attempt < remediation_repo.MAX_EXECUTION_ATTEMPTS and claim_id:
        delay = retry_delay_seconds(attempt)
        updated = await remediation_repo.schedule_execution_retry(
            remediation_id,
            owner,
            claim_id,
            delay_seconds=delay,
            error_code=code,
            error_summary=summary,
        )
        if updated is not None:
            logger.info(
                "Remediation %s will retry in %ds (attempt %d)",
                remediation_id,
                delay,
                attempt,
            )
            return updated

    return await _fail(
        remediation_id, owner, claim_id, code=code, summary=summary
    )


async def _fail(
    remediation_id: str,
    owner: str,
    claim_id: str | None,
    *,
    code: str,
    summary: str,
) -> dict:
    """Record a terminal failure.

    `summary` is already safe: every caller passes a fixed string or an
    exception whose message was written for the video's owner. No path,
    bucket, key or FFmpeg stderr reaches here.
    """
    try:
        return await remediation_repo.transition(
            remediation_id,
            owner,
            RemediationState.FAILED,
            claim_id=claim_id,
            fields={"error_code": code, "error_summary": summary},
        )
    except ClaimLost:
        raise
    except (InvalidRemediationTransition, RemediationConflict, RemediationNotFound):
        logger.warning(
            "Could not record a failure for %s; its state changed first",
            remediation_id,
        )
        return await remediation_repo.get(remediation_id, owner)


__all__ = (
    "OUTPUT_EXTENSION",
    "ExecutionError",
    "NotExecutable",
    "execute",
    "new_output_video_id",
    "prepare",
    "resume_applied",
    "retry_delay_seconds",
    "submit_for_verification",
)
