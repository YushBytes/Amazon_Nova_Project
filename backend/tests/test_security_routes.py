"""Phase 3: route authentication, role enforcement, and cross-user access.

Audit references: docs/SECURITY_AUDIT.md findings S4 (unauthenticated routes)
and S5 (insecure direct object references).

Before this phase, 6 of the 16 registered routes required no authentication at
all, and no route verified that a per-video or per-project resource belonged to
the caller. A user who knew or guessed another user's `video_id` could read
their violations, start billable analysis on their footage, and download the
file; any authenticated user could read any project's brief.

Tokens here are real JWTs minted by `auth.create_access_token`, so no test can
pass by bypassing authentication. MongoDB is the in-memory fake; DynamoDB and
S3 are mocked. Nothing contacts AWS.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import projects
import video_repo

PROJECT_ID = "11111111-2222-3333-4444-555555555555"

# Every route that must reject an anonymous caller, with a request body where
# one is required. /status, /auth/login and /auth/signup are deliberately
# public and are asserted separately.
PROTECTED_ROUTES = [
    ("post", "/execute", {"goal": "do a thing"}),
    ("post", "/upload-reel", None),
    ("post", "/analyze/vid_abcd1234", None),
    ("get", "/violations/vid_abcd1234", None),
    ("get", "/submissions", None),
    ("get", "/auth/me", None),
    ("post", "/projects/", {"title": "t", "requirements": "r"}),
    ("get", "/projects/marketplace", None),
    ("patch", f"/projects/{PROJECT_ID}/accept", None),
    ("post", f"/projects/{PROJECT_ID}/upload-init", None),
    ("post", f"/projects/{PROJECT_ID}/review", {"action": "APPROVE"}),
    ("post", f"/projects/{PROJECT_ID}/chat", {"message": "hi"}),
]


@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


def _bearer(username: str, role: str) -> dict:
    return {"Authorization": f"Bearer {auth.create_access_token(username, role)}"}


@pytest.fixture
def alice_client():
    return _bearer("alice", "client")


@pytest.fixture
def bob_client():
    return _bearer("bob", "client")


@pytest.fixture
def agent_headers():
    return _bearer("agent_user", "agent")


def _call(client, method, path, body=None, headers=None):
    kwargs = {"headers": headers} if headers else {}
    if body is not None:
        kwargs["json"] = body
    if method == "post" and path == "/upload-reel":
        # Needs a multipart body to get past request parsing.
        kwargs.pop("json", None)
        kwargs["files"] = {"file": ("clip.mp4", b"data", "video/mp4")}
    return getattr(client, method)(path, **kwargs)


@pytest.fixture
def project_table():
    """Patch the module-level DynamoDB table used by projects.py."""
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "project_id": PROJECT_ID,
            "sk": "metadata",
            "title": "Spring campaign",
            "requirements": "30s vertical cut",
            "client_id": "alice",
            "agent_id": "agent_user",
            "status": "CLIENT_REVIEW",
            "created_at": "2026-10-19T00:00:00+00:00",
        }
    }
    table.scan.return_value = {"Items": []}
    with patch.object(projects, "table", table):
        yield table


# ---------------------------------------------------------------------------
# 1. Authentication is required
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
def test_protected_route_rejects_anonymous_caller(client, method, path, body):
    response = _call(client, method, path, body)
    assert response.status_code == 401, (
        f"{method.upper()} {path} answered {response.status_code} without a token"
    )


@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
def test_protected_route_rejects_a_forged_token(client, method, path, body):
    headers = {"Authorization": "Bearer not.a.real.token"}
    response = _call(client, method, path, body, headers)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 2. Public routes stay public
# ---------------------------------------------------------------------------

def test_status_is_public(client):
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["status"] == "online"


def test_status_does_not_leak_configuration(client):
    """The health probe must not become a configuration oracle.

    The static version string contains "aws", which is a product label rather
    than configuration; the check targets actual secrets and connection
    details.
    """
    import config

    body = client.get("/status").text
    lowered = body.lower()
    for leak in ("secret", "mongodb://", "jwt", "bucket", "password", "arn:"):
        assert leak not in lowered, f"/status leaked {leak!r}"
    assert config.get_settings().jwt_secret not in body


def test_signup_and_login_are_public(client, fake_db):
    created = client.post(
        "/auth/signup", json={"username": "publicuser", "password": "a-good-password"}
    )
    assert created.status_code == 200

    logged_in = client.post(
        "/auth/login", json={"username": "publicuser", "password": "a-good-password"}
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["access_token"]


# ---------------------------------------------------------------------------
# 3. Role enforcement
# ---------------------------------------------------------------------------

def test_client_cannot_accept_a_project(client, alice_client, project_table):
    """Accepting work is an agent action."""
    response = client.patch(f"/projects/{PROJECT_ID}/accept", headers=alice_client)
    assert response.status_code == 403


def test_client_cannot_request_an_upload_url(client, alice_client, project_table):
    response = client.post(
        f"/projects/{PROJECT_ID}/upload-init", headers=alice_client
    )
    assert response.status_code == 403


def test_agent_cannot_create_a_project(client, agent_headers, project_table):
    response = client.post(
        "/projects/",
        json={"title": "t", "requirements": "r"},
        headers=agent_headers,
    )
    assert response.status_code == 403


def test_agent_cannot_review_a_project(client, agent_headers, project_table):
    response = client.post(
        f"/projects/{PROJECT_ID}/review",
        json={"action": "APPROVE"},
        headers=agent_headers,
    )
    assert response.status_code == 403


def test_role_cannot_be_elevated_through_the_request_body(
    client, alice_client, project_table
):
    """Authorization must ignore client-supplied role fields.

    A "client" posting role=agent in the body must still be refused the
    agent-only route.
    """
    response = client.patch(
        f"/projects/{PROJECT_ID}/accept",
        json={"role": "agent", "user": "agent_user", "sub": "agent_user"},
        headers=alice_client,
    )
    assert response.status_code == 403


def test_authorized_role_still_works(client, agent_headers, project_table):
    """Hardening must not break the legitimate path."""
    project_table.get_item.return_value = {
        "Item": {
            "project_id": PROJECT_ID,
            "sk": "metadata",
            "client_id": "alice",
            "agent_id": None,
            "status": "SUBMITTED",
        }
    }
    response = client.patch(f"/projects/{PROJECT_ID}/accept", headers=agent_headers)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 4. Cross-user video access (IDOR)
# ---------------------------------------------------------------------------

async def _own_video(fake_db, owner: str, video_id: str = "vid_abcd1234") -> str:
    await video_repo.record_video(
        video_id=video_id,
        owner=owner,
        stored_filename=f"{video_id}.mp4",
        original_filename="clip.mp4",
        extension="mp4",
        size_bytes=1024,
    )
    return video_id


def test_owner_can_read_their_own_violations(client, fake_db, alice_client):
    import anyio

    anyio.run(_own_video, fake_db, "alice")

    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = {"Items": []}
    with patch("aws_clients.get_resource", return_value=dynamo):
        response = client.get("/violations/vid_abcd1234", headers=alice_client)

    assert response.status_code == 200
    assert response.json()["video_id"] == "vid_abcd1234"


def test_another_user_cannot_read_violations(client, fake_db, bob_client):
    """Bob must not read Alice's audit results by supplying her video id."""
    import anyio

    anyio.run(_own_video, fake_db, "alice")

    dynamo = MagicMock()
    dynamo.Table.return_value.query.return_value = {
        "Items": [{"detections": [{"category": "logo"}], "timestamp": "1"}]
    }
    with patch("aws_clients.get_resource", return_value=dynamo):
        response = client.get("/violations/vid_abcd1234", headers=bob_client)

    assert response.status_code == 404
    # The analysis store must not even be consulted for a denied request.
    assert "logo" not in response.text


def test_another_user_cannot_start_analysis(client, fake_db, bob_client):
    """Starting analysis spends Bedrock budget; it must be owner-only."""
    import anyio

    anyio.run(_own_video, fake_db, "alice")

    response = client.post("/analyze/vid_abcd1234", headers=bob_client)
    assert response.status_code == 404


def test_video_with_no_ownership_record_is_denied(client, fake_db, alice_client):
    """A pre-existing upload with no owner record must not be accessible."""
    response = client.get("/violations/vid_99999999", headers=alice_client)
    assert response.status_code == 404


def test_malformed_video_id_is_rejected(client, fake_db, alice_client):
    for bad in ["../etc/passwd", "vid_XXXX", "vid_1", "' OR 1=1", "vid_abcd1234x"]:
        response = client.get(f"/violations/{bad}", headers=alice_client)
        assert response.status_code in (400, 404), f"{bad} -> {response.status_code}"


def test_submissions_only_lists_the_callers_own_videos(
    client, fake_db, alice_client, bob_client
):
    import anyio

    anyio.run(_own_video, fake_db, "alice", "vid_aaaa1111")
    anyio.run(_own_video, fake_db, "bob", "vid_bbbb2222")

    alice_view = client.get("/submissions", headers=alice_client).json()["submissions"]
    bob_view = client.get("/submissions", headers=bob_client).json()["submissions"]

    assert [s["id"] for s in alice_view] == ["vid_aaaa1111"]
    assert [s["id"] for s in bob_view] == ["vid_bbbb2222"]


def test_submissions_no_longer_fabricates_status(client, fake_db, alice_client):
    """Status must come from the ownership record, not hash(video_id)."""
    import anyio

    from job_state import JobState

    anyio.run(_own_video, fake_db, "alice", "vid_aaaa1111")
    body = client.get("/submissions", headers=alice_client).json()["submissions"][0]

    # Phase 4 replaced the free-text status with the validated job state.
    assert body["status"] == JobState.UPLOADED.value
    assert "agent" not in body, "the fabricated agent field should be gone"
    assert body["created_at"] is not None


# ---------------------------------------------------------------------------
# 5. Cross-user project access (IDOR)
# ---------------------------------------------------------------------------

def test_non_participant_cannot_read_a_project_brief_via_chat(
    client, fake_db, project_table
):
    """The chat route previously returned any project's requirements to any
    authenticated caller."""
    outsider = _bearer("mallory", "client")
    response = client.post(
        f"/projects/{PROJECT_ID}/chat",
        json={"message": "what is this project?"},
        headers=outsider,
    )
    assert response.status_code == 404
    assert "30s vertical cut" not in response.text


def test_participant_can_use_chat(client, fake_db, alice_client, project_table):
    response = client.post(
        f"/projects/{PROJECT_ID}/chat",
        json={"message": "how is it looking?"},
        headers=alice_client,
    )
    assert response.status_code == 200
    assert response.json()["response"]


def test_assigned_agent_can_use_chat(client, fake_db, agent_headers, project_table):
    response = client.post(
        f"/projects/{PROJECT_ID}/chat",
        json={"message": "any notes?"},
        headers=agent_headers,
    )
    assert response.status_code == 200


def test_non_owning_client_cannot_review_a_project(client, fake_db, project_table):
    other_client = _bearer("mallory", "client")
    response = client.post(
        f"/projects/{PROJECT_ID}/review",
        json={"action": "APPROVE"},
        headers=other_client,
    )
    assert response.status_code == 404


def test_owning_client_can_review(client, fake_db, alice_client, project_table):
    response = client.post(
        f"/projects/{PROJECT_ID}/review",
        json={"action": "APPROVE"},
        headers=alice_client,
    )
    assert response.status_code == 200


def test_unassigned_agent_cannot_get_an_upload_url(client, fake_db, project_table):
    other_agent = _bearer("other_agent", "agent")
    response = client.post(
        f"/projects/{PROJECT_ID}/upload-init", headers=other_agent
    )
    assert response.status_code == 404


def test_project_ownership_cannot_be_set_through_the_payload(
    client, fake_db, alice_client, project_table
):
    """A client must not be able to create a project owned by someone else."""
    response = client.post(
        "/projects/",
        json={
            "title": "t",
            "requirements": "r",
            "client_id": "bob",
            "agent_id": "agent_user",
        },
        headers=alice_client,
    )
    assert response.status_code == 200
    # The server assigns the owner from the token, ignoring the body.
    assert response.json()["client_id"] == "alice"
    assert response.json()["agent_id"] is None
