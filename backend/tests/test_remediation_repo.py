"""Remediation persistence: ownership scoping, conditional writes, projection.

Phase 8. These exercise `remediation_repo` directly rather than through the
API, because two of its properties are defence in depth and must hold even if
a route forgets them:

* every read and every conditional write is filtered by `owner`;
* the client-facing projection never carries the owner or the storage key.

MongoDB is the in-memory fake from conftest. Nothing contacts a database.
"""

import anyio
import pytest

import remediation_repo
from remediation_repo import RemediationConflict, RemediationNotFound
from remediation_state import (
    InvalidRemediationTransition,
    RemediationState,
)

VIDEO_ID = "vid_abcd1234"


def finding_payload():
    return {
        "frame_number": 3,
        "detection_index": 0,
        "category": "competitor_logo",
        "description": "A rival mark is visible.",
        "risk_level": "high",
        "confidence": 0.91,
        "bounding_box": [100.0, 200.0, 300.0, 400.0],
        "timestamp_seconds": 3.0,
    }


def proposal_payload():
    return {
        "action": "remove_region",
        "summary": "Obscure the reported competitor_logo.",
        "rationale": "The frame analysis flagged it.",
        "model_claim": "A rival mark is visible.",
        "target_description": "A region roughly 20%-40% across.",
        "limitations": "Not a brand-policy determination.",
        "requires_approval": True,
        "confidence_gate": "pass",
        "gate_explanation": "At or above the threshold.",
    }


async def _create(owner="alice", video_id=VIDEO_ID, frame_number=3, index=0):
    return await remediation_repo.create(
        video_id=video_id,
        owner=owner,
        frame_number=frame_number,
        detection_index=index,
        finding_fingerprint="fp-" + str(frame_number) + "-" + str(index),
        finding=finding_payload(),
        proposal=proposal_payload(),
    )


def create(fake_db, **kwargs):
    return anyio.run(lambda: _create(**kwargs))


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------

def test_a_record_begins_life_proposed(fake_db):
    record = create(fake_db)
    assert record["state"] == RemediationState.PROPOSED.value
    assert record["remediation_id"].startswith(remediation_repo.ID_PREFIX)


def test_creation_records_no_decision_and_no_asset(fake_db):
    """Nothing is agreed, generated or changed at creation."""
    record = create(fake_db)
    for field in (
        "approved_by",
        "approved_at",
        "decided_by",
        "decided_at",
        "provider",
        "asset_key",
        "asset_bytes",
        "output_video_id",
        "verification_video_id",
        "verified_at",
        "error_code",
        "error_summary",
    ):
        assert record[field] is None, field
    assert record["attempt"] == 0


def test_the_owner_is_stored_and_ids_are_server_generated(fake_db):
    a = create(fake_db)
    b = create(fake_db, frame_number=4)
    assert a["owner"] == "alice"
    assert a["remediation_id"] != b["remediation_id"]


# ---------------------------------------------------------------------------
# ownership scoping
# ---------------------------------------------------------------------------

def test_a_read_is_scoped_to_the_owner(fake_db):
    record = create(fake_db, owner="alice")

    async def attempt():
        with pytest.raises(RemediationNotFound):
            await remediation_repo.get(record["remediation_id"], "bob")

    anyio.run(attempt)


def test_a_transition_is_scoped_to_the_owner(fake_db):
    """Even bypassing the route, another user cannot move the record."""
    record = create(fake_db, owner="alice")

    async def attempt():
        with pytest.raises(RemediationNotFound):
            await remediation_repo.transition(
                record["remediation_id"], "bob", RemediationState.APPROVED
            )

    anyio.run(attempt)

    after = anyio.run(
        lambda: remediation_repo.get(record["remediation_id"], "alice")
    )
    assert after["state"] == RemediationState.PROPOSED.value


def test_listing_is_scoped_to_the_owner_and_the_video(fake_db):
    create(fake_db, owner="alice", video_id=VIDEO_ID)
    create(fake_db, owner="bob", video_id=VIDEO_ID, frame_number=4)
    create(fake_db, owner="alice", video_id="vid_ffff0000", frame_number=5)

    alices = anyio.run(
        lambda: remediation_repo.list_for_video(VIDEO_ID, "alice")
    )
    assert len(alices) == 1
    assert alices[0]["owner"] == "alice"
    assert alices[0]["video_id"] == VIDEO_ID


def test_counting_is_scoped_to_the_owner_and_the_video(fake_db):
    create(fake_db, owner="alice")
    create(fake_db, owner="bob", frame_number=4)

    assert anyio.run(lambda: remediation_repo.count_for_video(VIDEO_ID, "alice")) == 1
    assert anyio.run(lambda: remediation_repo.count_for_video(VIDEO_ID, "bob")) == 1


# ---------------------------------------------------------------------------
# conditional writes
# ---------------------------------------------------------------------------

def test_a_transition_validates_the_move_before_writing(fake_db):
    record = create(fake_db)

    async def attempt():
        with pytest.raises(InvalidRemediationTransition):
            await remediation_repo.transition(
                record["remediation_id"], "alice", RemediationState.VERIFIED
            )

    anyio.run(attempt)

    after = anyio.run(
        lambda: remediation_repo.get(record["remediation_id"], "alice")
    )
    assert after["state"] == RemediationState.PROPOSED.value


def test_an_expected_state_mismatch_raises_a_conflict(fake_db):
    record = create(fake_db)

    async def attempt():
        with pytest.raises(RemediationConflict):
            await remediation_repo.transition(
                record["remediation_id"],
                "alice",
                RemediationState.PROCESSING,
                expected=RemediationState.APPROVED,
            )

    anyio.run(attempt)


def test_a_transition_persists_its_justifying_fields_atomically(fake_db):
    record = create(fake_db)

    updated = anyio.run(
        lambda: remediation_repo.transition(
            record["remediation_id"],
            "alice",
            RemediationState.APPROVED,
            fields={"approved_by": "alice", "decision_note": "looks right"},
        )
    )
    assert updated["state"] == RemediationState.APPROVED.value
    assert updated["approved_by"] == "alice"
    assert updated["decision_note"] == "looks right"
    assert updated["updated_at"] >= record["updated_at"]


def test_a_second_transition_from_a_stale_state_is_refused(fake_db):
    record = create(fake_db)
    rid = record["remediation_id"]

    anyio.run(
        lambda: remediation_repo.transition(rid, "alice", RemediationState.APPROVED)
    )

    async def attempt():
        with pytest.raises(InvalidRemediationTransition):
            await remediation_repo.transition(
                rid, "alice", RemediationState.REJECTED
            )

    anyio.run(attempt)


# ---------------------------------------------------------------------------
# open-record lookup (idempotent creation)
# ---------------------------------------------------------------------------

def test_an_open_record_for_the_same_finding_is_found(fake_db):
    create(fake_db)
    found = anyio.run(
        lambda: remediation_repo.find_open_for_finding(VIDEO_ID, "alice", 3, 0)
    )
    assert found is not None


def test_a_terminal_record_is_not_an_open_one(fake_db):
    """A rejected proposal must not block proposing again."""
    record = create(fake_db)
    anyio.run(
        lambda: remediation_repo.transition(
            record["remediation_id"], "alice", RemediationState.REJECTED
        )
    )

    found = anyio.run(
        lambda: remediation_repo.find_open_for_finding(VIDEO_ID, "alice", 3, 0)
    )
    assert found is None


def test_an_open_record_lookup_is_per_finding(fake_db):
    create(fake_db, frame_number=3, index=0)

    other_frame = anyio.run(
        lambda: remediation_repo.find_open_for_finding(VIDEO_ID, "alice", 4, 0)
    )
    other_index = anyio.run(
        lambda: remediation_repo.find_open_for_finding(VIDEO_ID, "alice", 3, 1)
    )
    other_owner = anyio.run(
        lambda: remediation_repo.find_open_for_finding(VIDEO_ID, "bob", 3, 0)
    )
    assert other_frame is None
    assert other_index is None
    assert other_owner is None


# ---------------------------------------------------------------------------
# superseding
# ---------------------------------------------------------------------------

def test_mark_superseded_records_a_safe_reason(fake_db):
    record = create(fake_db)
    updated = anyio.run(
        lambda: remediation_repo.mark_superseded(
            record["remediation_id"], "alice", "the video was re-analysed"
        )
    )
    assert updated["state"] == RemediationState.SUPERSEDED.value
    assert updated["error_code"] == "finding_changed"
    assert updated["error_summary"] == "the video was re-analysed"


def test_mark_superseded_tolerates_an_already_terminal_record(fake_db):
    """Returns None rather than raising: there is nothing to supersede.

    A caller is mid-request and must not fail because the record happened to
    be terminal already.
    """
    record = create(fake_db)
    anyio.run(
        lambda: remediation_repo.transition(
            record["remediation_id"], "alice", RemediationState.REJECTED
        )
    )

    result = anyio.run(
        lambda: remediation_repo.mark_superseded(
            record["remediation_id"], "alice", "whatever"
        )
    )
    assert result is None

    after = anyio.run(
        lambda: remediation_repo.get(record["remediation_id"], "alice")
    )
    assert after["state"] == RemediationState.REJECTED.value


# ---------------------------------------------------------------------------
# the projection
# ---------------------------------------------------------------------------

def test_the_view_omits_the_owner_and_the_storage_key(fake_db):
    record = create(fake_db)
    record["asset_key"] = "remediation/vid_abcd1234/whatever.png"

    view = remediation_repo.view(record)
    assert "owner" not in view
    assert "asset_key" not in view
    # Presence and shape only.
    assert view["has_asset"] is True


def test_the_view_reports_no_asset_when_there_is_none(fake_db):
    view = remediation_repo.view(create(fake_db))
    assert view["has_asset"] is False


def test_the_view_answers_the_compliance_question_itself(fake_db):
    """So no client has to re-derive the one rule that matters."""
    record = create(fake_db)
    view = remediation_repo.view(record)
    assert view["is_compliance_claim"] is False
    assert view["source_video_modified"] is False

    record["state"] = RemediationState.VERIFIED.value
    assert remediation_repo.view(record)["is_compliance_claim"] is True


def test_the_view_serialises_timestamps_as_iso_strings(fake_db):
    view = remediation_repo.view(create(fake_db))
    assert isinstance(view["created_at"], str)
    assert isinstance(view["updated_at"], str)
    assert view["approved_at"] is None


def test_the_view_carries_a_human_stage_label(fake_db):
    view = remediation_repo.view(create(fake_db))
    assert view["stage"] == "Proposed; awaiting your review"
    assert view["is_terminal"] is False
    assert view["is_active"] is False


# ---------------------------------------------------------------------------
# indexes
# ---------------------------------------------------------------------------

def test_ensure_indexes_declares_a_unique_id(fake_db):
    anyio.run(remediation_repo.ensure_indexes)
    declared = fake_db[remediation_repo.COLLECTION].indexes
    assert ("remediation_id", True) in declared
    assert ("owner", False) in declared
    assert ("video_id", False) in declared


def test_ensure_indexes_is_idempotent(fake_db):
    anyio.run(remediation_repo.ensure_indexes)
    anyio.run(remediation_repo.ensure_indexes)
    declared = fake_db[remediation_repo.COLLECTION].indexes
    assert declared.count(("remediation_id", True)) == 1


def test_database_initialization_creates_them_without_touching_accounts(fake_db):
    """Phase 8 is additive: no existing document is read or written."""
    import db_init

    users = fake_db["users"]
    users.insert_raw({"username": "alice", "password_hash": "x"})

    results = anyio.run(lambda: db_init.initialize_database(strict=False))

    assert results["remediation_indexes"] is True
    assert len(users.docs) == 1
    assert users.docs[0]["username"] == "alice"
    assert ("remediation_id", True) in fake_db[
        remediation_repo.COLLECTION
    ].indexes
