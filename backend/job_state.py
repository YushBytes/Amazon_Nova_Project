"""Analysis job state model.

Phase 4. Before this, a video's only state was a free-text `status` string on
its ownership record, set to "UPLOADED" at upload and "ANALYZING" when analysis
was *scheduled*. Nothing ever moved it to a terminal value, so a job that
failed in the background was indistinguishable from one still running, and a
job that finished looked identical to one that had just started.

This module defines the states, the legal transitions between them, and the
stages a caller may be told about. It holds no I/O: `video_repo` persists these
values and `pipeline` drives the transitions.

STATE MACHINE

    UPLOADED ──────► QUEUED ──────► EXTRACTING ──────► ANALYZING
       │                │               │                  │
       │                │               │                  ├──► COMPLETED
       │                ▼               ▼                  ▼
       └──────────────► FAILED ◄────────┴──────────────────┘
                          │
                          └──► QUEUED        (explicit, bounded retry)

    EXTRACTING ──┐
    ANALYZING  ──┴──► QUEUED               (crash recovery, lease expired)

  UPLOADED    file stored, ownership recorded, analysis not requested
  QUEUED      analysis accepted; the worker has not begun
  EXTRACTING  frames are being extracted from the source video
  ANALYZING   extracted frames are being sent to the model
  COMPLETED   every extracted frame reached a terminal per-frame state AND
              the job summary was persisted
  FAILED      a stage failed; `error_summary` says which, in safe terms

Deliberate properties:

* COMPLETED is reachable only from ANALYZING. Scheduling work can never
  produce it.
* FAILED is reachable from every non-terminal state, so a job cannot be
  stranded mid-pipeline.
* Both terminal states may transition back to QUEUED, but only through an
  explicit retry that increments `attempt`. COMPLETED → QUEUED is permitted so
  a finished job can be re-run deliberately; `pipeline` guards against
  concurrent duplicates separately.
* EXTRACTING and ANALYZING may also return to QUEUED, but only for crash
  recovery (Phase 4.1): a dead worker's job has to be requeueable. Every
  other caller is blocked from that path - see the note on `_ALLOWED`.
* An unknown or absent stored status is treated as UPLOADED, so records
  written before this phase keep working.
"""

from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    """Persisted job state. Inherits from `str` so it serialises directly."""

    UPLOADED = "uploaded"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """True while a worker is, or should be, doing something."""
        return self in _ACTIVE


_TERMINAL: frozenset[JobState] = frozenset({JobState.COMPLETED, JobState.FAILED})

# States that mean work is in flight. Used to refuse a duplicate run and to
# tell the frontend whether to keep polling.
_ACTIVE: frozenset[JobState] = frozenset(
    {JobState.QUEUED, JobState.EXTRACTING, JobState.ANALYZING}
)

# Legal transitions. Anything absent here is rejected by `assert_transition`,
# so an implementation bug cannot quietly move a job to a nonsensical state.
_ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.UPLOADED: frozenset({JobState.QUEUED, JobState.FAILED}),
    JobState.QUEUED: frozenset(
        {JobState.EXTRACTING, JobState.FAILED}
    ),
    # EXTRACTING/ANALYZING -> QUEUED exists for crash recovery only (Phase
    # 4.1). A worker that dies leaves its job in one of these states, and the
    # recovery sweep must be able to return it to the queue. It is NOT a path
    # a request can take: `pipeline.submit` refuses any active job, and
    # `video_repo.reclaim` - the only caller - makes the write conditional on
    # the dead worker's claim id, so a live worker's job cannot be requeued
    # out from under it.
    JobState.EXTRACTING: frozenset(
        {JobState.ANALYZING, JobState.FAILED, JobState.QUEUED}
    ),
    JobState.ANALYZING: frozenset(
        {JobState.COMPLETED, JobState.FAILED, JobState.QUEUED}
    ),
    # Terminal states re-enter the pipeline only through an explicit retry.
    JobState.COMPLETED: frozenset({JobState.QUEUED}),
    JobState.FAILED: frozenset({JobState.QUEUED}),
}


class InvalidTransition(ValueError):
    """Raised when a transition is not permitted by the state machine."""


def coerce(value: object) -> JobState:
    """Interpret a stored status value as a JobState.

    Tolerant by design: records written before Phase 4 carry "UPLOADED" or
    "ANALYZING" in upper case, and some carry no status at all. Anything
    unrecognised becomes UPLOADED rather than raising, because refusing to read
    an old record would make it permanently inaccessible.
    """
    if isinstance(value, JobState):
        return value
    if isinstance(value, str):
        try:
            return JobState(value.strip().lower())
        except ValueError:
            return JobState.UPLOADED
    return JobState.UPLOADED


def can_transition(current: object, target: object) -> bool:
    return coerce(target) in _ALLOWED[coerce(current)]


def assert_transition(current: object, target: object) -> JobState:
    """Validate a transition and return the target state.

    Raises:
        InvalidTransition: if the move is not in the state machine.
    """
    current_state = coerce(current)
    target_state = coerce(target)
    if target_state not in _ALLOWED[current_state]:
        raise InvalidTransition(
            f"cannot move a job from {current_state.value} to {target_state.value}"
        )
    return target_state


# ---------------------------------------------------------------------------
# Stages reported to the client
# ---------------------------------------------------------------------------

# Named milestones, deliberately NOT percentages. The pipeline cannot know how
# many frames a video will yield until extraction finishes, so any percentage
# before that point would be invented. Callers get a stage plus real counts.
STAGE_LABELS: dict[JobState, str] = {
    JobState.UPLOADED: "Uploaded; analysis not started",
    JobState.QUEUED: "Queued for analysis",
    JobState.EXTRACTING: "Extracting frames",
    JobState.ANALYZING: "Analyzing frames",
    JobState.COMPLETED: "Analysis complete",
    JobState.FAILED: "Analysis failed",
}


def stage_label(value: object) -> str:
    return STAGE_LABELS[coerce(value)]


# Maximum automatic attempts for one video. A retry past this is refused so a
# persistently failing video cannot be retried forever, burning Bedrock spend.
MAX_ATTEMPTS = 3


__all__ = (
    "InvalidTransition",
    "JobState",
    "MAX_ATTEMPTS",
    "STAGE_LABELS",
    "assert_transition",
    "can_transition",
    "coerce",
    "stage_label",
)
