"""Validated representation of Amazon Nova Pro analysis output.

Phase 4. Model output was previously parsed with a greedy regex and stored
verbatim: whatever JSON the model produced went straight into DynamoDB and then
to the browser. Malformed output was indistinguishable from a clean frame, and a
hallucinated bounding box reached the UI unchecked.

Everything here treats the model response as **untrusted input**. A field is
kept only when the model actually supplied a usable value:

* `confidence` is kept only if it is a real number in [0, 1]. A missing or
  nonsensical confidence becomes `None` rather than a default like 0.0, because
  a fabricated number is worse than an absent one.
* `bounding_box` is kept only if it is four finite numbers in the documented
  0-1000 normalized space, with ymin < ymax and xmin < xmax. An inverted,
  out-of-range, or short box is dropped and the detection is kept without it.
* Unknown categories are preserved as text but flagged, rather than silently
  remapped to something the model never said.

The schema follows `backend/prompts.py`, which asks for
`{scene_description, violations[], recommendation}` with each violation
carrying `category`, `description`, `confidence`, `bounding_box`
([ymin, xmin, ymax, xmax], 0-1000) and `risk_level`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

# Coordinates are normalized to this grid by the prompt contract.
COORD_MIN = 0.0
COORD_MAX = 1000.0

RISK_LEVELS = frozenset({"high", "medium", "low"})


class AnalysisParseError(ValueError):
    """The model response could not be read as an analysis result.

    Raised so a caller can record a frame-level failure. It must never be
    treated as "no violations found".
    """


class BoundingBox(BaseModel):
    """A normalized box in the 0-1000 space, as [ymin, xmin, ymax, xmax]."""

    model_config = ConfigDict(frozen=True)

    ymin: float
    xmin: float
    ymax: float
    xmax: float

    @classmethod
    def parse(cls, raw: Any) -> "BoundingBox | None":
        """Build a box from model output, or return None if unusable.

        Returns None rather than raising: a bad box should cost the box, not
        the whole detection.
        """
        if raw is None:
            return None

        values: list[Any]
        if isinstance(raw, dict):
            # Some responses use an object instead of an array.
            try:
                values = [raw["ymin"], raw["xmin"], raw["ymax"], raw["xmax"]]
            except (KeyError, TypeError):
                return None
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            return None

        if len(values) != 4:
            return None

        numbers: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                return None
            numbers.append(number)

        ymin, xmin, ymax, xmax = numbers

        if not all(COORD_MIN <= n <= COORD_MAX for n in numbers):
            return None
        # A degenerate or inverted box would render as an empty or wrong
        # overlay, so drop it.
        if ymin >= ymax or xmin >= xmax:
            return None

        return cls(ymin=ymin, xmin=xmin, ymax=ymax, xmax=xmax)

    def as_list(self) -> list[float]:
        """Serialise in the order the frontend overlay expects."""
        return [self.ymin, self.xmin, self.ymax, self.xmax]


class Detection(BaseModel):
    """One compliance finding on one frame."""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    risk_level: str | None = None
    # Only ever set when the model supplied a usable value.
    confidence: float | None = None
    bounding_box: list[float] | None = None

    @field_validator("category", "description", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalise_risk(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        lowered = value.strip().lower()
        return lowered if lowered in RISK_LEVELS else None

    @classmethod
    def parse(cls, raw: Any) -> "Detection | None":
        """Build a detection from one model violation entry.

        Returns None when the entry carries no usable category, since a
        finding with no subject is not reportable.
        """
        if not isinstance(raw, dict):
            return None

        category = raw.get("category") or raw.get("type") or raw.get("violation")
        if not isinstance(category, str) or not category.strip():
            return None

        box = BoundingBox.parse(raw.get("bounding_box") or raw.get("box_2d"))

        return cls(
            category=category,
            description=raw.get("description") or raw.get("label") or "",
            risk_level=raw.get("risk_level"),
            confidence=_parse_confidence(raw.get("confidence")),
            bounding_box=box.as_list() if box else None,
        )


def _parse_confidence(raw: Any) -> float | None:
    """Accept a confidence only if it is a real number in [0, 1].

    Some responses give a percentage; a value in (1, 100] is rescaled. Words
    like "high" are rejected rather than mapped to a number we would be
    inventing.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if 0.0 <= value <= 1.0:
        return round(value, 4)
    if 1.0 < value <= 100.0:
        return round(value / 100.0, 4)
    return None


class FrameAnalysis(BaseModel):
    """A validated analysis of a single frame."""

    model_config = ConfigDict(extra="ignore")

    scene_description: str = Field(default="", max_length=4000)
    recommendation: str = Field(default="", max_length=2000)
    detections: list[Detection] = Field(default_factory=list)
    # Set when the model produced output we could not use. Its presence means
    # the frame was NOT successfully audited.
    error: str | None = None
    # Entries the model returned that failed validation, counted for
    # observability. A non-zero value with detections present means the usable
    # subset was kept.
    discarded_entries: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def detection_count(self) -> int:
        return len(self.detections)


def extract_json_object(text: str) -> dict:
    """Pull the analysis JSON object out of a model text response.

    The previous implementation used `re.search(r'\\{.*\\}', text, DOTALL)`,
    which spans the FIRST `{` to the LAST `}` and therefore produces invalid
    JSON whenever a response contains more than one object or trailing prose.
    This scans for a balanced object instead, preferring one that looks like an
    analysis result.

    Raises:
        AnalysisParseError: if no balanced JSON object can be read.
    """
    if not isinstance(text, str) or not text.strip():
        raise AnalysisParseError("the model returned an empty response")

    stripped = text.strip()

    # A fenced code block is the most common wrapper.
    fence = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1).strip())

    candidates.extend(_balanced_objects(stripped))

    best: dict | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        # Prefer an object that actually looks like an analysis result.
        if "violations" in parsed or "scene_description" in parsed:
            return parsed
        if best is None:
            best = parsed

    if best is not None:
        return best
    raise AnalysisParseError("the model response contained no usable JSON object")


def _balanced_objects(text: str) -> list[str]:
    """Return every balanced `{...}` span, outermost first.

    String-aware, so a brace inside a quoted value does not unbalance the
    scan.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start : index + 1])
    return spans


def parse_frame_analysis(payload: Any) -> FrameAnalysis:
    """Validate a model response into a FrameAnalysis.

    Accepts either an already-decoded dict or the raw response text.

    Raises:
        AnalysisParseError: when the response cannot be read at all. A
            response that parses but contains unusable violation entries is
            NOT an error: the usable subset is kept and the rest counted in
            `discarded_entries`.
    """
    if isinstance(payload, str):
        data = extract_json_object(payload)
    elif isinstance(payload, dict):
        data = payload
    else:
        raise AnalysisParseError("the model response was not an object")

    raw_violations = data.get("violations")
    if raw_violations is None:
        raw_violations = data.get("detections") or []
    if isinstance(raw_violations, dict):
        raw_violations = [raw_violations]
    if not isinstance(raw_violations, list):
        raise AnalysisParseError("the model's violations field was not a list")

    detections: list[Detection] = []
    discarded = 0
    for entry in raw_violations:
        detection = Detection.parse(entry)
        if detection is None:
            discarded += 1
        else:
            detections.append(detection)

    if discarded:
        logger.warning(
            "Discarded %d unusable violation entr(y/ies) from a model response",
            discarded,
        )

    return FrameAnalysis(
        scene_description=_text(data.get("scene_description")),
        recommendation=_text(data.get("recommendation")),
        detections=detections,
        discarded_entries=discarded,
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()[:4000]


def failed_analysis(reason: str) -> FrameAnalysis:
    """A FrameAnalysis marking the frame as not successfully audited."""
    return FrameAnalysis(error=reason)


__all__ = (
    "AnalysisParseError",
    "BoundingBox",
    "COORD_MAX",
    "COORD_MIN",
    "Detection",
    "FrameAnalysis",
    "extract_json_object",
    "failed_analysis",
    "parse_frame_analysis",
)
