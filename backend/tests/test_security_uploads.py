"""Phase 3: upload validation, filename safety, and media access control.

Audit reference: docs/SECURITY_AUDIT.md finding S7.

The original `/upload-reel` took the stored file's extension straight from the
client filename (`file.filename.split('.')[-1]`) with no allowlist, no size
limit, no auth, and no content check, and `uploads/` was then served to the
world by a `StaticFiles` mount. That combination let an unauthenticated caller
host arbitrary content - including HTML that executes as script - on the
application's own origin.

Note on the earlier audit wording: this sink does NOT permit directory escape,
because the extracted fragment is always appended after `{video_id}.` and so
stays relative to the upload directory. The real defect was unrestricted
extension control plus public serving. Both are covered below.

All filesystem writes go to a per-test temporary directory. No AWS call is made.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import video_repo

GOOD_PASSWORD = "a-sufficiently-long-password"


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """Point the app's upload directory at a temporary location."""
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", target.resolve())
    return target.resolve()


@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def alice():
    return {"Authorization": f"Bearer {auth.create_access_token('alice', 'client')}"}


def _upload(client, headers, filename, content=b"fake-video-bytes", content_type="video/mp4"):
    return client.post(
        "/upload-reel",
        files={"file": (filename, content, content_type)},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_upload_requires_authentication(client, upload_dir, fake_db):
    response = _upload(client, None, "clip.mp4")
    assert response.status_code == 401
    assert not list(upload_dir.iterdir()), "an anonymous upload reached the disk"


# ---------------------------------------------------------------------------
# Extension allowlist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["clip.mp4", "clip.MOV", "a.webm", "b.mkv", "c.avi"])
def test_supported_video_extensions_are_accepted(
    client, upload_dir, fake_db, alice, filename
):
    response = _upload(client, alice, filename)
    assert response.status_code == 200, response.text
    body = response.json()
    # The stored name is server-generated, lowercase, and carries the
    # normalised extension.
    assert body["filename"].startswith("vid_")
    assert body["filename"].split(".")[-1] == filename.split(".")[-1].lower()


@pytest.mark.parametrize(
    "filename",
    [
        "payload.html",      # would execute as script if served from our origin
        "payload.svg",       # SVG can carry script too
        "script.js",
        "shell.py",
        "config.env",
        "archive.zip",
        "doc.pdf",
        "image.png",
    ],
)
def test_disallowed_extensions_are_rejected(
    client, upload_dir, fake_db, alice, filename
):
    response = _upload(client, alice, filename)
    assert response.status_code == 400, f"{filename} was accepted"
    assert not list(upload_dir.iterdir()), f"{filename} reached the disk"


@pytest.mark.parametrize(
    "filename",
    [
        "a.mp4/../../evil.py",
        "evil.py/../../pwned.txt",
        "x.mp4/../../../escaped.html",
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\cmd.exe",
        "a.b/../../../../tmp/x",
        "clip.mp4\x00.py",
    ],
)
def test_path_traversal_shaped_filenames_are_rejected(
    client, upload_dir, fake_db, alice, filename
):
    """These must be refused outright rather than sanitised.

    Even though this sink cannot escape the directory, accepting such a name
    would mean a client-controlled fragment reaching a path expression.
    """
    response = _upload(client, alice, filename)
    assert response.status_code == 400, f"{filename!r} was accepted"


def test_no_file_written_outside_the_upload_directory(
    client, upload_dir, fake_db, alice, tmp_path
):
    """Whatever the client sends, every byte lands inside the upload directory.

    Note that "../../x.mp4" is ACCEPTED: its final suffix is an allowed video
    extension, and the directory fragment is discarded entirely because the
    stored name is server-generated. The security property under test is
    containment, not rejection.
    """
    for name in ["a.mp4/../../evil.py", "../../x.mp4", "evil.py/../../p.txt"]:
        _upload(client, alice, name)

    # Nothing may exist in tmp_path outside the uploads directory itself.
    stray = [
        path
        for path in tmp_path.rglob("*")
        if path != upload_dir and upload_dir not in path.parents
    ]
    assert not stray, f"files written outside the upload directory: {stray}"

    # And everything that was stored carries a safe, server-generated name.
    import re

    for stored in upload_dir.iterdir():
        assert re.fullmatch(r"vid_[0-9a-f]{8}\.mp4", stored.name), stored.name


@pytest.mark.parametrize("filename", ["noextension", "", ".", "..", ".mp4."])
def test_missing_or_degenerate_extension_is_rejected(
    client, upload_dir, fake_db, alice, filename
):
    """Either our 400 or multipart's 422 is acceptable; both refuse the upload."""
    response = _upload(client, alice, filename)
    assert response.status_code in (400, 422), response.status_code
    assert not list(upload_dir.iterdir())


def test_stored_filename_shape_is_server_controlled(
    client, upload_dir, fake_db, alice
):
    """The original name survives only as display metadata."""
    response = _upload(client, alice, "My Holiday Reel (final).mp4")
    assert response.status_code == 200
    body = response.json()

    import re

    assert re.fullmatch(r"vid_[0-9a-f]{8}\.mp4", body["filename"])
    assert body["original_filename"] == "My Holiday Reel (final).mp4"
    # The original name must not appear in the on-disk name.
    assert "Holiday" not in body["filename"]


# ---------------------------------------------------------------------------
# Content type
# ---------------------------------------------------------------------------

def test_mismatched_content_type_is_rejected(client, upload_dir, fake_db, alice):
    response = _upload(
        client, alice, "clip.mp4", content_type="text/html"
    )
    assert response.status_code == 400


def test_octet_stream_is_tolerated(client, upload_dir, fake_db, alice):
    """Browsers send this for some container formats."""
    response = _upload(
        client, alice, "clip.mkv", content_type="application/octet-stream"
    )
    assert response.status_code == 200


def test_content_type_alone_cannot_bypass_the_extension_allowlist(
    client, upload_dir, fake_db, alice
):
    """Claiming video/mp4 must not let a .html file through."""
    response = _upload(
        client, alice, "payload.html", content_type="video/mp4"
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Size and emptiness
# ---------------------------------------------------------------------------

def test_empty_upload_is_rejected(client, upload_dir, fake_db, alice):
    response = _upload(client, alice, "clip.mp4", content=b"")
    assert response.status_code == 400
    assert not list(upload_dir.iterdir())


def test_oversized_upload_is_rejected(client, upload_dir, fake_db, alice, monkeypatch):
    """The limit is enforced while streaming, and the partial file removed."""
    monkeypatch.setattr(app_module.settings, "max_upload_bytes", 1024)

    response = _upload(client, alice, "clip.mp4", content=b"x" * 5000)
    assert response.status_code == 413
    assert not list(upload_dir.iterdir()), "a partial oversized file was left behind"


def test_upload_at_the_limit_is_accepted(client, upload_dir, fake_db, alice, monkeypatch):
    monkeypatch.setattr(app_module.settings, "max_upload_bytes", 4096)
    response = _upload(client, alice, "clip.mp4", content=b"x" * 4096)
    assert response.status_code == 200


def test_malformed_multipart_request_is_rejected(client, upload_dir, fake_db, alice):
    """A missing file part must produce a validation error, not a crash."""
    response = client.post("/upload-reel", headers=alice)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Ownership is recorded, and enforced on read
# ---------------------------------------------------------------------------

def test_upload_records_the_caller_as_owner(client, upload_dir, fake_db, alice):
    import anyio

    body = _upload(client, alice, "clip.mp4").json()

    record = anyio.run(video_repo.get_video, body["video_id"])
    assert record["owner"] == "alice"
    assert record["stored_filename"] == body["filename"]
    assert record["size_bytes"] > 0


def test_owner_can_fetch_their_own_video(client, upload_dir, fake_db, alice):
    body = _upload(client, alice, "clip.mp4").json()
    # The upload response carries a scoped media token in the URL.
    path = body["video_url"].split("/uploads/", 1)[1]

    response = client.get(f"/uploads/{path}")
    assert response.status_code == 200
    assert response.content == b"fake-video-bytes"
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_media_without_a_token_is_refused(client, upload_dir, fake_db, alice):
    """Replaces the old public StaticFiles mount."""
    body = _upload(client, alice, "clip.mp4").json()
    response = client.get(f"/uploads/{body['filename']}")
    assert response.status_code == 401


def test_another_users_media_token_cannot_read_this_video(
    client, upload_dir, fake_db, alice
):
    body = _upload(client, alice, "clip.mp4").json()
    mallory_token = auth.create_media_token("mallory", body["video_id"])

    response = client.get(
        f"/uploads/{body['filename']}?access_token={mallory_token}"
    )
    assert response.status_code == 404


def test_media_token_for_one_video_cannot_read_another(
    client, upload_dir, fake_db, alice
):
    first = _upload(client, alice, "one.mp4").json()
    second = _upload(client, alice, "two.mp4").json()

    token_for_first = auth.create_media_token("alice", first["video_id"])
    response = client.get(
        f"/uploads/{second['filename']}?access_token={token_for_first}"
    )
    assert response.status_code == 404


def test_session_token_is_not_accepted_as_a_media_token(
    client, upload_dir, fake_db, alice
):
    """Scope separation at the HTTP layer."""
    body = _upload(client, alice, "clip.mp4").json()
    session_token = auth.create_access_token("alice", "client")

    response = client.get(
        f"/uploads/{body['filename']}?access_token={session_token}"
    )
    assert response.status_code == 401


def test_media_path_traversal_is_refused(client, upload_dir, fake_db, alice):
    token = auth.create_media_token("alice", "vid_abcd1234")
    for probe in ["../config.py", "..%2fconfig.py", "vid_abcd1234.mp4/../../x"]:
        response = client.get(f"/uploads/{probe}?access_token={token}")
        assert response.status_code == 404, f"{probe} -> {response.status_code}"


def test_upload_rolls_back_if_ownership_cannot_be_recorded(
    client, upload_dir, fake_db, alice, monkeypatch
):
    """An unattributable file must not be left on disk."""

    async def boom(**_kwargs):
        raise RuntimeError("mongo unavailable")

    monkeypatch.setattr(video_repo, "record_video", boom)
    monkeypatch.setattr(app_module.video_repo, "record_video", boom)

    response = _upload(client, alice, "clip.mp4")
    assert response.status_code == 500
    assert not list(upload_dir.iterdir()), "orphaned file left after a failed upload"


def test_error_responses_do_not_leak_internals(
    client, upload_dir, fake_db, alice, monkeypatch
):
    """A server-side failure must not return the exception text."""

    async def boom(**_kwargs):
        raise RuntimeError("mongo://secret-host/internal-detail")

    monkeypatch.setattr(app_module.video_repo, "record_video", boom)

    response = _upload(client, alice, "clip.mp4")
    assert "secret-host" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
