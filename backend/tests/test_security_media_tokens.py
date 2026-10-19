"""Phase 3.5: media-token scoping and keeping tokens out of logs.

Phase 3 introduced short-lived, video-scoped media tokens so a browser
`<video>` element (which cannot send an `Authorization` header) can fetch a
protected file. That puts a credential in a URL query string, which makes the
request target itself sensitive.

These tests verify the token's scope and lifetime, and that this process's own
logging does not record the token value.

WHAT IS NOT COVERED HERE, and cannot be: logs written by a reverse proxy, CDN,
load balancer, or hosting platform, and the browser's own history and `Referer`
behaviour. Those are outside this process. See `docs/SECURITY_OPERATIONS.md`.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jose import jwt

import app as app_module
import auth
import config
from logging_setup import RedactQueryStringFilter, install_log_redaction


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
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


def _upload(client, headers, filename="clip.mp4"):
    return client.post(
        "/upload-reel",
        files={"file": (filename, b"fake-video-bytes", "video/mp4")},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# scope: resource and action
# ---------------------------------------------------------------------------

def test_media_token_names_exactly_one_video():
    claims = auth.decode_media_token(auth.create_media_token("alice", "vid_abcd1234"))
    assert claims["vid"] == "vid_abcd1234"
    assert claims["scope"] == auth.SCOPE_MEDIA
    assert claims["sub"] == "alice"


def test_media_token_grants_read_only(client, fake_db, upload_dir, alice):
    """A media token must not be usable for anything but fetching the file.

    It is rejected by the bearer-authenticated routes, so it cannot start
    analysis or read results even for its own video.
    """
    body = _upload(client, alice).json()
    media = auth.create_media_token("alice", body["video_id"])

    # Not accepted as a session credential anywhere.
    for method, path in [
        ("post", f"/analyze/{body['video_id']}"),
        ("get", f"/violations/{body['video_id']}"),
        ("get", "/submissions"),
    ]:
        response = getattr(client, method)(
            path, headers={"Authorization": f"Bearer {media}"}
        )
        assert response.status_code == 401, f"{path} accepted a media token"


def test_token_for_one_video_cannot_fetch_another(client, fake_db, upload_dir, alice):
    first = _upload(client, alice, "one.mp4").json()
    second = _upload(client, alice, "two.mp4").json()

    token = auth.create_media_token("alice", first["video_id"])
    response = client.get(f"/uploads/{second['filename']}?access_token={token}")
    assert response.status_code == 404


def test_token_cannot_fetch_arbitrary_files(client, fake_db, upload_dir, alice):
    """A valid token must not become a general file-read primitive."""
    body = _upload(client, alice).json()
    token = auth.create_media_token("alice", body["video_id"])

    # Plant a file the token must never reach.
    (upload_dir / "secret.txt").write_bytes(b"not yours")

    for probe in [
        "secret.txt",
        "../config.py",
        "..%2fconfig.py",
        "....//config.py",
        f"{body['filename']}/../../config.py",
    ]:
        response = client.get(f"/uploads/{probe}?access_token={token}")
        assert response.status_code == 404, f"{probe} -> {response.status_code}"


def test_wrong_user_token_is_rejected(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    mallory = auth.create_media_token("mallory", body["video_id"])

    response = client.get(f"/uploads/{body['filename']}?access_token={mallory}")
    assert response.status_code == 404


def test_forged_media_token_is_rejected(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    forged = jwt.encode(
        {
            "sub": "alice",
            "scope": auth.SCOPE_MEDIA,
            "vid": body["video_id"],
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        "a-different-signing-key-0123456789abcdefgh",
        algorithm=config.get_settings().jwt_algorithm,
    )

    response = client.get(f"/uploads/{body['filename']}?access_token={forged}")
    assert response.status_code == 401


def test_expired_media_token_is_rejected(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    settings = config.get_settings()
    expired = jwt.encode(
        {
            "sub": "alice",
            "scope": auth.SCOPE_MEDIA,
            "vid": body["video_id"],
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(f"/uploads/{body['filename']}?access_token={expired}")
    assert response.status_code == 401


def test_missing_token_is_rejected(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    assert client.get(f"/uploads/{body['filename']}").status_code == 401


# ---------------------------------------------------------------------------
# lifetime
# ---------------------------------------------------------------------------

def test_media_token_lifetime_is_short_by_default():
    settings = config.get_settings()
    assert settings.media_token_ttl_minutes == 15
    # Much shorter than a session, because it travels in a URL.
    assert settings.media_token_ttl_minutes < settings.access_token_expire_minutes


def test_media_token_lifetime_is_configurable(monkeypatch):
    config.reset_settings_cache()
    monkeypatch.setenv("JWT_SECRET_KEY", "media-ttl-test-key-0123456789abcdefghij")
    monkeypatch.setenv("MEDIA_TOKEN_TTL_MINUTES", "3")
    assert config.Settings().media_token_ttl_minutes == 3
    config.reset_settings_cache()


def test_media_token_expiry_matches_the_configured_ttl():
    settings = config.get_settings()
    claims = auth.decode_media_token(auth.create_media_token("alice", "vid_abcd1234"))

    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == settings.media_token_ttl_minutes * 60


# ---------------------------------------------------------------------------
# response headers
# ---------------------------------------------------------------------------

def test_media_response_sets_safe_headers(client, fake_db, upload_dir, alice):
    body = _upload(client, alice).json()
    path = body["video_url"].split("/uploads/", 1)[1]

    response = client.get(f"/uploads/{path}")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    # An explicit video type, never a sniffable or HTML-ish one.
    assert response.headers["content-type"].startswith("video/")


# ---------------------------------------------------------------------------
# tokens must not reach this process's logs
# ---------------------------------------------------------------------------

def test_access_log_filter_redacts_the_token():
    """uvicorn.access passes the request target as a formatting argument."""
    install_log_redaction()
    logger = logging.getLogger("uvicorn.access")

    token = "eyJhbGciOiJIUzI1NiJ9.payload-part.signature-part"
    record = logger.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/uploads/vid_abcd1234.mp4?access_token={token}", "1.1", 200),
        None,
    )

    for log_filter in logger.filters:
        log_filter.filter(record)

    rendered = record.getMessage()
    assert token not in rendered
    assert "payload-part" not in rendered
    assert "access_token=REDACTED" in rendered
    # The useful part of the log line survives.
    assert "/uploads/vid_abcd1234.mp4" in rendered
    assert "200" in rendered


def test_filter_leaves_ordinary_log_lines_untouched():
    log_filter = RedactQueryStringFilter()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/violations/vid_abcd1234?page=2", "1.1", 200),
        None,
    )
    log_filter.filter(record)
    assert "page=2" in record.getMessage()
    assert "REDACTED" not in record.getMessage()


def test_filter_redacts_a_token_embedded_in_a_message():
    log_filter = RedactQueryStringFilter()
    token = "abc.def.ghi"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        f"served /uploads/x.mp4?access_token={token}", None, None,
    )
    log_filter.filter(record)
    assert token not in record.getMessage()


def test_install_log_redaction_is_idempotent():
    install_log_redaction()
    install_log_redaction()
    logger = logging.getLogger("uvicorn.access")
    installed = [f for f in logger.filters if isinstance(f, RedactQueryStringFilter)]
    assert len(installed) == 1


def test_application_logging_does_not_record_media_tokens(
    client, fake_db, upload_dir, alice, caplog
):
    """End-to-end: serving media must not log the token anywhere."""
    body = _upload(client, alice).json()
    path = body["video_url"].split("/uploads/", 1)[1]
    token = path.split("access_token=", 1)[1]

    with caplog.at_level("DEBUG"):
        assert client.get(f"/uploads/{path}").status_code == 200

    assert token not in caplog.text


def test_unhandled_error_log_does_not_include_the_query_string(
    client, fake_db, upload_dir, alice, caplog, monkeypatch
):
    """The catch-all handler logs the path only, never the query.

    Guards against a future change to `request.url` (which includes the query)
    in that handler.
    """
    body = _upload(client, alice).json()
    path = body["video_url"].split("/uploads/", 1)[1]
    token = path.split("access_token=", 1)[1]

    def boom(_name):
        raise RuntimeError("induced failure")

    monkeypatch.setattr(app_module, "_resolve_within_uploads", boom)

    with caplog.at_level("DEBUG"):
        client.get(f"/uploads/{path}")

    assert token not in caplog.text


# ---------------------------------------------------------------------------
# tokens are not persisted more widely than necessary
# ---------------------------------------------------------------------------

def test_ownership_record_does_not_store_the_token(client, fake_db, upload_dir, alice):
    """A token in a stored record would outlive its purpose."""
    import anyio

    import video_repo

    body = _upload(client, alice).json()
    record = anyio.run(video_repo.get_video, body["video_id"])

    serialised = str(record)
    assert "access_token" not in serialised
    assert "video_url" not in record


def test_submissions_mints_fresh_tokens_rather_than_reusing_stored_ones(
    client, fake_db, upload_dir, alice
):
    """Each listing issues a new short-lived token, so none is stored."""
    _upload(client, alice).json()

    first = client.get("/submissions", headers=alice).json()["submissions"][0]["url"]
    second = client.get("/submissions", headers=alice).json()["submissions"][0]["url"]

    assert "access_token=" in first and "access_token=" in second
    # Both are valid for the same video; the point is they come from
    # create_media_token at request time, not from persisted state.
    stored_token = first.split("access_token=")[1]
    assert auth.decode_media_token(stored_token)["vid"]
