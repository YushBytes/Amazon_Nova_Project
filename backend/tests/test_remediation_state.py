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


def test_verified_is_not_reachable_in_this_build():
    """No code path can claim compliance, because none can establish it.

    Nothing generates an asset, nothing applies one to a video, and nothing
    re-analyses an output - so `verified` has no precondition this build can
    satisfy.
    """
    assert RemediationState.VERIFIED in UNREACHABLE_IN_THIS_BUILD
    assert RemediationState.APPLIED in UNREACHABLE_IN_THIS_BUILD
    assert RemediationState.ASSET_READY in UNREACHABLE_IN_THIS_BUILD


def test_the_reachable_set_is_the_review_workflow():
    assert REACHABLE_IN_THIS_BUILD == {
        RemediationState.PROPOSED,
        RemediationState.APPROVED,
        RemediationState.REJECTED,
        RemediationState.CANCELLED,
        RemediationState.SUPERSEDED,
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
        (RemediationState.ASSET_READY, RemediationState.APPLIED),
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
        # Generation cannot be skipped.
        (RemediationState.APPROVED, RemediationState.ASSET_READY),
        (RemediationState.APPROVED, RemediationState.APPLIED),
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


def test_applied_requires_an_asset():
    """APPLIED is only reachable from ASSET_READY."""
    sources = [
        s for s in RemediationState if RemediationState.APPLIED in allowed_targets(s)
    ]
    assert sources == [RemediationState.ASSET_READY]


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


def test_an_asset_cannot_be_superseded():
    """Once an object exists, losing track of it would orphan it."""
    assert not can_transition(
        RemediationState.ASSET_READY, RemediationState.SUPERSEDED
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
