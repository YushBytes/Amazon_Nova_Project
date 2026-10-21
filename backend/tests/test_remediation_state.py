"""The remediation state machine, and the claims each state is allowed to make.

Phase 8. These are the tests that stop Phase 8's whole point being undone: the
states exist so that "a change was proposed", "an owner agreed", "an image was
generated", "a video was modified" and "the result is compliant" cannot be
collapsed into one flag. Each assertion below pins one of those separations.
"""

import pytest

from remediation_state import (
    REACHABLE_IN_THIS_BUILD,
    STAGE_LABELS,
    UNREACHABLE_IN_THIS_BUILD,
    InvalidRemediationTransition,
    RemediationState,
    allowed_targets,
    assert_transition,
    can_transition,
    coerce,
    describes_modified_source,
    stage_label,
)


# ---------------------------------------------------------------------------
# the separations that matter
# ---------------------------------------------------------------------------

def test_only_verified_is_a_compliance_claim():
    """Exactly one state may be described as compliant.

    The direct regression test for the two historical defects: a remediation
    function that returned its input while the caller recorded success, and a
    detail view that printed "AUDIT PASSED" unconditionally.
    """
    claiming = [s for s in RemediationState if s.is_compliance_claim]
    assert claiming == [RemediationState.VERIFIED]


def test_applied_is_not_a_compliance_claim():
    """A modified video says nothing about compliance until re-analysed."""
    assert RemediationState.APPLIED.is_compliance_claim is False


def test_verification_failed_is_not_a_compliance_claim():
    """A failed verification is emphatically not clean."""
    assert RemediationState.VERIFICATION_FAILED.is_compliance_claim is False
    assert RemediationState.VERIFICATION_FAILED.is_terminal is True


def test_asset_ready_does_not_mean_the_video_changed():
    """A generated asset is a candidate, not a correction."""
    assert describes_modified_source(RemediationState.ASSET_READY) is False
    assert describes_modified_source(RemediationState.APPLIED) is True


def test_approval_does_not_mean_generation_happened():
    assert describes_modified_source(RemediationState.APPROVED) is False
    assert RemediationState.APPROVED.is_compliance_claim is False


def test_proposed_is_not_approved():
    """The two are distinct states, and proposed is not terminal."""
    assert RemediationState.PROPOSED is not RemediationState.APPROVED
    assert RemediationState.PROPOSED.is_terminal is False
    assert RemediationState.PROPOSED.is_active is False


def test_no_label_claims_compliance_except_the_verified_one():
    """Wording is part of the contract, not decoration."""
    for state, label in STAGE_LABELS.items():
        if state is RemediationState.VERIFIED:
            continue
        lowered = label.lower()
        assert "compliant" not in lowered, state
        assert "passed" not in lowered, state
        assert "fixed" not in lowered, state


def test_every_state_has_a_label():
    for state in RemediationState:
        assert stage_label(state)


# ---------------------------------------------------------------------------
# what this build can reach
# ---------------------------------------------------------------------------

def test_reachable_and_unreachable_sets_partition_the_enum():
    """The two sets must stay complementary.

    So that adding a provider means deliberately moving a name out of the
    unreachable set, rather than quietly leaving a stale declaration behind.
    """
    assert REACHABLE_IN_THIS_BUILD | UNREACHABLE_IN_THIS_BUILD == set(
        RemediationState
    )
    assert not (REACHABLE_IN_THIS_BUILD & UNREACHABLE_IN_THIS_BUILD)


def test_only_the_provider_state_is_unreachable_now():
    """Phase 9 made the chain reachable through the SUPPLIED-ASSET path.

    Phase 8 asserted the opposite, and that assertion was correct then: no
    asset could be obtained, so nothing downstream of it could happen. Phase 9
    lets an owner supply the asset themselves, so ASSET_READY onwards are now
    reachable.

    PROCESSING is the one that is not, and it is the right one to be left:
    it means "a provider call is in flight", and `image_provider` still ships
    no provider that can make one.
    """
    assert UNREACHABLE_IN_THIS_BUILD == {RemediationState.PROCESSING}
    assert RemediationState.ASSET_READY in REACHABLE_IN_THIS_BUILD
    assert RemediationState.VERIFIED in REACHABLE_IN_THIS_BUILD


def test_verified_still_requires_a_completed_re_analysis():
    """Reachable is not the same as easily reached.

    `verified` remains reachable ONLY from `verifying`, so the state machine
    still forbids claiming compliance without an analysis of the output
    having run. `verification.py` is the only module that takes that
    transition, and only from persisted results.
    """
    sources = [
        s for s in RemediationState if RemediationState.VERIFIED in allowed_targets(s)
    ]
    assert sources == [RemediationState.VERIFYING]


def test_the_reachable_set_is_the_whole_workflow_except_generation():
    assert REACHABLE_IN_THIS_BUILD == {
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


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "current,target",
    [
        (RemediationState.PROPOSED, RemediationState.APPROVED),
        (RemediationState.PROPOSED, RemediationState.REJECTED),
        (RemediationState.PROPOSED, RemediationState.CANCELLED),
        (RemediationState.PROPOSED, RemediationState.SUPERSEDED),
        (RemediationState.APPROVED, RemediationState.PROCESSING),
        (RemediationState.APPROVED, RemediationState.CANCELLED),
        (RemediationState.PROCESSING, RemediationState.ASSET_READY),
        (RemediationState.PROCESSING, RemediationState.FAILED),
        (RemediationState.APPROVED, RemediationState.ASSET_READY),
        (RemediationState.ASSET_READY, RemediationState.RECONSTRUCTING),
        (RemediationState.ASSET_READY, RemediationState.CANCELLED),
        (RemediationState.RECONSTRUCTING, RemediationState.APPLIED),
        (RemediationState.RECONSTRUCTING, RemediationState.FAILED),
        (RemediationState.RECONSTRUCTING, RemediationState.ASSET_READY),
        (RemediationState.APPLIED, RemediationState.VERIFYING),
        (RemediationState.VERIFYING, RemediationState.VERIFIED),
        (RemediationState.VERIFYING, RemediationState.VERIFICATION_FAILED),
    ],
)
def test_legal_transitions_are_allowed(current, target):
    assert can_transition(current, target)
    assert assert_transition(current, target) is target


@pytest.mark.parametrize(
    "current,target",
    [
        # Approval cannot be skipped.
        (RemediationState.PROPOSED, RemediationState.PROCESSING),
        (RemediationState.PROPOSED, RemediationState.ASSET_READY),
        (RemediationState.PROPOSED, RemediationState.APPLIED),
        # Compliance cannot be claimed without a verification stage.
        (RemediationState.PROPOSED, RemediationState.VERIFIED),
        (RemediationState.APPROVED, RemediationState.VERIFIED),
        (RemediationState.ASSET_READY, RemediationState.VERIFIED),
        (RemediationState.APPLIED, RemediationState.VERIFIED),
        # An output video cannot appear without a run that built it.
        (RemediationState.APPROVED, RemediationState.APPLIED),
        (RemediationState.ASSET_READY, RemediationState.APPLIED),
        (RemediationState.ASSET_READY, RemediationState.VERIFYING),
        # An asset cannot appear without a provider call.
        (RemediationState.PROPOSED, RemediationState.VERIFYING),
    ],
)
def test_illegal_transitions_are_rejected(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidRemediationTransition):
        assert_transition(current, target)


def test_verified_requires_a_verifying_stage():
    """VERIFYING is the ONLY state from which VERIFIED is reachable.

    That is what makes "verified" mean "a re-analysis ran", rather than
    "somebody decided it was fine".
    """
    sources = [
        s for s in RemediationState if RemediationState.VERIFIED in allowed_targets(s)
    ]
    assert sources == [RemediationState.VERIFYING]


def test_applied_requires_a_reconstruction_run():
    """APPLIED is only reachable from RECONSTRUCTING.

    Phase 8 had it reachable from ASSET_READY, when there was no run in
    between. Phase 9 added that run as its own claimed, leased state, so an
    output video cannot be recorded without a run having built it.
    """
    sources = [
        s for s in RemediationState if RemediationState.APPLIED in allowed_targets(s)
    ]
    assert sources == [RemediationState.RECONSTRUCTING]


def test_reconstruction_requires_a_ready_asset():
    """Nothing can be built before a validated asset exists."""
    sources = [
        s
        for s in RemediationState
        if RemediationState.RECONSTRUCTING in allowed_targets(s)
    ]
    assert sources == [RemediationState.ASSET_READY]


def test_an_asset_can_be_reached_without_a_provider_call():
    """The supplied-asset path, which is what Phase 9 is built on.

    APPROVED -> ASSET_READY skips PROCESSING because there is no provider call
    in flight. The asset is still validated and stored before the transition,
    so ASSET_READY keeps its meaning.
    """
    assert can_transition(RemediationState.APPROVED, RemediationState.ASSET_READY)
    assert can_transition(RemediationState.APPROVED, RemediationState.PROCESSING)


def test_processing_requires_approval():
    """A provider can only be called for an approved remediation."""
    sources = [
        s
        for s in RemediationState
        if RemediationState.PROCESSING in allowed_targets(s)
    ]
    assert sources == [RemediationState.APPROVED]


@pytest.mark.parametrize(
    "terminal",
    [
        RemediationState.VERIFIED,
        RemediationState.REJECTED,
        RemediationState.CANCELLED,
        RemediationState.SUPERSEDED,
        RemediationState.FAILED,
        RemediationState.VERIFICATION_FAILED,
    ],
)
def test_terminal_states_have_no_exit(terminal):
    """A new attempt is a new proposal, so the decision history stays intact."""
    assert terminal.is_terminal
    assert allowed_targets(terminal) == frozenset()


def test_a_stale_asset_ready_record_can_be_superseded():
    """Phase 9 allowed this, and Phase 8's reasoning did not actually apply.

    Phase 8 forbade it to avoid orphaning a stored object. But
    `mark_superseded` leaves `asset_key` on the record, so the asset stays
    named and auditable - nothing is lost. What the prohibition DID cause was
    worse: a record whose finding changed after an asset was attached kept
    reporting "ready" while every execution attempt was refused, so its state
    was a lie about why nothing was happening.
    """
    assert can_transition(
        RemediationState.ASSET_READY, RemediationState.SUPERSEDED
    )


def test_work_in_flight_cannot_be_superseded():
    """A worker holds a RECONSTRUCTING record; a sweep must not yank it.

    This is the prohibition that does still apply: the only way out of
    RECONSTRUCTING is the worker finishing, failing, or its lease lapsing and
    `reclaim` returning it - all of which are conditional on its claim id.
    """
    assert not can_transition(
        RemediationState.RECONSTRUCTING, RemediationState.SUPERSEDED
    )
    assert not can_transition(
        RemediationState.APPLIED, RemediationState.SUPERSEDED
    )


def test_no_state_transitions_to_itself():
    """Self-transitions are handled as idempotent no-ops by the routes.

    Allowing one here would make a repeated approval look like a fresh
    decision and overwrite `approved_at`.
    """
    for state in RemediationState:
        assert state not in allowed_targets(state), state


def test_every_state_appears_in_the_transition_table():
    for state in RemediationState:
        allowed_targets(state)  # raises KeyError if absent


# ---------------------------------------------------------------------------
# coercion
# ---------------------------------------------------------------------------

def test_coerce_accepts_stored_values():
    for state in RemediationState:
        assert coerce(state.value) is state
        assert coerce(state) is state


def test_coerce_normalises_case_and_whitespace():
    assert coerce("  APPROVED ") is RemediationState.APPROVED


@pytest.mark.parametrize("bad", ["", "fixed", "done", None, 7, object()])
def test_coerce_rejects_an_unknown_value(bad):
    """Unlike job_state, an unknown value raises rather than defaulting.

    There are no legacy remediation records to be tolerant of, and reading an
    unknown state as `proposed` would downgrade a record that might represent
    real work.
    """
    with pytest.raises(InvalidRemediationTransition):
        coerce(bad)
