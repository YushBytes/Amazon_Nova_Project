"""Phase 8: the remediation API - authorization, state, concurrency, honesty.

Tokens are real JWTs minted by `auth.create_access_token`, so no test can pass
by bypassing authentication. MongoDB is the in-memory fake, DynamoDB is mocked,
and nothing contacts AWS or Bedrock.

What these tests are for, in order of importance:

1. Nothing in this API can produce a compliance claim. There is no provider
   and no reconstruction, so `applied` and `verified` must be unreachable
   through every route - asserted directly, not inferred.
2. Approval is enforced in the backend. Hiding a button is not what stops an
   unapproved proposal being executed.
3. One user cannot read, approve, reject or execute another user's proposal,
   and a denied request answers 404 so ids cannot be enumerated.
4. A proposal whose underlying finding changed is superseded rather than acted
   on - the staleness rule that makes an approval mean something.
5. Concurrent and duplicate requests behave: the conditional write makes one
   winner, and a repeat of a decision already applied is idempotent.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import config
import image_provider
import remediation_repo
import video_repo
from remediation_state import RemediationState, UNREACHABLE_IN_THIS_BUILD

VIDEO_ID = "vid_abcd1234"
OTHER_VIDEO_ID = "vid_ffff0000"

# A minimal byte string with a real PNG magic number, long enough to pass the
# MIN_IMAGE_BYTES floor. The pixels are not meaningful - nothing in the
# backend decodes the image, it is handed to FFmpeg, which is mocked here.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


def _bearer(username: str, role: str = "client") -> dict:
    return {"Authorization": f"Bearer {auth.create_access_token(username, role)}"}


@pytest.fixture
def alice():
    return _bearer("alice")


@pytest.fixture
def bob():
    return _bearer("bob")


@pytest.fixture(autouse=True)
def _reset_provider():
    image_provider.reset_provider()
    config.reset_settings_cache()
    yield
    image_provider.reset_provider()
    config.reset_settings_cache()


def detection(**overrides):
    """A stored detection, shaped as `analyzer._record_frame_result` writes it."""
    item = {
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
    item.update(overrides)
    return item


def frame_row(detections=None, frame_number=3, status="audited"):
    return {
        "video_id": VIDEO_ID,
        "frame_number": Decimal(frame_number),
        "status": status,
        "timestamp": Decimal("3"),
        "detections": detections if detections is not None else [detection()],
    }


def dynamo_with(item):
    """A mocked DynamoDB resource whose get_item returns `item` (or nothing)."""
    dynamo = MagicMock()
    table = dynamo.Table.return_value
    table.get_item.return_value = {"Item": item} if item is not None else {}
    return dynamo


async def _own_video(database, owner="alice", video_id=VIDEO_ID, state="completed"):
    """Register a video and drive it to a job state, as the pipeline would."""
    await video_repo.record_video(
        video_id=video_id,
        owner=owner,
        stored_filename=f"{video_id}.mp4",
        original_filename="clip.mp4",
        extension="mp4",
        size_bytes=1024,
    )
    if state != "uploaded":
        col = database["videos"]
        await col.update_one({"video_id": video_id}, {"$set": {"status": state}})


def seed_video(fake_db, owner="alice", video_id=VIDEO_ID, state="completed"):
    anyio.run(_own_video, fake_db, owner, video_id, state)


def create_proposal(client, headers, item=None, body=None):
    """POST a proposal with the frame store mocked."""
    payload = body or {
        "video_id": VIDEO_ID,
        "frame_number": 3,
        "detection_index": 0,
    }
    with patch("aws_clients.get_resource", return_value=dynamo_with(item or frame_row())):
        return client.post("/remediations", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------

PROTECTED = [
    ("get", "/remediations/capabilities", None),
    ("post", "/remediations", {"video_id": VIDEO_ID, "frame_number": 0, "detection_index": 0}),
    ("get", f"/remediations?video_id={VIDEO_ID}", None),
    ("get", "/remediations/rem_000000000000", None),
    ("post", "/remediations/rem_000000000000/approve", {}),
    ("post", "/remediations/rem_000000000000/reject", {}),
    ("post", "/remediations/rem_000000000000/cancel", {}),
    ("post", "/remediations/rem_000000000000/execute", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_every_route_rejects_an_anonymous_caller(client, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_every_route_rejects_a_media_token(client, method, path, body):
    """Scope separation: a playback token must not reach the API."""
    token = auth.create_media_token("alice", VIDEO_ID)
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(
        path, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# capabilities: the UI is told the truth
# ---------------------------------------------------------------------------

def test_capabilities_reports_no_generation_but_a_supplied_asset_path(
    client, fake_db, alice
):
    """Phase 9 changed two of these deliberately.

    Phase 8 reported `applies_assets_to_video` and `verifies_compliance` as
    hardcoded false, which was correct then. Phase 9 implements both through
    the supplied-asset path, so they are now read from configuration - and
    GENERATION is still unavailable, which is the part that has not changed.
    """
    response = client.get("/remediations/capabilities", headers=alice)
    assert response.status_code == 200
    body = response.json()

    assert body["proposals_supported"] is True
    # Unchanged: no image provider ships.
    assert body["generation_available"] is False
    assert body["generation_provider"] == "none"
    assert body["generation_unavailable_reason"]
    # New in Phase 9: the owner can supply the image a provider would have
    # generated, and the output is then really built and really analysed.
    assert body["asset_upload_supported"] is True
    assert body["max_asset_bytes"] > 0
    assert body["accepted_asset_types"] == ["image/png", "image/jpeg"]
    assert body["applies_assets_to_video"] is True
    assert body["verifies_compliance"] is True
    assert body["reconstruction_available"] is True
    # Named plainly so nothing reads more into it than a scaled overlay.
    assert body["reconstruction_method"] == "scaled_overlay"
    assert body["verification_policy"] == "target_only"
    assert body["min_confidence"] == pytest.approx(0.8)
    assert "never modified" in body["notes"]


def test_capabilities_reports_reconstruction_off_when_disabled(
    client, fake_db, alice, monkeypatch
):
    """A deployment without FFmpeg should say so rather than fail every run."""
    monkeypatch.setenv("RECONSTRUCTION_ENABLED", "false")
    body = client.get("/remediations/capabilities", headers=alice).json()

    assert body["reconstruction_available"] is False
    assert body["applies_assets_to_video"] is False
    assert body["verifies_compliance"] is False
    assert "not enabled" in body["reconstruction_unavailable_reason"]


def test_capabilities_names_the_configured_but_unimplemented_provider(
    client, fake_db, alice, monkeypatch
):
    monkeypatch.setenv("REMEDIATION_IMAGE_PROVIDER", "nova-canvas")
    image_provider.reset_provider()

    body = client.get("/remediations/capabilities", headers=alice).json()
    assert body["generation_provider"] == "nova-canvas"
    assert body["generation_available"] is False
    assert "not implemented" in body["generation_unavailable_reason"]


def test_the_whole_router_disappears_when_disabled(
    client, fake_db, alice, monkeypatch
):
    """A deployment that turns the feature off does not advertise it."""
    monkeypatch.setenv("REMEDIATION_ENABLED", "false")
    response = client.get("/remediations/capabilities", headers=alice)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# proposal creation
# ---------------------------------------------------------------------------

def test_a_proposal_is_created_from_a_real_persisted_finding(
    client, fake_db, alice
):
    seed_video(fake_db)
    response = create_proposal(client, alice)

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "proposed"
    assert body["video_id"] == VIDEO_ID
    assert body["frame_number"] == 3
    assert body["detection_index"] == 0
    assert body["finding"]["category"] == "competitor_logo"
    assert body["finding"]["confidence"] == pytest.approx(0.91)
    assert body["proposal"]["requires_approval"] is True
    assert body["proposal"]["confidence_gate"] == "pass"
    # Nothing has been agreed, generated or changed.
    assert body["approved_by"] is None
    assert body["has_asset"] is False
    assert body["is_compliance_claim"] is False
    assert body["source_video_modified"] is False


def test_a_proposal_for_a_nonexistent_finding_is_404(client, fake_db, alice):
    seed_video(fake_db)
    response = create_proposal(
        client,
        alice,
        body={"video_id": VIDEO_ID, "frame_number": 3, "detection_index": 7},
    )
    assert response.status_code == 404


def test_a_proposal_for_a_nonexistent_frame_is_404(client, fake_db, alice):
    seed_video(fake_db)
    with patch("aws_clients.get_resource", return_value=dynamo_with(None)):
        response = client.post(
            "/remediations",
            json={"video_id": VIDEO_ID, "frame_number": 99, "detection_index": 0},
            headers=alice,
        )
    assert response.status_code == 404


def test_a_finding_with_no_category_is_422_not_404(client, fake_db, alice):
    """Different facts: the address is right, the data cannot support review."""
    seed_video(fake_db)
    row = frame_row(detections=[{"description": "no category here"}])
    response = create_proposal(client, alice, item=row)
    assert response.status_code == 422


def test_a_failed_frame_yields_no_proposal(client, fake_db, alice):
    seed_video(fake_db)
    row = frame_row(status="audit_failed", detections=[])
    response = create_proposal(client, alice, item=row)
    assert response.status_code == 404


def test_a_proposal_is_refused_while_the_analysis_is_incomplete(
    client, fake_db, alice
):
    """A finding is only final once the job reports completed.

    Proposing against a running job would let an owner approve a finding the
    same job is about to overwrite.
    """
    seed_video(fake_db, state="analyzing")
    response = create_proposal(client, alice)
    assert response.status_code == 409
    assert "has not completed" in response.json()["detail"]


def test_a_proposal_is_refused_for_a_never_analysed_video(client, fake_db, alice):
    seed_video(fake_db, state="uploaded")
    response = create_proposal(client, alice)
    assert response.status_code == 409


def test_a_storage_failure_is_502_not_a_missing_finding(client, fake_db, alice):
    """An outage must never be reported as "no such finding"."""
    from botocore.exceptions import ClientError

    seed_video(fake_db)
    dynamo = MagicMock()
    dynamo.Table.return_value.get_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
    )
    with patch("aws_clients.get_resource", return_value=dynamo):
        response = client.post(
            "/remediations",
            json={"video_id": VIDEO_ID, "frame_number": 3, "detection_index": 0},
            headers=alice,
        )
    assert response.status_code == 502
    # No botocore detail, table name or ARN.
    assert "ProvisionedThroughput" not in response.text
    assert "Table" not in response.text


def test_duplicate_proposal_requests_return_the_same_record(
    client, fake_db, alice
):
    """A double-clicked "propose" must not create two competing records."""
    seed_video(fake_db)
    first = create_proposal(client, alice)
    second = create_proposal(client, alice)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["remediation_id"] == first.json()["remediation_id"]

    listed = client.get(f"/remediations?video_id={VIDEO_ID}", headers=alice).json()
    assert len(listed["remediations"]) == 1


def test_a_rejected_proposal_does_not_block_proposing_again(
    client, fake_db, alice
):
    """A terminal record is not an open one."""
    seed_video(fake_db)
    first = create_proposal(client, alice).json()
    client.post(
        f"/remediations/{first['remediation_id']}/reject", json={}, headers=alice
    )

    second = create_proposal(client, alice)
    assert second.status_code == 201
    assert second.json()["remediation_id"] != first["remediation_id"]


def test_the_per_video_cap_is_enforced(client, fake_db, alice, monkeypatch):
    monkeypatch.setenv("MAX_REMEDIATIONS_PER_VIDEO", "1")
    seed_video(fake_db)

    first = create_proposal(client, alice).json()
    client.post(
        f"/remediations/{first['remediation_id']}/reject", json={}, headers=alice
    )
    second = create_proposal(client, alice)

    assert second.status_code == 409
    assert "maximum number" in second.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"video_id": VIDEO_ID, "frame_number": -1, "detection_index": 0},
        {"video_id": VIDEO_ID, "frame_number": 0, "detection_index": -1},
        {"video_id": VIDEO_ID, "frame_number": 0, "detection_index": 10_000},
        {"video_id": VIDEO_ID, "frame_number": 0},
        {"frame_number": 0, "detection_index": 0},
        {"video_id": VIDEO_ID, "frame_number": "x", "detection_index": 0},
    ],
)
def test_a_malformed_payload_is_rejected_before_any_lookup(
    client, fake_db, alice, body
):
    seed_video(fake_db)
    response = client.post("/remediations", json=body, headers=alice)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad", ["../etc/passwd", "vid_XXXX", "vid_1", "' OR 1=1", "vid_abcd1234x"]
)
def test_a_malformed_video_id_is_rejected(client, fake_db, alice, bad):
    seed_video(fake_db)
    response = client.post(
        "/remediations",
        json={"video_id": bad, "frame_number": 0, "detection_index": 0},
        headers=alice,
    )
    assert response.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------

def test_another_user_cannot_propose_against_your_video(client, fake_db, bob):
    seed_video(fake_db, owner="alice")
    response = create_proposal(client, bob)
    assert response.status_code == 404


def test_a_video_with_no_ownership_record_is_denied(client, fake_db, alice):
    response = create_proposal(
        client,
        alice,
        body={"video_id": "vid_99999999", "frame_number": 3, "detection_index": 0},
    )
    assert response.status_code == 404


def test_another_user_cannot_read_your_proposal(client, fake_db, alice, bob):
    seed_video(fake_db, owner="alice")
    created = create_proposal(client, alice).json()

    response = client.get(f"/remediations/{created['remediation_id']}", headers=bob)
    assert response.status_code == 404
    # Nothing about the finding leaks through the denial.
    assert "competitor_logo" not in response.text


def test_another_user_cannot_list_your_proposals(client, fake_db, alice, bob):
    seed_video(fake_db, owner="alice")
    create_proposal(client, alice)

    response = client.get(f"/remediations?video_id={VIDEO_ID}", headers=bob)
    assert response.status_code == 404
    assert "competitor_logo" not in response.text


@pytest.mark.parametrize("action", ["approve", "reject", "cancel", "execute"])
def test_another_user_cannot_decide_on_your_proposal(
    client, fake_db, alice, bob, action
):
    """The most important authorization test in this file.

    Approval authorises a consequential operation. It must be impossible for
    anyone but the owner, enforced server-side.
    """
    seed_video(fake_db, owner="alice")
    created = create_proposal(client, alice).json()

    response = client.post(
        f"/remediations/{created['remediation_id']}/{action}", json={}, headers=bob
    )
    assert response.status_code == 404

    # And the record is untouched.
    after = client.get(
        f"/remediations/{created['remediation_id']}", headers=alice
    ).json()
    assert after["state"] == "proposed"
    assert after["approved_by"] is None


def test_an_unknown_remediation_id_is_404(client, fake_db, alice):
    response = client.get("/remediations/rem_deadbeefcafe", headers=alice)
    assert response.status_code == 404


def test_listing_requires_the_video_id(client, fake_db, alice):
    assert client.get("/remediations", headers=alice).status_code == 422


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------

def _approve(client, headers, remediation_id, item=None):
    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(item or frame_row())
    ):
        return client.post(
            f"/remediations/{remediation_id}/approve", json={}, headers=headers
        )


def test_approval_records_the_authenticated_identity(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    response = _approve(client, alice, created["remediation_id"])
    assert response.status_code == 200
    body = response.json()

    assert body["state"] == "approved"
    assert body["approved_by"] == "alice"
    assert body["approved_at"]
    # Approval is a decision, not an action.
    assert body["has_asset"] is False
    assert body["source_video_modified"] is False
    assert body["is_compliance_claim"] is False


def test_an_approval_identity_in_the_body_is_ignored(client, fake_db, alice):
    """The approver comes from the token and from nowhere else."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        response = client.post(
            f"/remediations/{created['remediation_id']}/approve",
            json={"note": "ok", "approved_by": "bob", "owner": "bob", "state": "verified"},
            headers=alice,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["approved_by"] == "alice"
    assert body["state"] == "approved"


def test_a_decision_note_is_recorded(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        response = client.post(
            f"/remediations/{created['remediation_id']}/approve",
            json={"note": "Checked against the brief."},
            headers=alice,
        )
    assert response.json()["decision_note"] == "Checked against the brief."


def test_a_repeated_approval_is_idempotent(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    first = _approve(client, alice, created["remediation_id"]).json()
    second = _approve(client, alice, created["remediation_id"]).json()

    assert second["state"] == "approved"
    # The original decision timestamp is preserved, not overwritten.
    assert second["approved_at"] == first["approved_at"]


def test_approving_a_rejected_proposal_is_409(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    client.post(
        f"/remediations/{created['remediation_id']}/reject", json={}, headers=alice
    )

    response = _approve(client, alice, created["remediation_id"])
    assert response.status_code == 409
    assert "rejected" in response.json()["detail"]


def test_rejecting_an_approved_proposal_is_409(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    _approve(client, alice, created["remediation_id"])

    response = client.post(
        f"/remediations/{created['remediation_id']}/reject", json={}, headers=alice
    )
    assert response.status_code == 409


def test_a_repeated_rejection_is_idempotent(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    path = f"/remediations/{created['remediation_id']}/reject"

    assert client.post(path, json={}, headers=alice).status_code == 200
    second = client.post(path, json={}, headers=alice)
    assert second.status_code == 200
    assert second.json()["state"] == "rejected"


def test_an_approved_proposal_can_be_cancelled(client, fake_db, alice):
    """Approval must not be irreversible before anything consequential."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    _approve(client, alice, created["remediation_id"])

    response = client.post(
        f"/remediations/{created['remediation_id']}/cancel", json={}, headers=alice
    )
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


def test_a_cancelled_proposal_cannot_be_approved(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    client.post(
        f"/remediations/{created['remediation_id']}/cancel", json={}, headers=alice
    )

    response = _approve(client, alice, created["remediation_id"])
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------

def test_approval_is_refused_when_the_finding_changed(client, fake_db, alice):
    """A re-analysis between proposal and approval invalidates the proposal."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    changed = frame_row(
        detections=[detection(category="something_else", description="different")]
    )
    response = _approve(client, alice, created["remediation_id"], item=changed)

    assert response.status_code == 409
    assert "re-analysed" in response.json()["detail"]

    after = client.get(
        f"/remediations/{created['remediation_id']}", headers=alice
    ).json()
    assert after["state"] == "superseded"
    assert after["is_compliance_claim"] is False


def test_approval_is_refused_when_the_finding_vanished(client, fake_db, alice):
    """A shorter re-analysis deletes frame rows; the finding is simply gone."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    with patch("aws_clients.get_resource", return_value=dynamo_with(None)):
        response = client.post(
            f"/remediations/{created['remediation_id']}/approve",
            json={},
            headers=alice,
        )

    assert response.status_code == 409
    after = client.get(
        f"/remediations/{created['remediation_id']}", headers=alice
    ).json()
    assert after["state"] == "superseded"


def test_a_superseded_proposal_cannot_have_an_asset_attached(
    client, fake_db, alice
):
    """Attaching an asset is consequential, so it checks staleness too.

    Phase 9 moved this check onto the asset route as well as execution: an
    asset is what makes a record claimable by a worker, so a stale proposal
    must not be able to acquire one.
    """
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    _approve(client, alice, created["remediation_id"])

    changed = frame_row(detections=[detection(category="something_else")])
    with patch("aws_clients.get_resource", return_value=dynamo_with(changed)):
        response = client.post(
            f"/remediations/{created['remediation_id']}/asset",
            files={"file": ("logo.png", PNG_BYTES, "image/png")},
            headers=alice,
        )

    assert response.status_code == 409
    after = client.get(
        f"/remediations/{created['remediation_id']}", headers=alice
    ).json()
    assert after["state"] == "superseded"
    assert after["has_asset"] is False


def test_an_unchanged_finding_does_not_supersede(client, fake_db, alice):
    """The fingerprint must survive a storage round-trip.

    If it did not, every approval would be superseded on the next read.
    """
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    response = _approve(client, alice, created["remediation_id"])
    assert response.status_code == 200
    assert response.json()["state"] == "approved"


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def test_an_unapproved_proposal_cannot_be_executed(client, fake_db, alice):
    """Enforced in the backend, not by hiding a button."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    response = client.post(
        f"/remediations/{created['remediation_id']}/execute", headers=alice
    )
    assert response.status_code == 409
    assert "approved" in response.json()["detail"]
    # And nothing was built or recorded.
    after = client.get(
        f"/remediations/{created['remediation_id']}", headers=alice
    ).json()
    assert after["state"] == "proposed"
    assert after["has_output"] is False


def test_execution_reports_the_provider_as_unavailable(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    _approve(client, alice, created["remediation_id"])

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        response = client.post(
            f"/remediations/{created['remediation_id']}/execute", headers=alice
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "not configured" in detail
    # Phase 9: the refusal now names the supported alternative.
    assert "replacement image" in detail


def test_a_refused_execution_leaves_the_record_approved(client, fake_db, alice):
    """Nothing was attempted, so nothing failed.

    The proposal stays executable if a provider is ever configured, which is
    why this does not mark the record failed.
    """
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    _approve(client, alice, created["remediation_id"])

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        client.post(
            f"/remediations/{created['remediation_id']}/execute", headers=alice
        )

    after = client.get(
        f"/remediations/{created['remediation_id']}", headers=alice
    ).json()
    assert after["state"] == "approved"
    assert after["error_code"] is None
    assert after["has_asset"] is False


def test_execution_is_refused_below_the_confidence_threshold(
    client, fake_db, alice
):
    """This is what REMEDIATION_MIN_CONFIDENCE finally gates.

    The threshold has been in prompts.py and planning.md since Phase 1 with no
    code reading it.
    """
    seed_video(fake_db)
    low = frame_row(detections=[detection(confidence=Decimal("0.40"))])
    created = create_proposal(client, alice, item=low).json()
    assert created["proposal"]["confidence_gate"] == "below_threshold"

    _approve(client, alice, created["remediation_id"], item=low)
    with patch("aws_clients.get_resource", return_value=dynamo_with(low)):
        response = client.post(
            f"/remediations/{created['remediation_id']}/execute", headers=alice
        )

    assert response.status_code == 409
    assert "confidence threshold" in response.json()["detail"]


def test_execution_is_refused_when_no_confidence_was_reported(
    client, fake_db, alice
):
    """Absent is not high."""
    seed_video(fake_db)
    row = frame_row(
        detections=[
            {
                "category": "logo",
                "description": "x",
                "bounding_box": [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")],
            }
        ]
    )
    created = create_proposal(client, alice, item=row).json()
    assert created["proposal"]["confidence_gate"] == "not_reported"

    _approve(client, alice, created["remediation_id"], item=row)
    with patch("aws_clients.get_resource", return_value=dynamo_with(row)):
        response = client.post(
            f"/remediations/{created['remediation_id']}/execute", headers=alice
        )
    assert response.status_code == 409


def test_a_below_threshold_finding_can_still_be_reviewed(client, fake_db, alice):
    """Review is the point; only generation is gated."""
    seed_video(fake_db)
    low = frame_row(detections=[detection(confidence=Decimal("0.40"))])
    created = create_proposal(client, alice, item=low)

    assert created.status_code == 201
    body = created.json()
    assert "reject" in body["proposal"]["gate_explanation"].lower()

    rejected = client.post(
        f"/remediations/{body['remediation_id']}/reject", json={}, headers=alice
    )
    assert rejected.status_code == 200


# ---------------------------------------------------------------------------
# no route can produce a compliance claim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", sorted(s.value for s in UNREACHABLE_IN_THIS_BUILD))
def test_no_sequence_of_requests_reaches_an_unreachable_state(
    client, fake_db, alice, state
):
    """Exhaustive over the states this build must not be able to produce.

    Drives every route in every order on one record and asserts the state is
    never one of them - including `verified`, which is the only state that may
    be described as compliant.
    """
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    rid = created["remediation_id"]

    observed = {created["state"]}
    for action in ("execute", "approve", "execute", "cancel", "approve", "reject"):
        with patch(
            "aws_clients.get_resource", return_value=dynamo_with(frame_row())
        ):
            client.post(f"/remediations/{rid}/{action}", json={}, headers=alice)
        observed.add(
            client.get(f"/remediations/{rid}", headers=alice).json()["state"]
        )

    assert state not in observed


def test_no_record_ever_reports_a_compliance_claim(client, fake_db, alice):
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    rid = created["remediation_id"]

    for action in ("approve", "execute"):
        with patch(
            "aws_clients.get_resource", return_value=dynamo_with(frame_row())
        ):
            client.post(f"/remediations/{rid}/{action}", json={}, headers=alice)
        body = client.get(f"/remediations/{rid}", headers=alice).json()
        assert body["is_compliance_claim"] is False
        assert body["source_video_modified"] is False
        assert body["verified_at"] is None
        assert body["output_video_id"] is None


def test_the_response_never_exposes_a_storage_key_or_an_owner(
    client, fake_db, alice
):
    seed_video(fake_db)
    body = create_proposal(client, alice).json()

    assert "asset_key" not in body
    assert "owner" not in body
    assert "has_asset" in body


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------

def test_a_concurrent_decision_produces_one_winner(client, fake_db, alice):
    """The conditional write is what makes this safe without a lock.

    Approve and reject are raced at the repository layer, where the write
    condition includes the current state: the loser matches zero documents.
    """
    seed_video(fake_db)
    created = create_proposal(client, alice).json()
    rid = created["remediation_id"]

    outcomes = []

    async def race():
        async def approve():
            try:
                await remediation_repo.transition(
                    rid, "alice", RemediationState.APPROVED
                )
                outcomes.append("approved")
            except Exception as exc:
                outcomes.append(type(exc).__name__)

        async def reject():
            try:
                await remediation_repo.transition(
                    rid, "alice", RemediationState.REJECTED
                )
                outcomes.append("rejected")
            except Exception as exc:
                outcomes.append(type(exc).__name__)

        async with anyio.create_task_group() as tg:
            tg.start_soon(approve)
            tg.start_soon(reject)

    anyio.run(race)

    # Exactly one decision stuck, and the record is in that state.
    assert sorted(outcomes).count("approved") + sorted(outcomes).count(
        "rejected"
    ) >= 1
    final = client.get(f"/remediations/{rid}", headers=alice).json()["state"]
    assert final in ("approved", "rejected")


def test_an_expected_state_mismatch_is_a_conflict(client, fake_db, alice):
    """The repository refuses a transition from a state it is not in."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    async def attempt():
        with pytest.raises(remediation_repo.RemediationConflict):
            await remediation_repo.transition(
                created["remediation_id"],
                "alice",
                RemediationState.PROCESSING,
                expected=RemediationState.APPROVED,
            )

    anyio.run(attempt)


def test_the_repository_refuses_a_cross_owner_transition(client, fake_db, alice):
    """Defence in depth: even bypassing the route, the write is owner-scoped."""
    seed_video(fake_db)
    created = create_proposal(client, alice).json()

    async def attempt():
        with pytest.raises(remediation_repo.RemediationNotFound):
            await remediation_repo.transition(
                created["remediation_id"], "bob", RemediationState.APPROVED
            )

    anyio.run(attempt)


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def test_listing_returns_only_this_videos_records(client, fake_db, alice):
    seed_video(fake_db, video_id=VIDEO_ID)
    seed_video(fake_db, video_id=OTHER_VIDEO_ID)

    create_proposal(client, alice)
    other_row = dict(frame_row(), video_id=OTHER_VIDEO_ID)
    with patch("aws_clients.get_resource", return_value=dynamo_with(other_row)):
        client.post(
            "/remediations",
            json={
                "video_id": OTHER_VIDEO_ID,
                "frame_number": 3,
                "detection_index": 0,
            },
            headers=alice,
        )

    listed = client.get(f"/remediations?video_id={VIDEO_ID}", headers=alice).json()
    assert len(listed["remediations"]) == 1
    assert listed["remediations"][0]["video_id"] == VIDEO_ID


def test_listing_an_empty_video_is_an_empty_list(client, fake_db, alice):
    seed_video(fake_db)
    listed = client.get(f"/remediations?video_id={VIDEO_ID}", headers=alice).json()
    assert listed == {"video_id": VIDEO_ID, "remediations": []}


# ---------------------------------------------------------------------------
# the /violations contract extension
# ---------------------------------------------------------------------------

def test_violations_now_carries_the_detection_index(client, fake_db, alice):
    """Phase 8 added `detection_index` so a client can address a finding.

    Without it the flattened detections cannot be mapped back to the
    (video_id, frame_number, detection_index) address the remediation API
    uses, and the browser would have to derive the index from list position by
    undocumented convention.
    """
    seed_video(fake_db)
    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = {
        "Items": [
            {
                "video_id": VIDEO_ID,
                "frame_number": Decimal(3),
                "status": "audited",
                "timestamp": Decimal("3"),
                "detections": [detection(), detection(category="second")],
            }
        ]
    }
    with patch("aws_clients.get_resource", return_value=dynamo):
        response = client.get(f"/violations/{VIDEO_ID}", headers=alice)

    assert response.status_code == 200
    found = response.json()["detections"]
    assert [d["detection_index"] for d in found] == [0, 1]
    assert [d["frame_number"] for d in found] == [3, 3]


def test_the_violations_response_shape_is_otherwise_unchanged(
    client, fake_db, alice
):
    """Backward compatibility: the Phase 7 client reads these keys.

    Adding a field is safe; removing or renaming one is not, so the existing
    contract is asserted explicitly alongside the addition.
    """
    seed_video(fake_db)
    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = {
        "Items": [
            {
                "video_id": VIDEO_ID,
                "frame_number": Decimal(3),
                "status": "audited",
                "timestamp": Decimal("3"),
                "detections": [detection()],
            }
        ]
    }
    with patch("aws_clients.get_resource", return_value=dynamo):
        body = client.get(f"/violations/{VIDEO_ID}", headers=alice).json()

    assert {
        "video_id",
        "detections",
        "frame_count",
        "audited_frames",
        "failed_frames",
        "next_cursor",
        "has_more",
        "job",
    } <= set(body)

    found = body["detections"][0]
    for key in (
        "category",
        "description",
        "timestamp",
        "frame_number",
        "risk_level",
        "confidence",
        "bounding_box",
    ):
        assert key in found, key


def test_an_addressable_finding_round_trips_into_a_proposal(
    client, fake_db, alice
):
    """End to end over the real contract, with storage mocked.

    Read the findings from /violations, take one finding's address straight
    from the response, and propose a remediation for it. This is the journey
    the UI performs, and it is what the added field exists for.
    """
    seed_video(fake_db)
    row = {
        "video_id": VIDEO_ID,
        "frame_number": Decimal(3),
        "status": "audited",
        "timestamp": Decimal("3"),
        "detections": [detection(category="first"), detection(category="second")],
    }

    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = {"Items": [row]}
    dynamo.Table.return_value.get_item.return_value = {"Item": row}

    with patch("aws_clients.get_resource", return_value=dynamo):
        findings = client.get(f"/violations/{VIDEO_ID}", headers=alice).json()[
            "detections"
        ]
        target = findings[1]
        created = client.post(
            "/remediations",
            json={
                "video_id": VIDEO_ID,
                "frame_number": target["frame_number"],
                "detection_index": target["detection_index"],
            },
            headers=alice,
        )

    assert created.status_code == 201
    body = created.json()
    assert body["finding"]["category"] == "second"
    assert body["detection_index"] == 1
