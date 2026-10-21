"""Building a proposal from a persisted finding, and the confidence gate.

Phase 8. Two things are under test:

* `finding_from_frame_item` - reading a finding out of a real DynamoDB frame
  row, including every way that row can fail to yield one. Decimal handling
  matters here: DynamoDB has no float type, so a confidence stored by the
  analyzer comes back as Decimal and must fingerprint identically to the float
  it was written from.
* the confidence gate - `prompts.py` has asked the model for a confidence
  since Phase 1 and `docs/planning.md` specified 0.8, but until Phase 8 no
  code read either.
"""

from decimal import Decimal

import pytest

import remediation
from remediation import (
    GATE_BELOW_THRESHOLD,
    GATE_NOT_REPORTED,
    GATE_PASS,
    PROPOSAL_LIMITATIONS,
    FindingNotFound,
    FindingNotRemediable,
    build_proposal,
    confidence_gate,
    finding_from_frame_item,
)


def frame_item(detections=None, status="audited", frame_number=3, timestamp=3.0):
    """A frame row shaped exactly as `analyzer._record_frame_result` writes it."""
    if detections is None:
        detections = [
            {
                "category": "competitor_logo",
                "description": "A rival mark is visible on the cup.",
                "risk_level": "high",
                "confidence": Decimal("0.91"),
                "bounding_box": [
                    Decimal("100"),
                    Decimal("200"),
                    Decimal("300"),
                    Decimal("400"),
                ],
            }
        ]
    return {
        "video_id": "vid_abcd1234",
        "frame_number": Decimal(frame_number),
        "status": status,
        "timestamp": Decimal(str(timestamp)),
        "detections": detections,
    }


# ---------------------------------------------------------------------------
# reading a finding
# ---------------------------------------------------------------------------

def test_reads_a_finding_from_a_real_frame_row():
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")

    assert finding.video_id == "vid_abcd1234"
    assert finding.frame_number == 3
    assert finding.detection_index == 0
    assert finding.category == "competitor_logo"
    assert finding.description == "A rival mark is visible on the cup."
    assert finding.risk_level == "high"
    assert finding.confidence == pytest.approx(0.91)
    assert finding.bounding_box == (100.0, 200.0, 300.0, 400.0)
    assert finding.timestamp_seconds == pytest.approx(3.0)


def test_a_failed_frame_yields_no_finding():
    """`audit_failed` frames store an empty detections list by design.

    A failure must never read as a finding, nor as "no violations".
    """
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(frame_item(status="audit_failed"), 0, "vid_abcd1234")


def test_a_pending_frame_yields_no_finding():
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(frame_item(status="pending_audit"), 0, "vid_abcd1234")


@pytest.mark.parametrize("index", [1, 5, 99])
def test_an_out_of_range_index_is_not_found(index):
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(frame_item(), index, "vid_abcd1234")


def test_a_negative_index_is_not_found():
    """Must not wrap around to the last detection, as Python indexing would."""
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(frame_item(), -1, "vid_abcd1234")


def test_an_empty_detections_list_is_not_found():
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(frame_item(detections=[]), 0, "vid_abcd1234")


def test_a_missing_detections_field_is_not_found():
    item = frame_item()
    del item["detections"]
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(item, 0, "vid_abcd1234")


def test_a_finding_with_no_category_is_not_remediable():
    """Distinct from not-found: it exists but a human cannot review it."""
    with pytest.raises(FindingNotRemediable):
        finding_from_frame_item(
            frame_item(detections=[{"description": "something"}]), 0, "vid_abcd1234"
        )


def test_a_non_mapping_detection_is_not_remediable():
    with pytest.raises(FindingNotRemediable):
        finding_from_frame_item(
            frame_item(detections=["not a dict"]), 0, "vid_abcd1234"
        )


def test_a_missing_optional_field_stays_missing():
    """Absent is not zero. `detections.py` keeps it absent for this reason."""
    finding = finding_from_frame_item(
        frame_item(detections=[{"category": "unsafe_item"}]), 0, "vid_abcd1234"
    )
    assert finding.confidence is None
    assert finding.risk_level is None
    assert finding.bounding_box is None
    assert finding.description == ""


@pytest.mark.parametrize(
    "box",
    [
        [1, 2, 3],  # too short
        [1, 2, 3, 4, 5],  # too long
        [500, 100, 100, 400],  # inverted on y
        [100, 400, 300, 200],  # inverted on x
        [-1, 0, 100, 100],  # out of range
        [0, 0, 1001, 100],  # out of range
        "100,200,300,400",  # not a sequence of numbers
        [None, None, None, None],
        [True, False, True, False],  # bools are not coordinates
        None,
    ],
)
def test_a_malformed_box_is_dropped_without_raising(box):
    """A bad box costs the box, not the finding.

    Re-validated here through the same parser the analyzer used, so a row
    written by an older pipeline version cannot carry an unusable region into
    a provider request.
    """
    finding = finding_from_frame_item(
        frame_item(detections=[{"category": "logo", "bounding_box": box}]),
        0,
        "vid_abcd1234",
    )
    assert finding.bounding_box is None


def test_a_row_with_no_frame_number_is_not_found():
    item = frame_item()
    del item["frame_number"]
    with pytest.raises(FindingNotFound):
        finding_from_frame_item(item, 0, "vid_abcd1234")


def test_a_non_mapping_row_is_not_found():
    with pytest.raises(FindingNotFound):
        finding_from_frame_item("nope", 0, "vid_abcd1234")


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------

def test_the_same_finding_fingerprints_identically():
    a = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    b = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    assert a.fingerprint == b.fingerprint


def test_a_decimal_and_a_float_confidence_fingerprint_identically():
    """A storage round-trip must not look like a changed finding.

    The analyzer writes Decimal("0.91"); a float 0.91 is the same finding.
    If these differed, every approval would be superseded on the next read.
    """
    stored = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    in_memory = finding_from_frame_item(
        frame_item(
            detections=[
                {
                    "category": "competitor_logo",
                    "description": "A rival mark is visible on the cup.",
                    "risk_level": "high",
                    "confidence": 0.91,
                    "bounding_box": [100.0, 200.0, 300.0, 400.0],
                }
            ]
        ),
        0,
        "vid_abcd1234",
    )
    assert stored.fingerprint == in_memory.fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        {"category": "something_else"},
        {"description": "different wording"},
        {"risk_level": "low"},
        {"confidence": Decimal("0.50")},
        {"bounding_box": [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]},
    ],
)
def test_changing_any_reviewed_field_changes_the_fingerprint(mutation):
    """Every field a reviewer's decision depends on is covered.

    This is what makes `superseded` work: if a re-analysis changes what the
    owner was shown, the approval no longer applies.
    """
    base = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")

    detection = dict(frame_item()["detections"][0])
    detection.update(mutation)
    changed = finding_from_frame_item(
        frame_item(detections=[detection]), 0, "vid_abcd1234"
    )
    assert changed.fingerprint != base.fingerprint


def test_the_fingerprint_ignores_the_index_and_the_timestamp():
    """The index is the address, not the content.

    A finding that moved from index 1 to index 0 without changing is still the
    same finding; the timestamp is derived from the frame number, which is
    already covered.
    """
    two = frame_item(
        detections=[
            {"category": "other", "description": "x"},
            dict(frame_item()["detections"][0]),
        ]
    )
    at_index_one = finding_from_frame_item(two, 1, "vid_abcd1234")
    at_index_zero = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    assert at_index_one.fingerprint == at_index_zero.fingerprint


def test_the_fingerprint_covers_the_frame_number():
    a = finding_from_frame_item(frame_item(frame_number=3), 0, "vid_abcd1234")
    b = finding_from_frame_item(frame_item(frame_number=4), 0, "vid_abcd1234")
    assert a.fingerprint != b.fingerprint


def test_the_fingerprint_covers_the_video_id():
    a = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    b = finding_from_frame_item(frame_item(), 0, "vid_ffff0000")
    assert a.fingerprint != b.fingerprint


# ---------------------------------------------------------------------------
# the confidence gate
# ---------------------------------------------------------------------------

def test_a_confidence_at_the_threshold_passes():
    assert confidence_gate(0.8, threshold=0.8) == GATE_PASS


def test_a_confidence_above_the_threshold_passes():
    assert confidence_gate(0.95, threshold=0.8) == GATE_PASS


def test_a_confidence_below_the_threshold_does_not_pass():
    assert confidence_gate(0.79, threshold=0.8) == GATE_BELOW_THRESHOLD


def test_an_absent_confidence_does_not_pass():
    """Absent is not high.

    The model omitting a confidence is not evidence of a confident finding,
    and `detections.py` keeps it absent rather than defaulting it to 0.0.
    """
    assert confidence_gate(None, threshold=0.8) == GATE_NOT_REPORTED


def test_a_zero_confidence_is_distinguishable_from_an_absent_one():
    assert confidence_gate(0.0, threshold=0.8) == GATE_BELOW_THRESHOLD
    assert confidence_gate(None, threshold=0.8) == GATE_NOT_REPORTED


def test_the_default_threshold_matches_the_documented_one(monkeypatch):
    """docs/planning.md specifies 0.8; config is where it finally lives."""
    monkeypatch.delenv("REMEDIATION_MIN_CONFIDENCE", raising=False)
    assert confidence_gate(0.8) == GATE_PASS
    assert confidence_gate(0.79) == GATE_BELOW_THRESHOLD


# ---------------------------------------------------------------------------
# the proposal
# ---------------------------------------------------------------------------

def test_a_proposal_is_grounded_in_the_stored_finding():
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    proposal = build_proposal(finding, threshold=0.8)

    assert proposal.action == remediation.ACTION_REMOVE_REGION
    assert "competitor_logo" in proposal.summary
    assert "frame 3" in proposal.summary
    # The model's own words are quoted, attributed, and kept separate from the
    # rationale - an explanation is not evidence.
    assert proposal.model_claim == "A rival mark is visible on the cup."
    assert "the model reported" in proposal.rationale.lower()


def test_a_proposal_always_requires_approval():
    """There is no automatic path in this build, and the field says so."""
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    assert build_proposal(finding, threshold=0.8).requires_approval is True


def test_a_proposal_states_its_own_limits():
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    proposal = build_proposal(finding, threshold=0.8)
    assert proposal.limitations == PROPOSAL_LIMITATIONS
    assert "not a brand-policy" in proposal.limitations
    assert "does not by itself change any video" in proposal.limitations


def test_a_proposal_invents_no_brand_rule_or_legal_claim():
    """The application has no configurable brand-rule system to cite."""
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    proposal = build_proposal(finding, threshold=0.8)

    text = " ".join(
        [proposal.summary, proposal.rationale, proposal.target_description]
    ).lower()
    for invented in ("trademark", "guideline", "regulation", "policy §", "clause"):
        assert invented not in text


def test_a_proposal_never_claims_the_finding_is_confirmed():
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    proposal = build_proposal(finding, threshold=0.8)
    lowered = proposal.rationale.lower()
    assert "not a confirmed violation" in lowered


def test_a_proposal_describes_the_region_as_percentages():
    """No pixel figures: frame dimensions are not persisted anywhere."""
    finding = finding_from_frame_item(frame_item(), 0, "vid_abcd1234")
    proposal = build_proposal(finding, threshold=0.8)
    assert "%" in proposal.target_description
    assert "px" not in proposal.target_description


def test_a_proposal_for_a_finding_with_no_region_says_so():
    finding = finding_from_frame_item(
        frame_item(detections=[{"category": "offensive_text"}]), 0, "vid_abcd1234"
    )
    proposal = build_proposal(finding, threshold=0.8)
    assert "did not return a usable region" in proposal.target_description
    assert "manually" in proposal.summary


def test_a_proposal_records_the_gate_and_explains_it():
    low = finding_from_frame_item(
        frame_item(
            detections=[{"category": "logo", "confidence": Decimal("0.40")}]
        ),
        0,
        "vid_abcd1234",
    )
    proposal = build_proposal(low, threshold=0.8)
    assert proposal.confidence_gate == GATE_BELOW_THRESHOLD
    assert "0.8" in proposal.gate_explanation
    # A reviewer is told they can still look at it; only generation is refused.
    assert "reject" in proposal.gate_explanation.lower()


def test_a_proposal_for_an_unreported_confidence_explains_that_too():
    unreported = finding_from_frame_item(
        frame_item(detections=[{"category": "logo"}]), 0, "vid_abcd1234"
    )
    proposal = build_proposal(unreported, threshold=0.8)
    assert proposal.confidence_gate == GATE_NOT_REPORTED
    assert "no confidence" in proposal.gate_explanation.lower()
    assert "not treated as a high one" in proposal.gate_explanation
