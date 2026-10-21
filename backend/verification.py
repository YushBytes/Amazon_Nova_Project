"""Decide, from persisted analysis of the OUTPUT video, what was achieved.

Phase 9. The only module permitted to move a remediation to `verified` or
`verification_failed`.

WHAT IS AND IS NOT EVIDENCE
---------------------------
Not evidence:
  * FFmpeg exiting 0;
  * the owner having approved the proposal;
  * an image provider returning bytes;
  * the output video existing and playing;
  * the ORIGINAL video's analysis results.

Evidence: a COMPLETED analysis job for the output video, whose per-frame
results were persisted by the normal pipeline, read back from the frame store
and keyed by the output video's own id. Anything less - still running, failed,
partially audited, unreadable - produces no verdict.

THREE OUTCOMES, NEVER CONFLATED
-------------------------------
The prompt for this phase is explicit, and so is this module:

  1. `target_resolved`      the targeted finding is no longer detected at its
                            place in the output.
  2. `policy_passed`        the output satisfies the configured compliance
                            policy. With the default policy this is the same
                            question as (1); under `clean_output` it also
                            requires the output to carry no findings at all.
  3. `other_findings`       what else the output's analysis reported. Recorded
                            and exposed whatever the answer to (1) and (2),
                            because "the logo is gone" and "the video is fine"
                            are different statements.

A remediation reaches `verified` only when `policy_passed` is true. It reaches
`verification_failed` when the analysis completed and the policy did not pass -
including the case where the target is gone but the policy is `clean_output`
and other findings remain.

MATCHING THE TARGET
-------------------
The targeted finding is identified by its CATEGORY at a TIME, not by index:
re-analysis of a different file produces its own frames in its own order, so
an index means nothing across videos. A detection in the output counts as the
same finding when its category matches (case-insensitively, trimmed) and its
timestamp is within `TIMESTAMP_TOLERANCE_SECONDS` of the original's.

The tolerance exists because frames are sampled at 1 fps and a re-extraction
need not land on exactly the same instants. It is deliberately a little wider
than one sampling interval: being slow to declare success is the safe
direction of error.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

import config
import remediation_repo
from job_state import JobState, coerce as coerce_job_state
from remediation_state import RemediationState

logger = logging.getLogger(__name__)

# How far apart two detections may be in time and still be the same finding.
# One sampling interval is 1s at the configured 1 fps; 1.5s tolerates a
# re-extraction landing half a frame off without reaching the next frame's
# evidence.
TIMESTAMP_TOLERANCE_SECONDS = 1.5

# Verification policies.
#
#   target_only   Verified when the targeted finding is gone. Other findings
#                 are recorded and exposed but do not block. This is the
#                 default because a remediation addresses ONE finding, and a
#                 video with two problems should not report the first fix as a
#                 failure.
#   clean_output  Verified only when the output carries no findings at all.
POLICY_TARGET_ONLY = "target_only"
POLICY_CLEAN_OUTPUT = "clean_output"
POLICIES = frozenset({POLICY_TARGET_ONLY, POLICY_CLEAN_OUTPUT})

# Outcomes of an evaluation.
OUTCOME_VERIFIED = "verified"
OUTCOME_FAILED = "verification_failed"
OUTCOME_PENDING = "pending"
OUTCOME_INCONCLUSIVE = "inconclusive"


class VerificationError(RuntimeError):
    """Verification could not be carried out. Never a verdict."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_policy() -> str:
    """The configured verification policy.

    Read through `config` for validation, so an unrecognised value fails at
    startup rather than silently defaulting to the permissive option.
    """
    return config.load_verification_policy()


# ---------------------------------------------------------------------------
# Reading the output's analysis
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> float | None:
    from decimal import Decimal

    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (ValueError, ArithmeticError):
            return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    return None


def _as_int(value: Any, default: int = 0) -> int:
    from decimal import Decimal

    if isinstance(value, bool):
        return default
    if isinstance(value, (Decimal, int, float)):
        try:
            return int(value)
        except (ValueError, ArithmeticError):
            return default
    return default


def read_output_findings(output_video_id: str) -> dict:
    """Read every persisted detection for the output video.

    Uses the same key-based Query the violations route uses, so this reads
    exactly what the pipeline wrote - no second analysis path, no separate
    store.

    Returns a mapping with the detections and the per-frame audit counts, so a
    caller can tell "no violations" from "no frames were audited". The second
    is not evidence of compliance and this is where that distinction is kept.

    Raises:
        VerificationError: the analysis store could not be read. Explicitly
            NOT a verdict - a storage outage must never read as a clean video.
    """
    import aws_clients
    import aws_resources
    from boto3.dynamodb.conditions import Key

    table = aws_clients.dynamodb_table(aws_resources.frames_table_name())

    detections: list[dict] = []
    frames_seen = 0
    audited = 0
    failed = 0
    cursor = None
    pages = 0

    try:
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("video_id").eq(output_video_id),
                "Limit": 200,
            }
            if cursor is not None:
                kwargs["ExclusiveStartKey"] = {
                    "video_id": output_video_id,
                    "frame_number": cursor,
                }
            response = table.query(**kwargs)
            items = response.get("Items") or []

            for item in items:
                frames_seen += 1
                status = item.get("status")
                if status == "audited":
                    audited += 1
                elif status == "audit_failed":
                    failed += 1
                timestamp = _as_float(item.get("timestamp"))
                for raw in item.get("detections") or []:
                    if not isinstance(raw, dict):
                        continue
                    detections.append(
                        {
                            "category": str(raw.get("category") or "").strip(),
                            "description": str(raw.get("description") or ""),
                            "risk_level": (
                                str(raw["risk_level"])
                                if raw.get("risk_level") is not None
                                else None
                            ),
                            # Absent stays absent. A fabricated 0.0 would
                            # assert certainty the model never expressed.
                            "confidence": _as_float(raw.get("confidence")),
                            "timestamp": timestamp,
                            "frame_number": _as_int(item.get("frame_number")),
                        }
                    )

            last = response.get("LastEvaluatedKey") or None
            pages += 1
            if not last or pages >= 25:
                break
            cursor = _as_int(last.get("frame_number"))
    except (BotoCoreError, ClientError) as exc:
        logger.exception("Could not read the output video's analysis results")
        raise VerificationError(
            "The analysis results for the corrected video could not be read."
        ) from exc

    return {
        "detections": detections,
        "frames_seen": frames_seen,
        "frames_audited": audited,
        "frames_failed": failed,
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _same_category(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


def find_target_matches(
    findings: list[dict],
    target_category: str | None,
    target_timestamp: float | None,
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> list[dict]:
    """Detections in the output that are the same finding as the target.

    Same category, and within `tolerance` of the original timestamp. When the
    original carried no timestamp, time is not used as a discriminator and a
    category match anywhere in the output counts - deliberately conservative:
    without a time we cannot show the finding moved, so we must not claim it
    was removed from one place.
    """
    matches: list[dict] = []
    for finding in findings:
        if not _same_category(finding.get("category"), target_category):
            continue
        if target_timestamp is None:
            matches.append(finding)
            continue
        found_at = finding.get("timestamp")
        if found_at is None:
            # A matching category with no timestamp cannot be excluded by time.
            matches.append(finding)
            continue
        if abs(float(found_at) - float(target_timestamp)) <= tolerance:
            matches.append(finding)
    return matches


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(
    *,
    record: dict,
    job_state: str,
    frames_extracted: int | None,
    analysis: dict,
    policy: str | None = None,
) -> dict:
    """Decide the outcome from the output's real analysis. Pure.

    Separated from the persistence below so the whole decision is testable
    without a database, and so there is exactly one place where "verified" is
    computed.

    Returns an evidence mapping with an `outcome` of OUTCOME_VERIFIED,
    OUTCOME_FAILED, OUTCOME_PENDING or OUTCOME_INCONCLUSIVE. Only
    OUTCOME_VERIFIED permits the `verified` state.
    """
    policy = policy or load_policy()
    state = coerce_job_state(job_state)

    target = record.get("finding") or {}
    target_category = target.get("category")
    target_timestamp = target.get("timestamp_seconds")

    evidence: dict[str, Any] = {
        "policy": policy,
        "evaluated_at": _now().isoformat(),
        "source_video_id": record.get("video_id"),
        "output_video_id": record.get("output_video_id"),
        "analysis_job_video_id": record.get("analysis_job_video_id"),
        "analysis_state": state.value,
        "frames_extracted": frames_extracted,
        "frames_audited": analysis.get("frames_audited"),
        "frames_failed": analysis.get("frames_failed"),
        "target_category": target_category,
        "target_timestamp_seconds": target_timestamp,
        "timestamp_tolerance_seconds": TIMESTAMP_TOLERANCE_SECONDS,
    }

    # --- the analysis has not finished -----------------------------------
    if not state.is_terminal:
        evidence.update(
            {
                "outcome": OUTCOME_PENDING,
                "target_resolved": None,
                "policy_passed": None,
                "other_findings": [],
                "reason": "The corrected video is still being analysed.",
            }
        )
        return evidence

    # --- the analysis failed ---------------------------------------------
    if state is not JobState.COMPLETED:
        evidence.update(
            {
                "outcome": OUTCOME_INCONCLUSIVE,
                "target_resolved": None,
                "policy_passed": None,
                "other_findings": [],
                "reason": (
                    "The corrected video's analysis did not complete, so "
                    "nothing can be concluded about it."
                ),
            }
        )
        return evidence

    # --- completed, but nothing was actually audited ----------------------
    #
    # A completed job with zero audited frames would otherwise produce an
    # empty detections list that reads exactly like a clean video. It is not
    # evidence of anything.
    if not analysis.get("frames_audited"):
        evidence.update(
            {
                "outcome": OUTCOME_INCONCLUSIVE,
                "target_resolved": None,
                "policy_passed": None,
                "other_findings": [],
                "reason": (
                    "The corrected video's analysis recorded no audited "
                    "frames, so its results cannot support a conclusion."
                ),
            }
        )
        return evidence

    # --- a completed analysis with real audited frames --------------------
    findings = analysis.get("detections") or []
    matches = find_target_matches(findings, target_category, target_timestamp)
    target_resolved = len(matches) == 0

    # Everything the output reported that is NOT the target. Exposed whatever
    # the verdict: a resolved target does not make these disappear.
    other = [f for f in findings if f not in matches]

    if policy == POLICY_CLEAN_OUTPUT:
        policy_passed = target_resolved and not other
    else:
        policy_passed = target_resolved

    if target_resolved and not policy_passed:
        reason = (
            f"The targeted {target_category or 'finding'} is no longer "
            f"detected, but the corrected video still has "
            f"{len(other)} other finding(s) and the configured policy "
            f"requires a clean output."
        )
    elif target_resolved:
        reason = (
            f"The targeted {target_category or 'finding'} is no longer "
            f"detected at its place in the corrected video."
        )
    else:
        reason = (
            f"The targeted {target_category or 'finding'} is still detected "
            f"in the corrected video."
        )

    evidence.update(
        {
            "outcome": OUTCOME_VERIFIED if policy_passed else OUTCOME_FAILED,
            "target_resolved": target_resolved,
            "policy_passed": policy_passed,
            # The actual detections that still match the target, so a reader
            # can see WHY it was not resolved.
            "target_matches": matches,
            "other_findings": other,
            "other_finding_count": len(other),
            "reason": reason,
        }
    )
    return evidence


# ---------------------------------------------------------------------------
# Persisting the verdict
# ---------------------------------------------------------------------------

async def advance(remediation_id: str, owner: str) -> dict:
    """Evaluate one VERIFYING remediation and persist the verdict if there is one.

    Idempotent and safe to call repeatedly - from the worker's sweep, or from
    the status route when a client asks. Every write is the conditional
    transition from `remediation_repo`, so two callers racing produce one
    winner and the loser's conflict is swallowed here (the record is already
    where it should be).

    Returns the STORED record (not the client-facing projection - callers
    that serve it pass it through `remediation_repo.view`). A record that is
    not VERIFYING is returned untouched: this function never reaches back into
    an earlier stage, and never re-opens a verdict already recorded.
    """
    import video_repo
    from fastapi.concurrency import run_in_threadpool

    record = await remediation_repo.get(remediation_id, owner)
    if (record.get("state") or "") != RemediationState.VERIFYING.value:
        return record

    output_video_id = record.get("analysis_job_video_id") or record.get(
        "output_video_id"
    )
    if not output_video_id:
        # VERIFYING with no job to look at should be impossible: the executor
        # writes the id in the same conditional write that sets this state.
        # Fail safely rather than guessing.
        logger.error(
            "Remediation %s is verifying with no analysis job recorded",
            remediation_id,
        )
        return await _fail(
            remediation_id,
            owner,
            code="verification_unlinked",
            summary=(
                "The corrected video's analysis could not be linked to this "
                "remediation, so no verdict was recorded."
            ),
        )

    # The output's job state comes from the SAME record the pipeline writes,
    # so this is the pipeline's own answer rather than a second opinion.
    try:
        job = await video_repo.get_video(str(output_video_id))
    except Exception:
        logger.exception("Could not read the output video's job record")
        return record

    # Guard against a crossed link: the job we are about to read results from
    # must be the output video this remediation created, and must belong to
    # the same owner. Without this a bug elsewhere could verify one user's
    # remediation from another's analysis.
    if job.get("owner") != owner:
        logger.error(
            "Refusing to verify %s from an analysis job with a different owner",
            remediation_id,
        )
        return await _fail(
            remediation_id,
            owner,
            code="verification_unlinked",
            summary=(
                "The corrected video's analysis could not be linked to this "
                "remediation, so no verdict was recorded."
            ),
        )

    state = coerce_job_state(job.get("status"))
    if not state.is_terminal:
        # Still running. Stay in VERIFYING and record nothing: a partial read
        # is not a verdict.
        return record

    try:
        analysis = await run_in_threadpool(
            read_output_findings, str(output_video_id)
        )
    except VerificationError as exc:
        # A storage failure is not a verdict either. Left in VERIFYING so a
        # later call can try again.
        logger.warning("Verification deferred: %s", type(exc).__name__)
        return record

    evidence = evaluate(
        record=record,
        job_state=str(job.get("status")),
        frames_extracted=job.get("frames_extracted"),
        analysis=analysis,
    )

    outcome = evidence["outcome"]
    if outcome == OUTCOME_PENDING:
        return record

    if outcome == OUTCOME_VERIFIED:
        target = RemediationState.VERIFIED
        fields = {
            "verification": evidence,
            "verification_video_id": str(output_video_id),
            "verified_at": _now(),
        }
    elif outcome == OUTCOME_FAILED:
        target = RemediationState.VERIFICATION_FAILED
        fields = {
            "verification": evidence,
            "verification_video_id": str(output_video_id),
            "error_code": "target_not_resolved"
            if not evidence.get("target_resolved")
            else "policy_not_met",
            "error_summary": evidence["reason"],
        }
    else:
        # Inconclusive: the analysis finished but cannot support a conclusion.
        # Terminal FAILED, not VERIFICATION_FAILED - the latter asserts that a
        # re-analysis ran and disagreed, which is a stronger claim than we can
        # make here.
        target = RemediationState.FAILED
        fields = {
            "verification": evidence,
            "verification_video_id": str(output_video_id),
            "error_code": "verification_inconclusive",
            "error_summary": evidence["reason"],
        }

    try:
        return await remediation_repo.transition(
            remediation_id,
            owner,
            target,
            expected=RemediationState.VERIFYING,
            fields=fields,
        )
    except Exception:
        # Another caller got there first, which is the conditional write doing
        # its job. Return whatever is stored now.
        logger.info(
            "Verification verdict for %s was already recorded", remediation_id
        )
        return await remediation_repo.get(remediation_id, owner)


async def _fail(remediation_id: str, owner: str, *, code: str, summary: str) -> dict:
    try:
        return await remediation_repo.transition(
            remediation_id,
            owner,
            RemediationState.FAILED,
            fields={"error_code": code, "error_summary": summary},
        )
    except Exception:
        return await remediation_repo.get(remediation_id, owner)


__all__ = (
    "OUTCOME_FAILED",
    "OUTCOME_INCONCLUSIVE",
    "OUTCOME_PENDING",
    "OUTCOME_VERIFIED",
    "POLICIES",
    "POLICY_CLEAN_OUTPUT",
    "POLICY_TARGET_ONLY",
    "TIMESTAMP_TOLERANCE_SECONDS",
    "VerificationError",
    "advance",
    "evaluate",
    "find_target_matches",
    "load_policy",
    "read_output_findings",
)
