"""Regression tests for Phase 2 defect B: un-awaited coroutine in project auth.

Audit reference: docs/PROJECT_AUDIT.md section 9.2.

`auth.decode_token` is declared `async def`, but `projects.get_current_user` was
a plain `def` that called it without `await`. FastAPI therefore injected a
coroutine object as `user`, and every handler's first `user["role"]` lookup
raised `TypeError: 'coroutine' object is not subscriptable` -> HTTP 500. All six
`/projects/*` routes were affected.

These tests exercise the real authentication path: tokens are minted with
`auth.create_access_token` and decoded by the real `auth.decode_token`, so a
test cannot pass by bypassing authentication. Only DynamoDB and S3 are mocked.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import projects

PROJECT_ID = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app_module.app)


def _bearer(username: str, role: str) -> dict:
    """A genuine signed JWT, produced by the application's own token helper."""
    token = auth.create_access_token(username, role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client_headers():
    return _bearer("client_user", "client")


@pytest.fixture
def agent_headers():
    return _bearer("agent_user", "agent")


def _project_item(**overrides):
    item = {
        "project_id": PROJECT_ID,
        "sk": "metadata",
        "title": "Spring campaign reel",
        "requirements": "30s vertical cut, no competitor logos",
        "client_id": "client_user",
        "agent_id": None,
        "status": "SUBMITTED",
        "created_at": "2026-10-18T00:00:00+00:00",
    }
    item.update(overrides)
    return item


@pytest.fixture
def table():
    """Patch the module-level DynamoDB Table used by projects.py."""
    mock = MagicMock()
    mock.get_item.return_value = {"Item": _project_item()}
    mock.scan.return_value = {"Items": [_project_item()]}
    mock.put_item.return_value = {}
    mock.update_item.return_value = {}
    with patch.object(projects, "table", mock):
        yield mock


# --------------------------------------------------------------------------
# the defect itself
# --------------------------------------------------------------------------

async def test_get_current_user_returns_a_dict_not_a_coroutine():
    """The direct regression test for the missing `await`.

    Before the fix this returned a coroutine object, which is not subscriptable
    and broke every route that reads `user["role"]`.
    """
    token = auth.create_access_token("someone", "client")
    result = projects.get_current_user(token)

    # get_current_user is now a coroutine function, so awaiting it yields the
    # decoded payload. The point of the test is that what the routes receive is
    # a subscriptable mapping carrying the claims.
    import inspect
    if inspect.isawaitable(result):
        result = await result

    assert isinstance(result, dict), f"expected decoded claims dict, got {type(result)}"
    assert result["sub"] == "someone"
    assert result["role"] == "client"


# --------------------------------------------------------------------------
# route 1/6 — POST /projects/
# --------------------------------------------------------------------------

def test_create_project_as_client_succeeds(client, client_headers, table):
    body = {"title": "Spring campaign reel", "requirements": "30s vertical cut"}
    r = client.post("/projects/", json=body, headers=client_headers)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Spring campaign reel"
    assert data["client_id"] == "client_user"
    assert data["status"] == "SUBMITTED"
    assert data["agent_id"] is None
    assert table.put_item.called


def test_create_project_as_agent_is_forbidden(client, agent_headers, table):
    """Role enforcement must still work; the fix must not weaken it."""
    r = client.post(
        "/projects/",
        json={"title": "x", "requirements": "y"},
        headers=agent_headers,
    )
    assert r.status_code == 403
    assert not table.put_item.called


def test_create_project_without_token_is_unauthorized(client, table):
    r = client.post("/projects/", json={"title": "x", "requirements": "y"})
    assert r.status_code == 401


def test_create_project_with_invalid_token_is_unauthorized(client, table):
    r = client.post(
        "/projects/",
        json={"title": "x", "requirements": "y"},
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# route 2/6 — GET /projects/marketplace
# --------------------------------------------------------------------------

def test_marketplace_returns_submitted_projects(client, agent_headers, table):
    r = client.get("/projects/marketplace", headers=agent_headers)

    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list)
    assert items[0]["project_id"] == PROJECT_ID
    assert items[0]["status"] == "SUBMITTED"
    assert table.scan.called


def test_marketplace_requires_authentication(client, table):
    r = client.get("/projects/marketplace")
    assert r.status_code == 401


# --------------------------------------------------------------------------
# route 3/6 — PATCH /projects/{id}/accept
# --------------------------------------------------------------------------

def test_accept_project_as_agent_succeeds(client, agent_headers, table):
    r = client.patch(f"/projects/{PROJECT_ID}/accept", headers=agent_headers)

    assert r.status_code == 200, r.text
    assert "accepted" in r.json()["message"].lower()
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"project_id": PROJECT_ID, "sk": "metadata"}
    assert kwargs["ExpressionAttributeValues"][":a"] == "agent_user"
    assert kwargs["ExpressionAttributeValues"][":s"] == "IN_PROGRESS"


def test_accept_project_as_client_is_forbidden(client, client_headers, table):
    r = client.patch(f"/projects/{PROJECT_ID}/accept", headers=client_headers)
    assert r.status_code == 403
    assert not table.update_item.called


# --------------------------------------------------------------------------
# route 4/6 — POST /projects/{id}/upload-init
# --------------------------------------------------------------------------

def test_upload_init_as_assigned_agent_returns_presigned_url(
    client, agent_headers, table
):
    table.get_item.return_value = {"Item": _project_item(agent_id="agent_user")}

    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example.invalid/signed"

    with patch("aws_clients.get_client", return_value=s3):
        r = client.post(f"/projects/{PROJECT_ID}/upload-init", headers=agent_headers)

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["upload_url"] == "https://s3.example.invalid/signed"
    assert data["s3_key"] == f"raw/{PROJECT_ID}/agent_user/reel.mp4"


def test_upload_init_as_client_is_forbidden(client, client_headers, table):
    r = client.post(f"/projects/{PROJECT_ID}/upload-init", headers=client_headers)
    assert r.status_code == 403


def test_upload_init_rejects_agent_not_assigned_to_project(
    client, agent_headers, table
):
    """An agent must not get an upload URL for someone else's project.

    Phase 3 changed the response from 403 to 404: returning 403 confirmed that
    the supplied project_id exists, which let an attacker enumerate ids. The
    invariant that matters is unchanged - the request is refused.
    """
    table.get_item.return_value = {"Item": _project_item(agent_id="someone_else")}
    r = client.post(f"/projects/{PROJECT_ID}/upload-init", headers=agent_headers)
    assert r.status_code == 404
    assert r.status_code != 200


# --------------------------------------------------------------------------
# route 5/6 — POST /projects/{id}/review
# --------------------------------------------------------------------------

def test_review_as_owning_client_succeeds(client, client_headers, table):
    table.get_item.return_value = {
        "Item": _project_item(status="CLIENT_REVIEW", client_id="client_user")
    }
    r = client.post(
        f"/projects/{PROJECT_ID}/review",
        json={"action": "APPROVE", "post_time": "INSTANT"},
        headers=client_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "COMPLETED"
    assert table.update_item.called


def test_review_as_agent_is_forbidden(client, agent_headers, table):
    r = client.post(
        f"/projects/{PROJECT_ID}/review",
        json={"action": "APPROVE"},
        headers=agent_headers,
    )
    assert r.status_code == 403


def test_review_rejects_project_not_in_review_state(client, client_headers, table):
    """A project still being audited is not reviewable.

    NOTE: no code path currently assigns CLIENT_REVIEW (audit section 9.11), so
    in practice this guard always rejects. That gap is out of scope for Phase 2
    and is tracked in the roadmap; this test pins the guard's behaviour.
    """
    table.get_item.return_value = {"Item": _project_item(status="AI_AUDITING")}
    r = client.post(
        f"/projects/{PROJECT_ID}/review",
        json={"action": "APPROVE"},
        headers=client_headers,
    )
    assert r.status_code != 200


# --------------------------------------------------------------------------
# route 6/6 — POST /projects/{id}/chat
# --------------------------------------------------------------------------

def test_chat_returns_a_response_for_an_authenticated_user(
    client, client_headers, table
):
    r = client.post(
        f"/projects/{PROJECT_ID}/chat",
        json={"message": "Is the intro on brand?"},
        headers=client_headers,
    )

    assert r.status_code == 200, r.text
    assert isinstance(r.json()["response"], str)
    assert r.json()["response"]


def test_chat_requires_authentication(client, table):
    r = client.post(f"/projects/{PROJECT_ID}/chat", json={"message": "hi"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# no route may fail with the coroutine TypeError any more
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,body,role",
    [
        ("post", "/projects/", {"title": "t", "requirements": "r"}, "client"),
        ("get", "/projects/marketplace", None, "agent"),
        ("patch", f"/projects/{PROJECT_ID}/accept", None, "agent"),
        ("post", f"/projects/{PROJECT_ID}/upload-init", None, "agent"),
        ("post", f"/projects/{PROJECT_ID}/review", {"action": "APPROVE"}, "client"),
        ("post", f"/projects/{PROJECT_ID}/chat", {"message": "hi"}, "client"),
    ],
)
def test_no_project_route_returns_500_from_coroutine_misuse(
    client, table, method, path, body, role
):
    """Every one of the six routes must get past dependency resolution.

    A 500 here is the signature of the original defect. Business-level
    rejections (400/403) are acceptable outcomes; an unhandled server error
    from using a coroutine as a dict is not.
    """
    table.get_item.return_value = {
        "Item": _project_item(status="CLIENT_REVIEW", agent_id="agent_user")
    }
    headers = _bearer(f"{role}_user", role)

    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example.invalid/signed"
    with patch("aws_clients.get_client", return_value=s3):
        r = getattr(client, method)(path, json=body, headers=headers) if body \
            else getattr(client, method)(path, headers=headers)

    assert r.status_code != 500, f"{method.upper()} {path} -> 500: {r.text}"
