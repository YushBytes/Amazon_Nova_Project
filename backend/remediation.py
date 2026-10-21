"""Building a remediation proposal from a finding the analyzer really stored.

Phase 8. No I/O: this module turns a persisted DynamoDB frame row into a
validated proposal, and nothing here contacts a provider or writes anything.
`remediation_repo` persists; `remediation_routes` authorizes and serves.

WHAT A FINDING IS, AND HOW IT IS NAMED
--------------------------------------
The analyzer writes one DynamoDB row per frame, keyed (video_id,
frame_number), with `detections` as an ordered list. There is no per-detection
identifier stored, so a finding is addressed positionally:

    (video_id, frame_number, detection_index)

That address is not stable across a re-analysis. `analyzer._record_frame_result`
overwrites the row for a frame, and a second Bedrock call on the same frame can
return a different number of violations in a different order - so index 2 after
a retry may be a different finding, or may not exist. A proposal that stored
only the address would silently come to describe something else.

Each proposal therefore also stores a FINGERPRINT of the finding's content.
Before anything consequential happens the finding is re-read and re-fingerprinted;
a mismatch means the analysis changed underneath the proposal, and the proposal
is moved to `superseded` rather than acted on. That is the whole reason
`superseded` exists as a state.

WHAT A PROPOSAL IS NOT
----------------------
It is not a brand-policy determination. This application has no configurable
brand-rule system: there is one static prompt in `backend/prompts.py`, and the
only evidence a proposal has is what that prompt's model output produced. So a
proposal says what the model reported and what change would follow from it, and
states its own limits. It does not cite a guideline, a trademark, a regulation
or a client policy, because no such thing is configured here and inventing one
would be worse than having none.

Nor is the model's own `description` or `recommendation` treated as proof. It
is quoted as the model's claim, attributed, and never restated as fact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import config
from detections import BoundingBox

# How a finding's confidence compares with the configured threshold.
GATE_PASS = "pass"
GATE_BELOW_THRESHOLD = "below_threshold"
GATE_NOT_REPORTED = "not_reported"

# The kind of change a proposal describes. Only one is defined: there is
# exactly one capability a provider could plausibly be asked for here, and
# naming speculative alternatives would imply they exist.
ACTION_REMOVE_REGION = "remove_region"

# Fixed, non-negotiable text attached to every proposal. A reviewer must see
# the limits with the proposal, not in documentation they may never open.
PROPOSAL_LIMITATIONS = (
    "This proposal is derived only from one model's analysis of a single "
    "video frame. It is not a brand-policy, trademark or legal determination, "
    "and this application has no configured brand-rule system to check "
    "against. Detection accuracy has not been measured. Frames are sampled at "
    "1 fps, so neighbouring frames may contain the same issue without a "
    "separate finding. Approving this proposal records your decision; it does "
    "not by itself change any video."
)


class FindingNotFound(LookupError):
    """The addressed finding does not exist in the persisted analysis."""


class FindingNotRemediable(ValueError):
    """The finding exists but cannot be the subject of a proposal.

    Carries a safe, user-facing `reason`. Distinct from FindingNotFound so a
    route can answer 422 ("this finding cannot be remediated, here is why")
    rather than 404 ("no such finding"), which are different facts.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Finding:
    """One validated, persisted compliance finding.

    Built from a stored DynamoDB detection map, which the analyzer wrote from
    an already-validated `detections.Detection`. Re-validated here anyway: the
    row may have been written by an older pipeline version, and this module is
    the last point before a proposal is created from it.
    """

    video_id: str
    frame_number: int
    detection_index: int
    category: str
    description: str
    risk_level: str | None
    confidence: float | None
    bounding_box: tuple[float, float, float, float] | None
    timestamp_seconds: float | None

    @property
    def fingerprint(self) -> str:
        """A stable hash of this finding's content.

        Covers every field a reviewer's decision depends on. If any of them
        changes - the category, the wording, the severity, the confidence, the
        region - the approval no longer applies to what is now stored, and the
        proposal must not be acted on.

        Deliberately excludes `detection_index` and `timestamp_seconds`:
        the index is the address rather than the content, and the timestamp is
        derived from the frame number, which is already part of the address.
        """
        payload = {
            "video_id": self.video_id,
            "frame_number": self.frame_number,
            "category": self.category,
            "description": self.description,
            "risk_level": self.risk_level,
            # Formatted, not raw: a float and a Decimal of the same value must
            # fingerprint identically, or a storage round-trip would look like
            # a changed finding.
            "confidence": None if self.confidence is None else f"{self.confidence:.4f}",
            "bounding_box": None
            if self.bounding_box is None
            else [f"{v:.4f}" for v in self.bounding_box],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _as_float(value: Any) -> float | None:
    """Read a stored number, tolerating the Decimal DynamoDB returns."""
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


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        try:
            return int(value)
        except (ValueError, ArithmeticError):
            return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _normalised_box(raw: Any) -> list[float] | None:
    """Convert a stored bounding box to plain floats, or None.

    Returns None for anything that is not a four-element sequence of readable
    numbers, so `BoundingBox.parse` still performs the range and ordering
    checks on a well-shaped input rather than having them bypassed here.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    values: list[float] = []
    for entry in raw:
        number = _as_float(entry)
        if number is None:
            return None
        values.append(number)
    return values


def finding_from_frame_item(
    item: dict, detection_index: int, video_id: str
) -> Finding:
    """Extract one finding from a stored frame row.

    Raises:
        FindingNotFound: the row carries no detection at that index, or the
            row is not an audited frame.
        FindingNotRemediable: the detection exists but is unusable as the
            subject of a proposal.
    """
    if not isinstance(item, dict):
        raise FindingNotFound("no such frame")

    # A frame that failed to audit stores an empty detections list precisely so
    # a failure can never read as "no violations". It also means there is
    # nothing here to remediate.
    status = item.get("status")
    if status != "audited":
        raise FindingNotFound("the frame has no completed analysis")

    detections = item.get("detections")
    if not isinstance(detections, (list, tuple)):
        raise FindingNotFound("the frame recorded no detections")
    if detection_index < 0 or detection_index >= len(detections):
        raise FindingNotFound("no such finding on this frame")

    raw = detections[detection_index]
    if not isinstance(raw, dict):
        raise FindingNotRemediable(
            "The stored finding could not be read."
        )

    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        # A finding with no subject is not something a human can review.
        raise FindingNotRemediable(
            "The stored finding has no category and cannot be reviewed."
        )

    frame_number = _as_int(item.get("frame_number"))
    if frame_number is None:
        raise FindingNotFound("the frame row has no frame number")

    description = raw.get("description")
    risk_level = raw.get("risk_level")

    # Re-validated through the same parser the analyzer used, so a box stored
    # by an older pipeline version that would now be rejected is dropped here
    # rather than reaching a provider.
    #
    # Normalised to plain floats FIRST. The analyzer writes coordinates as
    # Decimal (DynamoDB has no float type), and `BoundingBox.parse` accepts
    # only int/float because its input is model JSON, which never contains a
    # Decimal. Handing it the stored values directly silently dropped every
    # box - the same conversion `app._normalise_detection` does before serving
    # a detection to the browser.
    box = BoundingBox.parse(_normalised_box(raw.get("bounding_box")))

    return Finding(
        video_id=video_id,
        frame_number=frame_number,
        detection_index=detection_index,
        category=category.strip()[:200],
        description=(description.strip()[:2000] if isinstance(description, str) else ""),
        risk_level=(
            risk_level.strip().lower()
            if isinstance(risk_level, str) and risk_level.strip()
            else None
        ),
        confidence=_as_float(raw.get("confidence")),
        bounding_box=(
            (box.ymin, box.xmin, box.ymax, box.xmax) if box is not None else None
        ),
        timestamp_seconds=_as_float(item.get("timestamp")),
    )


# ---------------------------------------------------------------------------
# The confidence gate
# ---------------------------------------------------------------------------

def confidence_gate(
    confidence: float | None, threshold: float | None = None
) -> str:
    """Classify a finding's confidence against the configured threshold.

    `None` is GATE_NOT_REPORTED, never GATE_PASS. The model omitting a
    confidence is not evidence of a high one, and `detections.py` keeps it
    absent rather than defaulting it for exactly this reason.
    """
    if threshold is None:
        threshold = config.load_remediation_min_confidence()
    if confidence is None:
        return GATE_NOT_REPORTED
    return GATE_PASS if confidence >= threshold else GATE_BELOW_THRESHOLD


def gate_explanation(gate: str, threshold: float) -> str:
    """A reviewer-facing sentence about what the gate result means."""
    if gate == GATE_PASS:
        return (
            f"The model reported a confidence at or above the configured "
            f"threshold of {threshold:g}."
        )
    if gate == GATE_BELOW_THRESHOLD:
        return (
            f"The model's confidence is below the configured threshold of "
            f"{threshold:g}. You can still review and reject this finding, "
            f"but automated generation is refused for it."
        )
    return (
        "The model reported no confidence for this finding. An absent "
        "confidence is not treated as a high one, so automated generation is "
        "refused for it."
    )


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """What is being proposed, and on what basis.

    Every field is derived from the stored finding or from configuration.
    Nothing is generated by a model at proposal time: adding a second model
    call here would mean a model explaining another model's output, and an
    explanation is not evidence.
    """

    action: str
    summary: str
    rationale: str
    model_claim: str
    target_description: str
    limitations: str
    requires_approval: bool
    confidence_gate: str
    gate_explanation: str


def _region_phrase(box: tuple[float, float, float, float] | None) -> str:
    """Describe where on the frame the change would apply.

    In the documented 0-1000 normalized space, converted to percentages for a
    reader. No pixel coordinates: frame dimensions are not persisted anywhere,
    so a pixel figure would be invented.
    """
    if box is None:
        return (
            "The model did not return a usable region for this finding, so no "
            "specific area of the frame can be targeted."
        )
    ymin, xmin, ymax, xmax = box
    return (
        f"A region covering roughly {xmin / 10:.0f}%-{xmax / 10:.0f}% across "
        f"and {ymin / 10:.0f}%-{ymax / 10:.0f}% down the frame."
    )


def build_proposal(finding: Finding, threshold: float | None = None) -> Proposal:
    """Describe the change that follows from a finding.

    Grounded entirely in the stored finding. The rationale states what the
    model reported and attributes it; it never asserts the finding is correct,
    and never cites a guideline, because none is configured.
    """
    if threshold is None:
        threshold = config.load_remediation_min_confidence()

    gate = confidence_gate(finding.confidence, threshold)

    if finding.bounding_box is None:
        summary = (
            f"Review the reported {finding.category} at "
            f"frame {finding.frame_number} manually."
        )
    else:
        summary = (
            f"Obscure the reported {finding.category} in its region of "
            f"frame {finding.frame_number}."
        )

    severity = (
        f"The model rated it {finding.risk_level} risk. "
        if finding.risk_level
        else ""
    )
    confidence_text = (
        f"It reported {finding.confidence:.0%} confidence. "
        if finding.confidence is not None
        else "It reported no confidence figure. "
    )

    rationale = (
        f"The frame analysis flagged a {finding.category} on frame "
        f"{finding.frame_number}. {severity}{confidence_text}"
        f"That is the entire basis for this proposal: it reflects what the "
        f"model reported, not a confirmed violation of any specific rule."
    )

    model_claim = (
        finding.description
        if finding.description
        else "The model gave no description for this finding."
    )

    return Proposal(
        action=ACTION_REMOVE_REGION,
        summary=summary,
        rationale=rationale,
        model_claim=model_claim,
        target_description=_region_phrase(finding.bounding_box),
        limitations=PROPOSAL_LIMITATIONS,
        # Always true. There is no automatic path in this build, and the
        # field is explicit rather than implied so a later phase that adds one
        # has to set it to False deliberately.
        requires_approval=True,
        confidence_gate=gate,
        gate_explanation=gate_explanation(gate, threshold),
    )


__all__ = (
    "ACTION_REMOVE_REGION",
    "GATE_BELOW_THRESHOLD",
    "GATE_NOT_REPORTED",
    "GATE_PASS",
    "PROPOSAL_LIMITATIONS",
    "Finding",
    "FindingNotFound",
    "FindingNotRemediable",
    "Proposal",
    "build_proposal",
    "confidence_gate",
    "finding_from_frame_item",
    "gate_explanation",
)
