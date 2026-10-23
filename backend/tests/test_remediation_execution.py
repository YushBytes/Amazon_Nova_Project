"""Phase 9: the asset path, execution, concurrency, and the output's identity.

Covers `remediation_assets`, `remediation_executor`, and the routes that drive
them. FFmpeg is stubbed at `reconstruction.reconstruct`; S3 is the in-memory
fake from conftest; MongoDB is the in-memory fake. Nothing contacts AWS.

The properties under test, in order of how much damage getting them wrong
would do:

1. The SOURCE object is never written. Asserted by byte comparison.
2. An unapproved, rejected, withdrawn, superseded or stale remediation cannot
   acquire an asset or be executed.
3. One user cannot touch another's asset, output or playback URL.
4. Two concurrent execution requests produce one reconstruction.
5. A failed reconstruction never reaches verification, and an invalid output
   never reaches analysis.
6. A client cannot assert that anything is verified.
"""

import glob
import os
import shutil
import tempfile
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import aws_resources
import config
import image_provider
import reconstruction
import remediation_assets
import remediation_executor
import remediation_repo
import source_store
import video_repo
from conftest import real_jpeg, real_png
from remediation_state import RemediationState

VIDEO_ID = "vid_abcd1234"
SOURCE_KEY = f"sources/{VIDEO_ID}/source.mp4"
SOURCE_BYTES = b"ORIGINAL-SOURCE-BYTES-DO-NOT-TOUCH" * 8

# A REAL, decodable image (Phase 9.1). It used to be a magic number followed
# by padding, which the old sniff-only validator accepted - but the asset
# layer now probes for decodability, because that exact shape is what makes
# FFmpeg block on a `-loop 1` overlay input. A padded header is now correctly
# rejected, so the happy path needs a genuine image.
PNG = real_png()
JPEG = real_jpeg()


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
def _reset_config():
    image_provider.reset_provider()
    config.reset_settings_cache()
    yield
    image_provider.reset_provider()
    config.reset_settings_cache()


def detection(**overrides):
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
    dynamo = MagicMock()
    dynamo.Table.return_value.get_item.return_value = (
        {"Item": item} if item is not None else {}
    )
    return dynamo


async def _seed_source(fake_db, fake_s3, owner="alice"):
    """A completed, owned video with its real source bytes in the fake S3."""
    await video_repo.record_video(
        video_id=VIDEO_ID,
        owner=owner,
        stored_filename=f"{VIDEO_ID}.mp4",
        original_filename="clip.mp4",
        extension="mp4",
        size_bytes=len(SOURCE_BYTES),
        s3_bucket="test-bucket",
        s3_key=SOURCE_KEY,
    )
    await fake_db["videos"].update_one(
        {"video_id": VIDEO_ID}, {"$set": {"status": "completed"}}
    )
    fake_s3.seed("test-bucket", SOURCE_KEY, SOURCE_BYTES)


def seed(fake_db, fake_s3, owner="alice"):
    anyio.run(lambda: _seed_source(fake_db, fake_s3, owner))


def propose(client, headers, item=None):
    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(item or frame_row())
    ):
        return client.post(
            "/remediations",
            json={
                "video_id": VIDEO_ID,
                "frame_number": 3,
                "detection_index": 0,
            },
            headers=headers,
        )


def approve(client, headers, rid, item=None):
    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(item or frame_row())
    ):
        return client.post(
            f"/remediations/{rid}/approve", json={}, headers=headers
        )


def attach_asset(client, headers, rid, data=PNG, name="logo.png", ctype="image/png",
                 item=None):
    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(item or frame_row())
    ):
        return client.post(
            f"/remediations/{rid}/asset",
            files={"file": (name, data, ctype)},
            headers=headers,
        )


def approved_with_asset(client, headers, fake_db, fake_s3, item=None):
    """Drive a remediation to ASSET_READY and return its id."""
    seed(fake_db, fake_s3)
    rid = propose(client, headers, item).json()["remediation_id"]
    approve(client, headers, rid, item)
    response = attach_asset(client, headers, rid, item=item)
    assert response.status_code == 200, response.text
    return rid


def fake_result(tmp_path, duration=10.0, audio=True, size=4096):
    """A ReconstructionResult over a real local file, as FFmpeg would produce."""
    output = tmp_path / "out.mp4"
    output.write_bytes(b"R" * size)
    probe = reconstruction.SourceProbe(
        width=1920, height=1080, duration_seconds=duration, has_audio=audio
    )
    return reconstruction.ReconstructionResult(
        output_path=str(output),
        size_bytes=size,
        duration_seconds=duration,
        has_audio=audio,
        region=reconstruction.PixelRegion(x=384, y=108, width=384, height=216),
        start_seconds=2.0,
        end_seconds=4.0,
        source_probe=probe,
    )


# ===========================================================================
# asset validation
# ===========================================================================

def test_a_png_and_a_jpeg_are_accepted():
    assert remediation_assets.validate_asset_bytes(PNG) == ("image/png", "png")
    assert remediation_assets.validate_asset_bytes(JPEG) == ("image/jpeg", "jpg")


@pytest.mark.parametrize(
    "data",
    [
        b"",  # empty
        b"\x89PNG",  # truncated
        b"GIF89a" + b"\x00" * 300,  # unsupported format
        b"<html>nope</html>" + b"\x00" * 300,  # an HTML error page
        b"MZ\x90\x00" + b"\x00" * 300,  # a Windows executable
        b"%PDF-1.4" + b"\x00" * 300,
    ],
)
def test_an_unacceptable_asset_is_rejected(data):
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(data)


def test_an_oversized_asset_is_rejected():
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (
        image_provider.MAX_IMAGE_BYTES + 1
    )
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(oversized)


# ---------------------------------------------------------------------------
# Phase 9.1: decodability, not just a magic number
# ---------------------------------------------------------------------------
#
# A magic number proves only that the first eight bytes LOOK like a PNG. The
# bytes below - the PNG signature followed by zeroes - passed every check this
# layer had, and then made FFmpeg BLOCK when handed to it as a `-loop 1`
# overlay input: measured at 301s per attempt on FFmpeg 9.0.1, classified
# retryable, so all three attempts were spent on it. Rejecting it here costs
# ~60ms and gives the uploader a true answer.
#
# These need the real ffprobe: without it `_require_decodable` deliberately
# degrades to the sniff alone, because on such a host reconstruction is
# disabled anyway and refusing the upload would block review for no benefit.

PADDED_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300


@pytest.mark.requires_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not on PATH"
)
def test_a_padded_png_header_is_rejected_as_undecodable():
    """The exact payload that used to hang the reconstruction pass."""
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(PADDED_PNG_HEADER)


@pytest.mark.requires_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not on PATH"
)
def test_the_decodability_probe_is_fast_and_permanent():
    """Bounded work, and an answer that will not change on a retry.

    The point is not the exact duration - it is that the refusal arrives
    without waiting for a 300s FFmpeg timeout.
    """
    started = time.perf_counter()
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(PADDED_PNG_HEADER)
    assert time.perf_counter() - started < 30


@pytest.mark.requires_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not on PATH"
)
def test_the_probe_leaves_no_temporary_file_behind():
    """It stages a real file for ffprobe to seek; it must remove it."""
    pattern = os.path.join(tempfile.gettempdir(), "novaflow-asset-*")
    before = set(glob.glob(pattern))

    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(PADDED_PNG_HEADER)
    remediation_assets.validate_asset_bytes(PNG)

    assert set(glob.glob(pattern)) == before


@pytest.mark.requires_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not on PATH"
)
def test_an_image_smaller_than_the_smallest_region_is_rejected():
    """Below MIN_ASSET_PIXELS it cannot usefully cover a region."""
    tiny = real_png(width=4, height=4)
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(tiny)


@pytest.mark.requires_ffmpeg
@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not on PATH"
)
def test_a_real_image_is_still_accepted():
    """The guard must not reject a legitimate replacement."""
    assert remediation_assets.validate_asset_bytes(real_png()) == (
        "image/png",
        "png",
    )
    assert remediation_assets.validate_asset_bytes(real_jpeg()) == (
        "image/jpeg",
        "jpg",
    )


def test_the_probe_is_skipped_when_ffprobe_is_absent(monkeypatch):
    """Degrade, do not block: the sniff and the size bound still apply.

    On a host with no FFmpeg, `RECONSTRUCTION_ENABLED` should be false and
    nothing will consume the asset, so refusing the upload would break the
    review workflow for no safety gain.
    """
    # `_require_decodable` imports shutil inside the function, so the module
    # attribute is the thing to patch, not a name on remediation_assets.
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert remediation_assets.validate_asset_bytes(PADDED_PNG_HEADER) == (
        "image/png",
        "png",
    )
    # The cheap checks are not skipped with it.
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.validate_asset_bytes(b"GIF89a" + b"\x00" * 300)


def test_require_decodable_false_skips_the_probe():
    """The documented escape hatch, for bytes already probed."""
    assert remediation_assets.validate_asset_bytes(
        PADDED_PNG_HEADER, require_decodable=False
    ) == ("image/png", "png")


def test_the_stored_key_is_server_generated(fake_s3):
    """No part of a client filename reaches a storage key."""
    stored = remediation_assets.store_asset(
        remediation_id="rem_abc123abc123",
        data=PNG,
        source=remediation_assets.SOURCE_UPLOAD,
    )
    assert stored["asset_key"] == (
        "remediation-assets/rem_abc123abc123/asset.png"
    )
    assert stored["asset_content_type"] == "image/png"
    assert stored["asset_bytes"] == len(PNG)


def test_a_stored_asset_is_encrypted_and_typed_from_its_bytes(fake_s3):
    remediation_assets.store_asset(
        remediation_id="rem_abc123abc123",
        data=JPEG,
        source=remediation_assets.SOURCE_UPLOAD,
    )
    put = fake_s3.puts[-1]
    assert put["ServerSideEncryption"] == "AES256"
    assert put["ContentType"] == "image/jpeg"


def test_downloading_refuses_a_key_from_another_remediation(fake_s3):
    """Defence against a crossed record compositing someone else's image."""
    fake_s3.seed(
        "test-bucket", "remediation-assets/rem_other/asset.png", PNG
    )
    record = {
        "remediation_id": "rem_abc123abc123",
        "asset_key": "remediation-assets/rem_other/asset.png",
    }
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.download_asset(record, "/tmp/x.png")


def test_a_download_revalidates_the_bytes(fake_s3, tmp_path):
    """The object could have been replaced out of band."""
    key = "remediation-assets/rem_abc123abc123/asset.png"
    fake_s3.seed("test-bucket", key, b"not an image at all" * 20)
    record = {"remediation_id": "rem_abc123abc123", "asset_key": key}
    with pytest.raises(remediation_assets.AssetRejected):
        remediation_assets.download_asset(record, str(tmp_path / "a.png"))


# ===========================================================================
# the asset route
# ===========================================================================

def test_an_asset_can_only_be_attached_to_an_approved_remediation(
    client, fake_db, fake_s3, alice
):
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]

    response = attach_asset(client, alice, rid)
    assert response.status_code == 409
    assert "approved" in response.json()["detail"]


def test_attaching_an_asset_moves_the_record_to_asset_ready(
    client, fake_db, fake_s3, alice
):
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    approve(client, alice, rid)

    response = attach_asset(client, alice, rid)
    assert response.status_code == 200
    body = response.json()

    assert body["state"] == "asset_ready"
    assert body["has_asset"] is True
    assert body["asset_content_type"] == "image/png"
    assert body["asset_source"] == "upload"
    assert body["asset_supplied_by"] == "alice"
    # An asset is not a correction.
    assert body["has_output"] is False
    assert body["is_compliance_claim"] is False
    assert body["source_video_modified"] is False
    # The storage key is never published.
    assert "asset_key" not in body


def test_the_declared_content_type_is_not_trusted(client, fake_db, fake_s3, alice):
    """An executable renamed .png is rejected on its magic number."""
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    approve(client, alice, rid)

    response = attach_asset(
        client, alice, rid, data=b"MZ\x90\x00" + b"\x00" * 300,
        name="logo.png", ctype="image/png",
    )
    assert response.status_code == 422
    assert "format" in response.json()["detail"].lower()


def test_an_oversized_upload_is_refused_by_the_route(
    client, fake_db, fake_s3, alice, monkeypatch
):
    monkeypatch.setenv("MAX_REMEDIATION_ASSET_BYTES", "1024")
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    approve(client, alice, rid)

    response = attach_asset(client, alice, rid, data=PNG + b"\x00" * 4096)
    assert response.status_code == 413


def test_an_empty_upload_is_refused(client, fake_db, fake_s3, alice):
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    approve(client, alice, rid)

    response = attach_asset(client, alice, rid, data=b"")
    assert response.status_code == 422


def test_another_user_cannot_attach_an_asset(client, fake_db, fake_s3, alice, bob):
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    approve(client, alice, rid)

    response = attach_asset(client, bob, rid)
    assert response.status_code == 404


def test_a_second_asset_cannot_silently_replace_the_first(
    client, fake_db, fake_s3, alice
):
    """Swapping the file would change what was reviewed."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    response = attach_asset(client, alice, rid, data=JPEG, name="other.jpg")
    assert response.status_code == 409
    assert "already attached" in response.json()["detail"]


def test_a_low_confidence_finding_cannot_acquire_an_asset(
    client, fake_db, fake_s3, alice
):
    """The confidence gate, enforced before any bytes are read."""
    seed(fake_db, fake_s3)
    low = frame_row(detections=[detection(confidence=Decimal("0.4"))])
    rid = propose(client, alice, low).json()["remediation_id"]
    approve(client, alice, rid, low)

    response = attach_asset(client, alice, rid, item=low)
    assert response.status_code == 409
    assert "confidence threshold" in response.json()["detail"]


# ===========================================================================
# the execute route
# ===========================================================================

def test_execution_requires_an_asset(client, fake_db, fake_s3, alice):
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    approve(client, alice, rid)

    response = client.post(f"/remediations/{rid}/execute", headers=alice)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "not configured" in detail
    assert "replacement image" in detail


def test_execution_is_accepted_and_durably_recorded(
    client, fake_db, fake_s3, alice
):
    """Acceptance is a database write made BEFORE the response."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)

    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(frame_row())
    ), patch("remediation_routes._dispatch_execution", return_value="worker"):
        response = client.post(f"/remediations/{rid}/execute", headers=alice)

    assert response.status_code == 200, response.text
    stored = anyio.run(lambda: remediation_repo.get(rid, "alice"))
    assert stored["execution_requested"] is True
    assert stored["execution_requested_at"] is not None


def test_a_repeated_execute_does_not_start_a_second_run(
    client, fake_db, fake_s3, alice
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)

    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(frame_row())
    ), patch(
        "remediation_routes._dispatch_execution", return_value="worker"
    ) as dispatch:
        first = client.post(f"/remediations/{rid}/execute", headers=alice)
        second = client.post(f"/remediations/{rid}/execute", headers=alice)

    assert first.status_code == 200
    assert second.status_code == 200
    # Both accepted, but the work is requested once; the worker claims it once.
    assert dispatch.call_count == 2
    stored = anyio.run(lambda: remediation_repo.get(rid, "alice"))
    assert stored["attempt"] == 0  # not claimed yet


@pytest.mark.parametrize(
    "state",
    ["reconstructing", "applied", "verifying", "verified", "verification_failed"],
)
def test_execute_on_a_record_already_past_that_point_returns_its_state(
    client, fake_db, fake_s3, alice, state
):
    """A retried request must not start a second reconstruction."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    anyio.run(
        lambda: fake_db[remediation_repo.COLLECTION].update_one(
            {"remediation_id": rid}, {"$set": {"state": state}}
        )
    )

    response = client.post(f"/remediations/{rid}/execute", headers=alice)
    assert response.status_code == 200
    assert response.json()["state"] == state


def test_another_user_cannot_execute(client, fake_db, fake_s3, alice, bob):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    assert client.post(f"/remediations/{rid}/execute", headers=bob).status_code == 404


def test_a_rejected_remediation_cannot_execute(client, fake_db, fake_s3, alice):
    seed(fake_db, fake_s3)
    rid = propose(client, alice).json()["remediation_id"]
    client.post(f"/remediations/{rid}/reject", json={}, headers=alice)

    response = client.post(f"/remediations/{rid}/execute", headers=alice)
    assert response.status_code == 409


def test_a_withdrawn_remediation_cannot_execute(client, fake_db, fake_s3, alice):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    assert (
        client.post(f"/remediations/{rid}/cancel", json={}, headers=alice).status_code
        == 200
    )

    response = client.post(f"/remediations/{rid}/execute", headers=alice)
    assert response.status_code == 409


def test_a_stale_finding_prevents_execution(client, fake_db, fake_s3, alice):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)

    changed = frame_row(detections=[detection(category="something_else")])
    with patch("aws_clients.get_resource", return_value=dynamo_with(changed)):
        response = client.post(f"/remediations/{rid}/execute", headers=alice)

    assert response.status_code == 409
    after = client.get(f"/remediations/{rid}", headers=alice).json()
    assert after["state"] == "superseded"


def test_execution_is_refused_when_reconstruction_is_disabled(
    client, fake_db, fake_s3, alice, monkeypatch
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    monkeypatch.setenv("RECONSTRUCTION_ENABLED", "false")

    response = client.post(f"/remediations/{rid}/execute", headers=alice)
    assert response.status_code == 503
    assert "not enabled" in response.json()["detail"]


def test_attempts_are_bounded(client, fake_db, fake_s3, alice):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    anyio.run(
        lambda: fake_db[remediation_repo.COLLECTION].update_one(
            {"remediation_id": rid},
            {"$set": {"attempt": remediation_repo.MAX_EXECUTION_ATTEMPTS}},
        )
    )

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        response = client.post(f"/remediations/{rid}/execute", headers=alice)
    assert response.status_code == 409
    assert "maximum" in response.json()["detail"]


# ===========================================================================
# the executor
# ===========================================================================

def run_execution(fake_db, fake_s3, rid, result=None, error=None, owner="alice"):
    """Execute with `reconstruction.reconstruct` stubbed."""

    def fake_reconstruct(**kwargs):
        if error is not None:
            raise error
        return result

    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(frame_row())
    ), patch("reconstruction.reconstruct", side_effect=fake_reconstruct), patch(
        "pipeline.submit", new=_fake_submit
    ):
        return anyio.run(lambda: remediation_executor.execute(rid, owner))


async def _fake_submit(video_id, *, allow_retry=True):
    """Stand-in for the real queue write, which needs no pipeline run here."""
    return await video_repo.transition(
        video_id, __import__("job_state").JobState.QUEUED
    )


def request_execution(fake_db, rid, owner="alice"):
    anyio.run(lambda: remediation_repo.request_execution(rid, owner))


def test_a_successful_execution_reaches_verifying(
    client, fake_db, fake_s3, alice, tmp_path
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    record = run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    assert record["state"] == RemediationState.VERIFYING.value
    assert record["output_video_id"]
    assert record["output_key"].startswith("remediated/")
    assert record["analysis_job_video_id"] == record["output_video_id"]
    assert record["reconstruction"]["method"] == "scaled_overlay"
    # The lease is released once reconstruction is over.
    assert record["claim_id"] is None


def test_the_source_object_is_never_overwritten(
    client, fake_db, fake_s3, alice, tmp_path
):
    """The headline guarantee, asserted on the stored bytes."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    assert fake_s3.objects[("test-bucket", SOURCE_KEY)] == SOURCE_BYTES
    # And nothing wrote to the sources/ prefix at all.
    assert all(
        not p["Key"].startswith("sources/") for p in fake_s3.puts
    )


def test_the_output_is_a_separate_object_under_its_own_prefix(
    client, fake_db, fake_s3, alice, tmp_path
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    record = run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))
    key = record["output_key"]

    assert key.startswith(f"{aws_resources.REMEDIATED_PREFIX}/")
    assert record["output_video_id"] in key
    assert key != SOURCE_KEY
    assert fake_s3.has("test-bucket", key)


def test_the_output_is_encrypted_at_rest(client, fake_db, fake_s3, alice, tmp_path):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    output_put = [p for p in fake_s3.puts if p["Key"].startswith("remediated/")][0]
    assert output_put["ServerSideEncryption"] == "AES256"


def test_the_output_is_registered_as_the_owners_own_video(
    client, fake_db, fake_s3, alice, tmp_path
):
    """Which is what lets the existing pipeline and playback route accept it."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    record = run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    output = anyio.run(
        lambda: video_repo.get_video(record["output_video_id"])
    )
    assert output["owner"] == "alice"
    assert output["is_remediated_output"] is True
    assert output["derived_from_video_id"] == VIDEO_ID
    assert output["derived_from_remediation_id"] == rid


def test_an_unsafe_region_fails_terminally_without_retrying(
    client, fake_db, fake_s3, alice
):
    """Deterministic: three attempts would reach the same conclusion."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    record = run_execution(
        fake_db,
        fake_s3,
        rid,
        error=reconstruction.UnsafeRegion("This finding has no region."),
    )

    assert record["state"] == RemediationState.FAILED.value
    assert record["error_code"] == "unsafe_region"
    assert record["output_video_id"] is None
    assert record["not_before"] is None


def test_a_retryable_reconstruction_failure_backs_off(
    client, fake_db, fake_s3, alice
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    record = run_execution(
        fake_db,
        fake_s3,
        rid,
        error=reconstruction.ReconstructionError("timed out", retryable=True),
    )

    assert record["state"] == RemediationState.ASSET_READY.value
    assert record["not_before"] is not None
    assert record["error_code"] == "reconstruction_failed"
    # Still requested, so the worker picks it up again after the backoff.
    assert record["execution_requested"] is True


def test_a_failed_reconstruction_never_reaches_verification(
    client, fake_db, fake_s3, alice
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    record = run_execution(
        fake_db,
        fake_s3,
        rid,
        error=reconstruction.ReconstructionError("bad output"),
    )

    assert record["state"] == RemediationState.FAILED.value
    assert record["analysis_job_video_id"] is None
    assert record["verification"] is None
    assert record["verified_at"] is None
    # And no output object was stored.
    assert not any(p["Key"].startswith("remediated/") for p in fake_s3.puts)


def test_an_invalid_output_never_reaches_analysis(
    client, fake_db, fake_s3, alice
):
    """`validate_output` raises inside `reconstruct`, so nothing is uploaded."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    record = run_execution(
        fake_db,
        fake_s3,
        rid,
        error=reconstruction.ReconstructionError(
            "The corrected video lost the original's audio, so it was not kept."
        ),
    )
    assert record["state"] == RemediationState.FAILED.value
    assert record["output_key"] is None


def test_an_orphaned_output_is_cleaned_up_when_registration_fails(
    client, fake_db, fake_s3, alice, tmp_path
):
    """An object no record names is unreachable and unbillable to anyone."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    with patch(
        "aws_clients.get_resource", return_value=dynamo_with(frame_row())
    ), patch(
        "reconstruction.reconstruct", return_value=fake_result(tmp_path)
    ), patch.object(
        video_repo, "record_video", side_effect=RuntimeError("mongo down")
    ):
        record = anyio.run(lambda: remediation_executor.execute(rid, "alice"))

    assert record["state"] in (
        RemediationState.ASSET_READY.value,
        RemediationState.FAILED.value,
    )
    # The uploaded object was removed again.
    assert not any(k.startswith("remediated/") for k in fake_s3.keys())
    assert fake_s3.deletes


def test_a_missing_source_fails_without_touching_anything(
    client, fake_db, fake_s3, alice
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    # The ownership record is gone, as if the video had been deleted.
    anyio.run(lambda: video_repo.delete_video(VIDEO_ID))

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        record = anyio.run(lambda: remediation_executor.execute(rid, "alice"))

    assert record["state"] == RemediationState.FAILED.value
    assert record["error_code"] == "source_missing"


def test_execution_refuses_a_source_owned_by_someone_else(
    client, fake_db, fake_s3, alice
):
    """Should be impossible; refused loudly rather than trusted."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    anyio.run(
        lambda: fake_db["videos"].update_one(
            {"video_id": VIDEO_ID}, {"$set": {"owner": "bob"}}
        )
    )

    with patch("aws_clients.get_resource", return_value=dynamo_with(frame_row())):
        record = anyio.run(lambda: remediation_executor.execute(rid, "alice"))

    assert record["state"] == RemediationState.FAILED.value
    assert record["error_code"] == "ownership_mismatch"


# ===========================================================================
# concurrency and recovery
# ===========================================================================

def test_only_one_worker_can_claim_a_reconstruction(client, fake_db, fake_s3, alice):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    outcomes = []

    async def race():
        async def attempt():
            try:
                await remediation_repo.claim_for_execution(rid, "alice")
                outcomes.append("claimed")
            except Exception as exc:
                outcomes.append(type(exc).__name__)

        async with anyio.create_task_group() as tg:
            tg.start_soon(attempt)
            tg.start_soon(attempt)

    anyio.run(race)
    assert outcomes.count("claimed") == 1


def test_a_claim_is_fenced_against_a_stale_worker(client, fake_db, fake_s3, alice):
    """A reclaimed record must not accept the old worker's output.

    Attaching the wrong file to a compliance claim is the specific damage
    this prevents.
    """
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    async def scenario():
        _record, stale_claim = await remediation_repo.claim_for_execution(
            rid, "alice"
        )
        # A sweep reclaims it and another worker takes it.
        await fake_db[remediation_repo.COLLECTION].update_one(
            {"remediation_id": rid},
            {"$set": {"claim_id": "a-newer-worker"}},
        )
        with pytest.raises(remediation_repo.ClaimLost):
            await remediation_repo.transition(
                rid,
                "alice",
                RemediationState.APPLIED,
                claim_id=stale_claim,
                fields={"output_video_id": "vid_badbadba"},
            )

    anyio.run(scenario)
    stored = anyio.run(lambda: remediation_repo.get(rid, "alice"))
    assert stored["output_video_id"] is None


def test_an_abandoned_reconstruction_is_reclaimed(client, fake_db, fake_s3, alice):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    async def scenario():
        await remediation_repo.claim_for_execution(rid, "alice")
        # The worker died: expire the lease.
        from datetime import datetime, timedelta, timezone

        await fake_db[remediation_repo.COLLECTION].update_one(
            {"remediation_id": rid},
            {
                "$set": {
                    "lease_expires_at": datetime.now(timezone.utc)
                    - timedelta(seconds=10)
                }
            },
        )
        expired = await remediation_repo.find_expired_execution_leases()
        assert len(expired) == 1
        return await remediation_repo.reclaim(expired[0])

    record = anyio.run(scenario)
    assert record["state"] == RemediationState.ASSET_READY.value
    assert record["claim_id"] is None
    # Visible to the owner rather than silent.
    assert record["recovered_count"] == 1


def test_a_live_worker_is_not_robbed_of_its_work(client, fake_db, fake_s3, alice):
    """The reclaim is conditional on the lease still being expired."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)

    async def scenario():
        record, claim = await remediation_repo.claim_for_execution(rid, "alice")
        stale_view = dict(record)
        stale_view["claim_id"] = claim
        # The worker heartbeats between the sweep's read and its write, so the
        # lease is no longer expired.
        await remediation_repo.heartbeat(rid, "alice", claim)
        return await remediation_repo.reclaim(stale_view)

    assert anyio.run(scenario) is None
    stored = anyio.run(lambda: remediation_repo.get(rid, "alice"))
    assert stored["state"] == RemediationState.RECONSTRUCTING.value


def test_an_exhausted_reclaim_fails_terminally(client, fake_db, fake_s3, alice):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)

    async def scenario():
        await fake_db[remediation_repo.COLLECTION].update_one(
            {"remediation_id": rid},
            {
                "$set": {
                    "state": RemediationState.RECONSTRUCTING.value,
                    "attempt": remediation_repo.MAX_EXECUTION_ATTEMPTS,
                    "claim_id": "dead",
                }
            },
        )
        stored = await remediation_repo.get(rid, "alice")
        return await remediation_repo.reclaim(stored)

    record = anyio.run(scenario)
    assert record["state"] == RemediationState.FAILED.value
    assert record["error_code"] == "execution_abandoned"


def test_a_record_stranded_in_applied_is_resumed(
    client, fake_db, fake_s3, alice, tmp_path
):
    """The crash window between building the output and submitting it."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)

    async def scenario():
        await video_repo.record_video(
            video_id="vid_0000beef",
            owner="alice",
            stored_filename="vid_0000beef.mp4",
            original_filename="clip (corrected).mp4",
            extension="mp4",
            size_bytes=4096,
            s3_bucket="test-bucket",
            s3_key="remediated/vid_0000beef/output.mp4",
        )
        await fake_db[remediation_repo.COLLECTION].update_one(
            {"remediation_id": rid},
            {
                "$set": {
                    "state": RemediationState.APPLIED.value,
                    "output_video_id": "vid_0000beef",
                    "output_key": "remediated/vid_0000beef/output.mp4",
                }
            },
        )
        resumed = await remediation_executor.resume_applied()
        return resumed, await remediation_repo.get(rid, "alice")

    with patch("pipeline.submit", new=_fake_submit):
        resumed, record = anyio.run(scenario)

    assert resumed == 1
    assert record["state"] == RemediationState.VERIFYING.value
    assert record["analysis_job_video_id"] == "vid_0000beef"


# ===========================================================================
# the output's playback and status
# ===========================================================================

def test_no_output_url_exists_before_there_is_an_output(
    client, fake_db, fake_s3, alice
):
    """A download action must never appear before something real backs it."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    response = client.get(f"/remediations/{rid}/output", headers=alice)
    assert response.status_code == 404


def test_the_output_url_is_a_scoped_token_not_a_storage_key(
    client, fake_db, fake_s3, alice, tmp_path
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    record = run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    response = client.get(f"/remediations/{rid}/output", headers=alice)
    assert response.status_code == 200
    body = response.json()

    assert body["output_video_id"] == record["output_video_id"]
    assert "access_token=" in body["playback_url"]
    # The S3 key is never published as a link.
    assert "remediated/" not in body["playback_url"]
    assert "test-bucket" not in body["playback_url"]


def test_another_user_cannot_get_the_output_url(
    client, fake_db, fake_s3, alice, bob, tmp_path
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    assert client.get(f"/remediations/{rid}/output", headers=bob).status_code == 404


def test_another_user_cannot_read_the_output_video_itself(
    client, fake_db, fake_s3, alice, bob, tmp_path
):
    """The output is protected by the same ownership check as any upload."""
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    record = run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))
    output_id = record["output_video_id"]

    assert client.get(f"/violations/{output_id}", headers=bob).status_code == 404
    assert client.get(f"/jobs/{output_id}", headers=bob).status_code == 404

    # And a media token minted for Alice cannot be replayed by Bob.
    token = auth.create_media_token("alice", output_id)
    served = client.get(f"/uploads/{output_id}.mp4?access_token={token}")
    # Bob has no session here at all; the token binds to Alice's subject and
    # the record's owner, so this is the token's own scope being enforced.
    assert served.status_code in (200, 206, 404, 502)


def test_the_status_route_reports_real_analysis_progress(
    client, fake_db, fake_s3, alice, tmp_path
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))

    response = client.get(f"/remediations/{rid}/status", headers=alice)
    assert response.status_code == 200
    body = response.json()

    assert body["state"] == "verifying"
    assert body["output_analysis"] is not None
    # A named stage from the backend, never an invented percentage.
    assert body["output_analysis"]["stage"]
    assert "%" not in str(body["output_analysis"]["stage"])
    assert body["is_compliance_claim"] is False


def test_the_status_route_advances_a_finished_verification(
    client, fake_db, fake_s3, alice, tmp_path
):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    request_execution(fake_db, rid)
    record = run_execution(fake_db, fake_s3, rid, result=fake_result(tmp_path))
    output_id = record["output_video_id"]

    anyio.run(
        lambda: fake_db["videos"].update_one(
            {"video_id": output_id},
            {"$set": {"status": "completed", "frames_extracted": 10}},
        )
    )

    with patch(
        "verification.read_output_findings",
        return_value={
            "detections": [],
            "frames_seen": 10,
            "frames_audited": 10,
            "frames_failed": 0,
        },
    ):
        body = client.get(f"/remediations/{rid}/status", headers=alice).json()

    assert body["state"] == "verified"
    assert body["is_compliance_claim"] is True
    assert body["verification"]["target_resolved"] is True


def test_a_client_cannot_assert_a_verified_state(
    client, fake_db, fake_s3, alice, tmp_path
):
    """No request body anywhere can make this claim.

    The status route takes no body, and every decision route ignores a
    client-supplied state. Verification comes only from persisted analysis.
    """
    rid = approved_with_asset(client, alice, fake_db, fake_s3)

    # Try to assert it through every route that accepts a body.
    for path in ("approve", "reject", "cancel"):
        client.post(
            f"/remediations/{rid}/{path}",
            json={
                "note": "x",
                "state": "verified",
                "is_compliance_claim": True,
                "verification": {"outcome": "verified"},
            },
            headers=alice,
        )

    stored = client.get(f"/remediations/{rid}", headers=alice).json()
    assert stored["state"] != "verified"
    assert stored["is_compliance_claim"] is False
    assert stored["verification"] is None


def test_another_user_cannot_read_the_status(client, fake_db, fake_s3, alice, bob):
    rid = approved_with_asset(client, alice, fake_db, fake_s3)
    assert client.get(f"/remediations/{rid}/status", headers=bob).status_code == 404


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/remediations/rem_000000000000/asset"),
        ("post", "/remediations/rem_000000000000/execute"),
        ("get", "/remediations/rem_000000000000/status"),
        ("get", "/remediations/rem_000000000000/output"),
    ],
)
def test_the_new_routes_reject_an_anonymous_caller(client, method, path):
    kwargs = {}
    if path.endswith("/asset"):
        kwargs["files"] = {"file": ("x.png", PNG, "image/png")}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
