"""Phase 3: public registration must not grant privileged roles.

Audit reference: docs/SECURITY_AUDIT.md finding S3.

`POST /auth/signup` previously took the role straight from the request body and
only checked that it was one of {"client", "agent"}. Any anonymous visitor
could therefore create an "agent" account, which is the privileged role: agents
accept projects from the marketplace and obtain pre-signed S3 upload URLs.

The server now always assigns the least-privileged role and ignores any role in
the body. These tests also confirm that existing privileged accounts are not
downgraded, and that the legitimate frontend flow still works.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import auth
import config

GOOD_PASSWORD = "a-sufficiently-long-password"


@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Escalation attempts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "requested_role",
    ["agent", "admin", "superuser", "root", "moderator", "AGENT", "Agent"],
)
def test_signup_never_grants_a_requested_privileged_role(
    client, fake_db, requested_role
):
    response = client.post(
        "/auth/signup",
        json={
            "username": f"esc_{requested_role.lower()}",
            "password": GOOD_PASSWORD,
            "role": requested_role,
        },
    )

    assert response.status_code == 200, response.text
    # The response reports the role that was actually assigned.
    assert response.json()["role"] == config.DEFAULT_SIGNUP_ROLE


@pytest.mark.parametrize(
    "requested_role",
    ["agent", "admin", "superuser", "root", "moderator"],
)
async def test_stored_record_has_the_default_role(fake_db, requested_role):
    from auth import SignupRequest, signup

    username = f"stored_{requested_role}"
    await signup(
        SignupRequest(
            username=username, password=GOOD_PASSWORD, role=requested_role
        )
    )

    stored = await fake_db["users"].find_one({"username": username})
    assert stored["role"] == config.DEFAULT_SIGNUP_ROLE, (
        "a client-supplied role reached persistent storage"
    )


def test_issued_token_carries_the_default_role_not_the_requested_one(
    client, fake_db
):
    """The escalation must not survive into the token's claims."""
    response = client.post(
        "/auth/signup",
        json={
            "username": "tokencheck",
            "password": GOOD_PASSWORD,
            "role": "agent",
        },
    )
    token = response.json()["access_token"]

    claims = auth.jwt.decode(
        token,
        config.get_settings().jwt_secret,
        algorithms=[config.get_settings().jwt_algorithm],
    )
    assert claims["role"] == config.DEFAULT_SIGNUP_ROLE


def test_escalated_signup_cannot_reach_agent_only_routes(client, fake_db):
    """End-to-end: ask for agent at signup, then try an agent-only route."""
    signup_response = client.post(
        "/auth/signup",
        json={"username": "wannabeagent", "password": GOOD_PASSWORD, "role": "agent"},
    )
    token = signup_response.json()["access_token"]

    denied = client.patch(
        "/projects/11111111-2222-3333-4444-555555555555/accept",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403


def test_extra_unexpected_body_fields_are_ignored(client, fake_db):
    """Unknown fields must not influence account creation."""
    response = client.post(
        "/auth/signup",
        json={
            "username": "extrafields",
            "password": GOOD_PASSWORD,
            "role": "agent",
            "is_admin": True,
            "permissions": ["*"],
            "created_at": "1999-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    assert response.json()["role"] == config.DEFAULT_SIGNUP_ROLE


# ---------------------------------------------------------------------------
# Legitimate behaviour is preserved
# ---------------------------------------------------------------------------

def test_normal_signup_without_a_role_works(client, fake_db):
    """The frontend may omit the field entirely."""
    response = client.post(
        "/auth/signup", json={"username": "plainuser", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["role"] == config.DEFAULT_SIGNUP_ROLE


def test_signup_requesting_client_role_works(client, fake_db):
    """The existing frontend posts role="client" by default."""
    response = client.post(
        "/auth/signup",
        json={"username": "clientuser", "password": GOOD_PASSWORD, "role": "client"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "client"


def test_duplicate_username_is_rejected(client, fake_db):
    first = client.post(
        "/auth/signup", json={"username": "dupe", "password": GOOD_PASSWORD}
    )
    assert first.status_code == 200

    second = client.post(
        "/auth/signup", json={"username": "dupe", "password": GOOD_PASSWORD}
    )
    assert second.status_code == 400


async def test_existing_agent_account_is_not_downgraded(fake_db):
    """An operator-granted agent role must survive login untouched."""
    from auth import LoginRequest, login

    await fake_db["users"].insert_one(
        {
            "username": "realagent",
            "password": auth.hash_password(GOOD_PASSWORD),
            "role": "agent",
        }
    )

    result = await login(
        LoginRequest(username="realagent", password=GOOD_PASSWORD)
    )
    assert result.role == "agent"

    stored = await fake_db["users"].find_one({"username": "realagent"})
    assert stored["role"] == "agent"


def test_operator_granted_agent_can_use_agent_routes(client, fake_db):
    """The privileged path still works when the role comes from storage."""
    import anyio

    async def seed():
        await fake_db["users"].insert_one(
            {
                "username": "grantedagent",
                "password": auth.hash_password(GOOD_PASSWORD),
                "role": "agent",
            }
        )

    anyio.run(seed)

    logged_in = client.post(
        "/auth/login",
        json={"username": "grantedagent", "password": GOOD_PASSWORD},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["role"] == "agent"


async def test_login_role_comes_from_storage_not_the_request(fake_db):
    """A role in the login body must be ignored."""
    from auth import LoginRequest, login

    await fake_db["users"].insert_one(
        {
            "username": "roleliar",
            "password": auth.hash_password(GOOD_PASSWORD),
            "role": "client",
        }
    )

    result = await login(
        LoginRequest(
            username="roleliar", password=GOOD_PASSWORD, role="agent"
        )
    )
    assert result.role == "client", "the request body influenced the session role"


# ---------------------------------------------------------------------------
# /auth/me reflects stored data
# ---------------------------------------------------------------------------

def test_me_returns_the_stored_role_not_the_token_role(client, fake_db):
    """A stale token must not keep granting a role the account no longer has.

    A token is minted with role=agent, but the stored account says client;
    /auth/me must report the stored value.
    """
    import anyio

    async def seed():
        await fake_db["users"].insert_one(
            {
                "username": "demoted",
                "password": auth.hash_password(GOOD_PASSWORD),
                "role": "client",
            }
        )

    anyio.run(seed)

    stale_token = auth.create_access_token("demoted", "agent")
    response = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {stale_token}"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "client"


def test_me_rejects_a_token_for_a_deleted_account(client, fake_db):
    token = auth.create_access_token("ghost", "client")
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
