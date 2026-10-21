"""Phase 9: what counts as evidence that a correction worked.

This is the most important test file in the phase. Everything upstream of it
builds a video; this is where the system decides whether to tell a user it is
compliant, and the wrong answer here is the difference between a product and a
liability.

The assertions are organised around the things that must NEVER be evidence:

  * FFmpeg exiting 0
  * the owner having approved the proposal
  * the output video existing and playing
  * the ORIGINAL video's analysis results
  * a completed job that audited no frames
  * a storage failure reading the results
  * an analysis still running

and the one thing that is: a COMPLETED analysis of the output video, with real
audited frames, whose persisted detections no longer carry the targeted
finding.

The three outcomes are kept apart throughout - "the target is gone", "the
policy passed", and "what else is in there" are different questions and a
single flag for all three is the defect this phase exists to avoid.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import anyio
import pytest

import remediation_repo
import verification
from remediation_state import RemediationState
from verification import (
    OUTCOME_FAILED,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_PENDING,
    OUTCOME_VERIFIED,
    POLICY_CLEAN_OUTPUT,
    POLICY_TARGET_ONLY,
    TIMESTAMP_TOLERANCE_SECONDS,
    evaluate,
    find_target_matches,
    read_output_findings,
)

VIDEO_ID = "vid_abcd1234"
OUTPUT_ID = "vid_0000beef"


def record(**overrides):
    """A remediation record as it looks in VERIFYING."""
    base = {
        "remediation_id": "rem_abc123abc123",
        "video_id": VIDEO_ID,
        "owner": "alice",
        "state": RemediationState.VERIFYING.value,
        "output_video_id": OUTPUT_ID,
        "analysis_job_video_id": OUTPUT_ID,
        "finding": {
            "frame_number": 3,
            "detection_index": 0,
            "category": "competitor_logo",
            "description": "A rival mark is visible.",
            "risk_level": "high",
            "confidence": 0.91,
            "bounding_box": [100.0, 200.0, 300.0, 400.0],
            "timestamp_seconds": 3.0,
        },
    }
    base.update(overrides)
    return base


def analysis(detections=None, audited=10, failed=0, seen=10):
    return {
        "detections": detections if detections is not None else [],
        "frames_seen": seen,
        "frames_audited": audited,
        "frames_failed": failed,
    }


def finding(category="competitor_logo", timestamp=3.0, confidence=0.9):
    return {
        "category": category,
        "description": "x",
        "risk_level": "high",
        "confidence": confidence,
        "timestamp": timestamp,
        "frame_number": int(timestamp) if timestamp is not None else 0,
    }


# ---------------------------------------------------------------------------
# the target is matched by category AND time, not by index
# ---------------------------------------------------------------------------

def test_the_target_matches_on_category_and_timestamp():
    matches = find_target_matches(
        [finding(timestamp=3.0)], "competitor_logo", 3.0
    )
    assert len(matches) == 1


def test_a_different_category_at_the_same_time_is_not_the_target():
    matches = find_target_matches(
        [finding(category="unsafe_item", timestamp=3.0)],
        "competitor_logo",
        3.0,
    )
    assert matches == []


def test_the_same_category_far_away_in_time_is_not_the_target():
    """A logo elsewhere in the clip is a different finding."""
    matches = find_target_matches(
        [finding(timestamp=30.0)], "competitor_logo", 3.0
    )
    assert matches == []


def test_the_tolerance_covers_a_re_extraction_landing_slightly_off():
    inside = TIMESTAMP_TOLERANCE_SECONDS - 0.1
    outside = TIMESTAMP_TOLERANCE_SECONDS + 0.1
    assert find_target_matches(
        [finding(timestamp=3.0 + inside)], "competitor_logo", 3.0
    )
    assert not find_target_matches(
        [finding(timestamp=3.0 + outside)], "competitor_logo", 3.0
    )


def test_category_matching_ignores_case_and_whitespace():
    matches = find_target_matches(
        [finding(category="  Competitor_Logo ")], "competitor_logo", 3.0
    )
    assert len(matches) == 1


def test_a_target_with_no_timestamp_matches_on_category_alone():
    """Conservative: without a time we cannot show the finding moved.

    So a category match anywhere counts, and we do not claim it was removed
    from one place.
    """
    matches = find_target_matches([finding(timestamp=50.0)], "competitor_logo", None)
    assert len(matches) == 1


def test_a_candidate_with_no_timestamp_is_not_excluded_by_time():
    matches = find_target_matches(
        [finding(timestamp=None)], "competitor_logo", 3.0
    )
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# a pending or failed analysis is never a verdict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["queued", "extracting", "analyzing"])
def test_a_running_analysis_is_pending_not_verified(state):
    evidence = evaluate(
        record=record(), job_state=state, frames_extracted=None, analysis=analysis()
    )
    assert evidence["outcome"] == OUTCOME_PENDING
    assert evidence["target_resolved"] is None
    assert evidence["policy_passed"] is None


def test_a_failed_analysis_is_inconclusive_not_verified():
    """The output may be fine or not; we have no evidence either way."""
    evidence = evaluate(
        record=record(), job_state="failed", frames_extracted=5, analysis=analysis()
    )
    assert evidence["outcome"] == OUTCOME_INCONCLUSIVE
    assert evidence["policy_passed"] is None
    assert "did not complete" in evidence["reason"]


def test_an_uploaded_output_that_was_never_analysed_is_pending():
    evidence = evaluate(
        record=record(),
        job_state="uploaded",
        frames_extracted=None,
        analysis=analysis(),
    )
    assert evidence["outcome"] == OUTCOME_PENDING


def test_a_completed_analysis_with_no_audited_frames_is_inconclusive():
    """The trap this check exists for.

    A completed job that audited nothing returns an empty detections list,
    which reads exactly like a clean video. It is not evidence of anything.
    """
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[], audited=0, failed=10),
    )
    assert evidence["outcome"] == OUTCOME_INCONCLUSIVE
    assert evidence["target_resolved"] is None
    assert "no audited frames" in evidence["reason"]


# ---------------------------------------------------------------------------
# the real verdicts
# ---------------------------------------------------------------------------

def test_a_resolved_target_verifies_under_the_default_policy():
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[]),
        policy=POLICY_TARGET_ONLY,
    )
    assert evidence["outcome"] == OUTCOME_VERIFIED
    assert evidence["target_resolved"] is True
    assert evidence["policy_passed"] is True
    assert evidence["other_findings"] == []


def test_a_remaining_target_fails_verification():
    """The whole point: a correction that did not work must say so."""
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[finding(timestamp=3.0)]),
    )
    assert evidence["outcome"] == OUTCOME_FAILED
    assert evidence["target_resolved"] is False
    assert evidence["policy_passed"] is False
    assert evidence["target_matches"]
    assert "still detected" in evidence["reason"]


def test_other_findings_are_retained_and_exposed():
    """A resolved target does not make the rest of the video disappear."""
    other = finding(category="unsafe_item", timestamp=7.0)
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[other]),
        policy=POLICY_TARGET_ONLY,
    )
    assert evidence["target_resolved"] is True
    assert evidence["policy_passed"] is True
    assert evidence["other_finding_count"] == 1
    assert evidence["other_findings"][0]["category"] == "unsafe_item"


def test_the_three_outcomes_are_never_conflated():
    """Target resolved, policy passed, and other findings are separate facts."""
    other = finding(category="unsafe_item", timestamp=7.0)
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[other]),
        policy=POLICY_CLEAN_OUTPUT,
    )
    # Target gone, but the policy demands a clean output and it is not clean.
    assert evidence["target_resolved"] is True
    assert evidence["policy_passed"] is False
    assert evidence["outcome"] == OUTCOME_FAILED
    assert evidence["other_finding_count"] == 1
    assert "still has" in evidence["reason"]


def test_the_clean_output_policy_verifies_a_genuinely_clean_output():
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[]),
        policy=POLICY_CLEAN_OUTPUT,
    )
    assert evidence["outcome"] == OUTCOME_VERIFIED


def test_new_violations_in_the_output_are_reported():
    """A correction can introduce a problem; the evidence must show it."""
    introduced = finding(category="offensive_text", timestamp=2.5)
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[introduced]),
        policy=POLICY_TARGET_ONLY,
    )
    assert evidence["other_findings"][0]["category"] == "offensive_text"


def test_the_evidence_is_explainable_from_its_own_fields():
    """Verification must be repeatable and explainable from persisted records."""
    evidence = evaluate(
        record=record(),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[]),
    )
    for key in (
        "policy",
        "evaluated_at",
        "source_video_id",
        "output_video_id",
        "analysis_job_video_id",
        "analysis_state",
        "frames_extracted",
        "frames_audited",
        "frames_failed",
        "target_category",
        "target_timestamp_seconds",
        "timestamp_tolerance_seconds",
        "outcome",
        "target_resolved",
        "policy_passed",
        "other_findings",
        "reason",
    ):
        assert key in evidence, key
    # The evidence names the OUTPUT, never the source, as what was analysed.
    assert evidence["output_video_id"] == OUTPUT_ID
    assert evidence["analysis_job_video_id"] == OUTPUT_ID


def test_the_evidence_carries_no_storage_key_or_token():
    evidence = evaluate(
        record=record(output_key="remediated/vid_0000beef/output.mp4"),
        job_state="completed",
        frames_extracted=10,
        analysis=analysis(detections=[]),
    )
    rendered = str(evidence)
    assert "remediated/" not in rendered
    assert "access_token" not in rendered


# ---------------------------------------------------------------------------
# reading the output's persisted results
# ---------------------------------------------------------------------------

def _dynamo_with(items, fail=None):
    dynamo = MagicMock()
    table = dynamo.Table.return_value
    if fail is not None:
        table.query.side_effect = fail
    else:
        table.query.return_value = {"Items": items}
    return dynamo


def test_read_output_findings_reads_the_output_video_id():
    """It must query the OUTPUT's frames, never the source's."""
    dynamo = _dynamo_with([])
    with patch("aws_clients.get_resource", return_value=dynamo):
        read_output_findings(OUTPUT_ID)

    kwargs = dynamo.Table.return_value.query.call_args.kwargs
    condition = kwargs["KeyConditionExpression"]
    assert OUTPUT_ID in str(condition.get_expression()["values"])


def test_read_output_findings_counts_audited_and_failed_frames():
    items = [
        {
            "video_id": OUTPUT_ID,
            "frame_number": Decimal(0),
            "status": "audited",
            "timestamp": Decimal("0"),
            "detections": [],
        },
        {
            "video_id": OUTPUT_ID,
            "frame_number": Decimal(1),
            "status": "audit_failed",
            "timestamp": Decimal("1"),
            "detections": [],
        },
    ]
    with patch("aws_clients.get_resource", return_value=_dynamo_with(items)):
        result = read_output_findings(OUTPUT_ID)

    assert result["frames_audited"] == 1
    assert result["frames_failed"] == 1
    assert result["frames_seen"] == 2


def test_read_output_findings_keeps_an_absent_confidence_absent():
    """A fabricated 0.0 would assert certainty the model never expressed."""
    items = [
        {
            "video_id": OUTPUT_ID,
            "frame_number": Decimal(3),
            "status": "audited",
            "timestamp": Decimal("3"),
            "detections": [{"category": "logo"}],
        }
    ]
    with patch("aws_clients.get_resource", return_value=_dynamo_with(items)):
        result = read_output_findings(OUTPUT_ID)

    assert result["detections"][0]["confidence"] is None


def test_read_output_findings_converts_decimals():
    items = [
        {
            "video_id": OUTPUT_ID,
            "frame_number": Decimal(3),
            "status": "audited",
            "timestamp": Decimal("3.5"),
            "detections": [
                {"category": "logo", "confidence": Decimal("0.91")}
            ],
        }
    ]
    with patch("aws_clients.get_resource", return_value=_dynamo_with(items)):
        result = read_output_findings(OUTPUT_ID)

    detection = result["detections"][0]
    assert detection["confidence"] == pytest.approx(0.91)
    assert detection["timestamp"] == pytest.approx(3.5)


def test_a_storage_failure_raises_rather_than_reporting_a_clean_video():
    """An outage must never read as evidence of compliance."""
    from botocore.exceptions import ClientError

    failure = ClientError({"Error": {"Code": "Throttling"}}, "Query")
    with patch(
        "aws_clients.get_resource", return_value=_dynamo_with([], fail=failure)
    ):
        with pytest.raises(verification.VerificationError):
            read_output_findings(OUTPUT_ID)


# ---------------------------------------------------------------------------
# persisting the verdict
# ---------------------------------------------------------------------------

async def _seed(fake_db, state=RemediationState.VERIFYING, **overrides):
    """Create a remediation and drive it to `state` with Phase 9 fields set."""
    created = await remediation_repo.create(
        video_id=VIDEO_ID,
        owner="alice",
        frame_number=3,
        detection_index=0,
        finding_fingerprint="fp",
        finding=record()["finding"],
        proposal={"confidence_gate": "pass"},
    )
    col = fake_db[remediation_repo.COLLECTION]
    await col.update_one(
        {"remediation_id": created["remediation_id"]},
        {
            "$set": {
                "state": state.value,
                "output_video_id": OUTPUT_ID,
                "analysis_job_video_id": OUTPUT_ID,
                **overrides,
            }
        },
    )
    return created["remediation_id"]


async def _seed_output_job(fake_db, status="completed", owner="alice"):
    import video_repo

    await video_repo.record_video(
        video_id=OUTPUT_ID,
        owner=owner,
        stored_filename=f"{OUTPUT_ID}.mp4",
        original_filename="clip (corrected).mp4",
        extension="mp4",
        size_bytes=2048,
        s3_bucket="test-bucket",
        s3_key=f"remediated/{OUTPUT_ID}/output.mp4",
    )
    await fake_db["videos"].update_one(
        {"video_id": OUTPUT_ID},
        {"$set": {"status": status, "frames_extracted": 10}},
    )


def test_advance_records_verified_only_from_real_results(fake_db):
    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="completed")
        with patch(
            "verification.read_output_findings",
            return_value=analysis(detections=[]),
        ):
            return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == RemediationState.VERIFIED.value
    assert result["verification"]["outcome"] == OUTCOME_VERIFIED
    assert result["verification_video_id"] == OUTPUT_ID
    assert result["verified_at"] is not None


def test_advance_records_verification_failed_when_the_target_remains(fake_db):
    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="completed")
        with patch(
            "verification.read_output_findings",
            return_value=analysis(detections=[finding(timestamp=3.0)]),
        ):
            return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == RemediationState.VERIFICATION_FAILED.value
    assert result["verification"]["target_resolved"] is False
    assert result["error_code"] == "target_not_resolved"
    # Never a compliance claim. Asserted through the client-facing projection,
    # which is what a caller actually receives.
    assert remediation_repo.view(result)["is_compliance_claim"] is False


def test_advance_leaves_a_running_analysis_in_verifying(fake_db):
    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="analyzing")
        return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == RemediationState.VERIFYING.value
    assert result["verification"] is None
    assert result["verified_at"] is None


def test_advance_does_not_verify_when_the_analysis_failed(fake_db):
    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="failed")
        with patch(
            "verification.read_output_findings",
            return_value=analysis(detections=[], audited=0),
        ):
            return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    # Terminal FAILED, not VERIFICATION_FAILED: the latter asserts a
    # re-analysis ran and disagreed, which is a stronger claim.
    assert result["state"] == RemediationState.FAILED.value
    assert result["error_code"] == "verification_inconclusive"
    assert remediation_repo.view(result)["is_compliance_claim"] is False


def test_advance_leaves_the_record_alone_when_results_cannot_be_read(fake_db):
    """A storage failure defers; it never produces a verdict."""

    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="completed")
        with patch(
            "verification.read_output_findings",
            side_effect=verification.VerificationError("boom"),
        ):
            return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == RemediationState.VERIFYING.value
    assert result["verification"] is None


def test_advance_refuses_an_output_job_owned_by_someone_else(fake_db):
    """Defence against a crossed link verifying one user from another's data."""

    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="completed", owner="bob")
        return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == RemediationState.FAILED.value
    assert result["error_code"] == "verification_unlinked"


def test_advance_fails_safely_when_no_analysis_job_is_recorded(fake_db):
    async def scenario():
        rid = await _seed(
            fake_db, output_video_id=None, analysis_job_video_id=None
        )
        return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == RemediationState.FAILED.value
    assert result["error_code"] == "verification_unlinked"


@pytest.mark.parametrize(
    "state",
    [
        RemediationState.PROPOSED,
        RemediationState.APPROVED,
        RemediationState.ASSET_READY,
        RemediationState.APPLIED,
        RemediationState.VERIFIED,
        RemediationState.REJECTED,
    ],
)
def test_advance_never_reaches_back_into_another_stage(fake_db, state):
    """Only a VERIFYING record is evaluated.

    So this function cannot skip the stages before it, and cannot re-open a
    verdict already recorded.
    """

    async def scenario():
        rid = await _seed(fake_db, state=state)
        await _seed_output_job(fake_db, status="completed")
        with patch(
            "verification.read_output_findings",
            return_value=analysis(detections=[]),
        ):
            return await verification.advance(rid, "alice")

    result = anyio.run(scenario)
    assert result["state"] == state.value


def test_advance_is_idempotent(fake_db):
    """Safe to call from the worker sweep and the status route at once."""

    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="completed")
        with patch(
            "verification.read_output_findings",
            return_value=analysis(detections=[]),
        ):
            first = await verification.advance(rid, "alice")
            second = await verification.advance(rid, "alice")
        return first, second

    first, second = anyio.run(scenario)
    assert first["state"] == RemediationState.VERIFIED.value
    assert second["state"] == RemediationState.VERIFIED.value
    assert first["verified_at"] == second["verified_at"]


def test_the_original_videos_results_are_never_used_as_evidence(fake_db):
    """The named hazard: verifying the output from the source's analysis.

    The source still carries the finding. If `advance` read the source's
    frames it would report the target as unresolved; if it ignored which
    video it read, the verdict would be meaningless. It queries the OUTPUT id.
    """

    async def scenario():
        rid = await _seed(fake_db)
        await _seed_output_job(fake_db, status="completed")
        seen = {}

        def fake_read(video_id):
            seen["video_id"] = video_id
            return analysis(detections=[])

        with patch("verification.read_output_findings", side_effect=fake_read):
            await verification.advance(rid, "alice")
        return seen

    seen = anyio.run(scenario)
    assert seen["video_id"] == OUTPUT_ID
    assert seen["video_id"] != VIDEO_ID
