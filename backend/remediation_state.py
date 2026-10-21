"""Remediation lifecycle: the states, and which moves between them are legal.

Phase 8. Deliberately a separate module from `remediation_repo` (which
persists) and `remediation` (which builds proposals), exactly as
`job_state.py` is separate from `video_repo.py`. It holds no I/O.

WHY THESE STATES ARE SEPARATE
-----------------------------
The defect this module exists to prevent is collapsing distinct facts into one
"fixed" flag. The deleted `audit_manager.remediate_frame` returned its input
unchanged while its caller recorded `remediated=True`; the frontend's old
detail view showed "AUDIT PASSED" for every video. Both were the same mistake:
one boolean standing in for a chain of claims, each of which can fail
independently.

Each of the following is a different statement about the world, and no two may
be inferred from one another:

  PROPOSED            A change has been described. Nobody has agreed to it,
                      nothing has been generated, nothing has changed.
  APPROVED            An authenticated owner agreed the change should be
                      attempted. Still nothing has been generated.
  PROCESSING          A provider call is in flight. It may fail.
  ASSET_READY         A validated asset exists in storage. The source video is
                      UNCHANGED - this is a candidate, not a correction.
  RECONSTRUCTING      A worker holds this remediation and is building an
                      output video. Phase 9. Carries the claim and lease, so
                      this is the state a crashed worker leaves behind and the
                      one the recovery sweep reclaims.
  APPLIED             The asset was incorporated into an output video. The
                      result's compliance is UNKNOWN.
  VERIFYING           A fresh analysis of that output is running.
  VERIFIED            A completed analysis of the output persisted results
                      that support the claim. ONLY this state may be
                      described as verified.

Terminal and recovery states:

  REJECTED            The owner declined the proposal.
  CANCELLED           Withdrawn before anything consequential happened.
  FAILED              A stage failed; `error_summary` says which, safely.
  SUPERSEDED          The underlying finding changed (the video was
                      re-analysed), so the proposal no longer describes
                      anything real. Not a failure - the proposal was valid
                      when made.
  VERIFICATION_FAILED A re-analysis ran and did NOT support the claim. This is
                      emphatically not "clean": see `is_compliance_claim`.

STATE MACHINE

    PROPOSED ─► APPROVED ─► PROCESSING ─► ASSET_READY ─► APPLIED ─► VERIFYING
       │           │            │                                      │
       ├► REJECTED │            ├► FAILED                              ├► VERIFIED
       ├► CANCELLED├► CANCELLED │                                      └► VERIFICATION_FAILED
       └► SUPERSEDED└► SUPERSEDED└► SUPERSEDED

WHAT THIS BUILD CAN ACTUALLY REACH
----------------------------------
No image provider is implemented (see `image_provider.py` for why), and video
reconstruction is a separate unresolved decision (CLAUDE.md §9 q3). So the
reachable set today is:

    PROPOSED, APPROVED, REJECTED, CANCELLED, SUPERSEDED

PROCESSING onwards are defined and validated here but **no code path can drive
a record into them**, and `test_remediation_state.py` asserts exactly that.
FAILED is in that set too: with no provider there is nothing that can fail,
and a route that answers "generation is not configured" by marking the record
FAILED would be making a terminal claim about the remediation when the only
thing missing is a deployment capability. Such a request leaves the state
untouched and returns 503, so the proposal stays executable if a provider is
ever configured.

The unreachable states are declared anyway because the contract is what stops
a later phase from adding an `applied` boolean beside these states instead of
extending them - and because `verified` needs its precondition written down
before anything can be tempted to claim it.
"""

from __future__ import annotations

from enum import Enum


class RemediationState(str, Enum):
    """Persisted remediation state. Inherits from `str` so it serialises."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    PROCESSING = "processing"
    ASSET_READY = "asset_ready"
    RECONSTRUCTING = "reconstructing"
    APPLIED = "applied"
    VERIFYING = "verifying"
    VERIFIED = "verified"

    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    VERIFICATION_FAILED = "verification_failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """True while something is, or should be, running."""
        return self in _ACTIVE

    @property
    def is_compliance_claim(self) -> bool:
        """Whether this state asserts the output is compliant.

        Exactly one state does. In particular VERIFICATION_FAILED does not,
        and neither does APPLIED - a modified video whose compliance has not
        been re-established says nothing about compliance.
        """
        return self is RemediationState.VERIFIED


_TERMINAL: frozenset[RemediationState] = frozenset(
    {
        RemediationState.VERIFIED,
        RemediationState.REJECTED,
        RemediationState.CANCELLED,
        RemediationState.SUPERSEDED,
        RemediationState.FAILED,
        RemediationState.VERIFICATION_FAILED,
    }
)

_ACTIVE: frozenset[RemediationState] = frozenset(
    {
        RemediationState.PROCESSING,
        RemediationState.RECONSTRUCTING,
        RemediationState.VERIFYING,
    }
)

# States that no code path in this build can produce. Enforced by a test, so
# that adding a provider or a reconstruction stage also means deliberately
# removing the name from here.
# Phase 9 made the whole chain reachable through the SUPPLIED-ASSET path, so
# only the provider state is left: PROCESSING means "a provider call is in
# flight", and `image_provider` still ships no provider that can make one.
UNREACHABLE_IN_THIS_BUILD: frozenset[RemediationState] = frozenset(
    {RemediationState.PROCESSING}
)

# What this build CAN reach, named explicitly so the two sets must stay
# complementary - a test asserts they partition the enum.
#
# Reaching VERIFIED still requires a completed analysis OF THE OUTPUT VIDEO
# whose persisted results support the claim. Being reachable is not a claim
# that it is easy to reach, and `verification.py` is the only thing that may
# take that transition.
REACHABLE_IN_THIS_BUILD: frozenset[RemediationState] = frozenset(
    {
        RemediationState.PROPOSED,
        RemediationState.APPROVED,
        RemediationState.ASSET_READY,
        RemediationState.RECONSTRUCTING,
        RemediationState.APPLIED,
        RemediationState.VERIFYING,
        RemediationState.VERIFIED,
        RemediationState.VERIFICATION_FAILED,
        RemediationState.REJECTED,
        RemediationState.CANCELLED,
        RemediationState.SUPERSEDED,
        RemediationState.FAILED,
    }
)

# Legal transitions. Anything absent is rejected by `assert_transition`, so an
# implementation bug cannot quietly move a record to a nonsensical state.
#
# SUPERSEDED is reachable from every pre-asset state because re-analysis can
# invalidate the underlying finding at any point before generation. It is NOT
# reachable once an asset exists: the asset is real whatever the finding now
# says, and losing track of a stored object would orphan it.
_ALLOWED: dict[RemediationState, frozenset[RemediationState]] = {
    RemediationState.PROPOSED: frozenset(
        {
            RemediationState.APPROVED,
            RemediationState.REJECTED,
            RemediationState.CANCELLED,
            RemediationState.SUPERSEDED,
        }
    ),
    RemediationState.APPROVED: frozenset(
        {
            RemediationState.PROCESSING,
            # Phase 9. An asset supplied directly by the owner skips
            # PROCESSING: that state means "a provider call is in flight", and
            # there is no call. The asset is still validated, content-sniffed
            # and stored before this transition is taken, so ASSET_READY keeps
            # its meaning - a validated asset exists.
            RemediationState.ASSET_READY,
            RemediationState.CANCELLED,
            RemediationState.SUPERSEDED,
            # An approved proposal can fail before a provider is reached -
            # for instance when the configured provider is unavailable.
            RemediationState.FAILED,
        }
    ),
    RemediationState.PROCESSING: frozenset(
        {
            RemediationState.ASSET_READY,
            RemediationState.FAILED,
            RemediationState.SUPERSEDED,
        }
    ),
    RemediationState.ASSET_READY: frozenset(
        {
            # Phase 9: reconstruction is claimed by a worker, so it gets its
            # own state. APPLIED is no longer reachable directly from here -
            # an output video cannot appear without a run that built it.
            RemediationState.RECONSTRUCTING,
            # The owner can still withdraw before anything is built. The
            # stored asset is retained rather than deleted, so the record of
            # what was supplied stays auditable.
            RemediationState.CANCELLED,
            # Phase 9. Phase 8 forbade this, reasoning that "losing track of a
            # stored object would orphan it". That reasoning was about an
            # OUTPUT video; it does not apply to an asset, because
            # `mark_superseded` leaves `asset_key` on the record, so the
            # object stays named and auditable. Allowing it matters: without
            # it, a record whose finding changed after an asset was attached
            # would keep reporting "ready" while every execution attempt was
            # refused - the state would be a lie about why nothing happens.
            RemediationState.SUPERSEDED,
            RemediationState.FAILED,
        }
    ),
    RemediationState.RECONSTRUCTING: frozenset(
        {
            RemediationState.APPLIED,
            RemediationState.FAILED,
            # Crash recovery only: a worker that died leaves the record here,
            # and the sweep must be able to return it to the queue. Not a path
            # a request can take - `remediation_repo.reclaim` makes the write
            # conditional on the dead worker's claim id, exactly as
            # `video_repo.reclaim` does.
            RemediationState.ASSET_READY,
        }
    ),
    RemediationState.APPLIED: frozenset(
        {RemediationState.VERIFYING, RemediationState.FAILED}
    ),
    RemediationState.VERIFYING: frozenset(
        {
            RemediationState.VERIFIED,
            RemediationState.VERIFICATION_FAILED,
            RemediationState.FAILED,
        }
    ),
    # Terminal. A new attempt is a NEW proposal rather than a resurrection of
    # this one, so the audit trail of what was agreed stays intact.
    RemediationState.VERIFIED: frozenset(),
    RemediationState.REJECTED: frozenset(),
    RemediationState.CANCELLED: frozenset(),
    RemediationState.SUPERSEDED: frozenset(),
    RemediationState.FAILED: frozenset(),
    RemediationState.VERIFICATION_FAILED: frozenset(),
}


class InvalidRemediationTransition(ValueError):
    """Raised when a transition is not permitted by the state machine."""


def coerce(value: object) -> RemediationState:
    """Interpret a stored value as a RemediationState.

    Unlike `job_state.coerce`, an unrecognised value RAISES rather than
    defaulting. There are no legacy remediation records to be tolerant of -
    this collection is new in Phase 8 - and silently reading an unknown state
    as `proposed` would downgrade a record that might represent real work.
    """
    if isinstance(value, RemediationState):
        return value
    if isinstance(value, str):
        try:
            return RemediationState(value.strip().lower())
        except ValueError as exc:
            raise InvalidRemediationTransition(
                f"unknown remediation state: {value!r}"
            ) from exc
    raise InvalidRemediationTransition(
        f"unknown remediation state of type {type(value).__name__}"
    )


def can_transition(current: object, target: object) -> bool:
    return coerce(target) in _ALLOWED[coerce(current)]


def assert_transition(current: object, target: object) -> RemediationState:
    """Validate a transition and return the target state.

    Raises:
        InvalidRemediationTransition: if the move is not in the machine.
    """
    current_state = coerce(current)
    target_state = coerce(target)
    if target_state not in _ALLOWED[current_state]:
        raise InvalidRemediationTransition(
            f"cannot move a remediation from {current_state.value} to "
            f"{target_state.value}"
        )
    return target_state


def allowed_targets(current: object) -> frozenset[RemediationState]:
    """The states reachable from `current` in one move."""
    return _ALLOWED[coerce(current)]


# ---------------------------------------------------------------------------
# What a client is told
# ---------------------------------------------------------------------------

# Phrasing is part of the contract. Every label states what HAS happened and
# none implies compliance except the verified one, which is the only state
# whose precondition is a completed re-analysis.
STAGE_LABELS: dict[RemediationState, str] = {
    RemediationState.PROPOSED: "Proposed; awaiting your review",
    RemediationState.APPROVED: "Approved; no asset generated yet",
    RemediationState.PROCESSING: "Generating a candidate asset",
    RemediationState.ASSET_READY: "Replacement asset ready; video unchanged",
    RemediationState.RECONSTRUCTING: "Building an output video; original unchanged",
    RemediationState.APPLIED: "Asset applied to an output video; not yet re-analysed",
    RemediationState.VERIFYING: "Re-analysing the output video",
    RemediationState.VERIFIED: "Re-analysis completed and found no violation here",
    RemediationState.REJECTED: "Rejected by you",
    RemediationState.CANCELLED: "Cancelled",
    RemediationState.SUPERSEDED: "Superseded; the video was re-analysed and this finding changed",
    RemediationState.FAILED: "Failed",
    RemediationState.VERIFICATION_FAILED: "Re-analysis did NOT confirm this was resolved",
}


def stage_label(value: object) -> str:
    return STAGE_LABELS[coerce(value)]


def describes_modified_source(value: object) -> bool:
    """Whether this state means the user's video was altered.

    The original source object is never overwritten by any phase, so this is
    about an OUTPUT video existing, not about the upload changing. Nothing in
    this build can make it true.
    """
    return coerce(value) in {
        RemediationState.APPLIED,
        RemediationState.VERIFYING,
        RemediationState.VERIFIED,
        RemediationState.VERIFICATION_FAILED,
    }


__all__ = (
    "InvalidRemediationTransition",
    "RemediationState",
    "STAGE_LABELS",
    "REACHABLE_IN_THIS_BUILD",
    "UNREACHABLE_IN_THIS_BUILD",
    "allowed_targets",
    "assert_transition",
    "can_transition",
    "coerce",
    "describes_modified_source",
    "stage_label",
)
