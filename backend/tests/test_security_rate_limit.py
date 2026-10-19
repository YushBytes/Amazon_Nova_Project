"""Phase 3.5: rate limiting on the credential endpoints.

Reference: `docs/SECURITY_IMPLEMENTATION_PLAN.md` item 36 — `/auth/login` and
`/auth/signup` were unthrottled, so credential stuffing was bounded only by
network speed.

These tests pin the behaviour that matters:
  * requests within the limit behave exactly as before;
  * exceeding the limit returns 429 with a usable Retry-After;
  * a MALFORMED request still consumes the budget (the decorator-based
    approach fails this, which is why the limiter is middleware);
  * the budget recovers once the window passes;
  * budgets are per-client and per-path, not global;
  * `X-Forwarded-For` is ignored unless trusted proxies are configured.

Time is controlled by monkeypatching `time.time` rather than sleeping, so the
window-recovery test is deterministic and fast.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import app as app_module
import config
from rate_limit import (
    LOGIN_PATH,
    SIGNUP_PATH,
    RateLimitMiddleware,
    build_credential_rules,
    client_identifier,
    parse_limits,
    redact_query_string,
)

GOOD_PASSWORD = "a-sufficiently-long-password"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find_limiter(application) -> RateLimitMiddleware | None:
    """Reach the live middleware instance so a test can reset its counters."""
    node = application.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    return None


@pytest.fixture
def client():
    """A client whose rate-limit counters start empty."""
    test_client = TestClient(app_module.app, raise_server_exceptions=False)
    # Build the middleware stack, then clear any counters from other tests.
    with test_client:
        limiter = _find_limiter(app_module.app)
        if limiter is not None:
            limiter.reset()
        yield test_client


def _mini_app(rules, trusted_proxy_count=0, enabled=True):
    """A minimal app with the same middleware, for isolated behaviour tests."""

    class Body(BaseModel):
        username: str = Field(min_length=3)
        password: str

    application = FastAPI()

    @application.post(LOGIN_PATH)
    async def login(body: Body):
        return {"ok": True}

    @application.post(SIGNUP_PATH)
    async def signup(body: Body):
        return {"ok": True}

    application.add_middleware(
        RateLimitMiddleware,
        rules=rules,
        trusted_proxy_count=trusted_proxy_count,
        enabled=enabled,
    )
    return application


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def test_limits_are_configurable(monkeypatch):
    config.reset_settings_cache()
    monkeypatch.setenv("JWT_SECRET_KEY", "config-test-key-long-enough-0123456789abc")
    monkeypatch.setenv("RATE_LIMIT_LOGIN", "7/minute")
    monkeypatch.setenv("RATE_LIMIT_SIGNUP", "2/hour")

    settings = config.Settings()
    assert settings.rate_limit_login == "7/minute"
    assert settings.rate_limit_signup == "2/hour"
    config.reset_settings_cache()


def test_defaults_are_stricter_for_login_than_for_general_traffic():
    """Login must be the most tightly bounded endpoint.

    There is no global limit on authenticated API traffic, so "stricter than
    ordinary traffic" means these are the only throttled routes; this test
    pins that login is at least as strict as signup per minute and that both
    are small.
    """
    login = parse_limits(config.DEFAULT_RATE_LIMIT_LOGIN)
    signup = parse_limits(config.DEFAULT_RATE_LIMIT_SIGNUP)

    login_per_minute = next(i for i in login if i.get_expiry() == 60)
    signup_per_minute = next(i for i in signup if i.get_expiry() == 60)

    assert login_per_minute.amount <= 10
    assert signup_per_minute.amount <= login_per_minute.amount
    # An hourly ceiling must exist so a slow attacker cannot walk around the
    # per-minute budget.
    assert any(i.get_expiry() == 3600 for i in login)


def test_malformed_limit_string_fails_at_startup(monkeypatch):
    config.reset_settings_cache()
    monkeypatch.setenv("JWT_SECRET_KEY", "config-test-key-long-enough-0123456789abc")
    monkeypatch.setenv("RATE_LIMIT_LOGIN", "not-a-limit")
    with pytest.raises(config.ConfigurationError):
        config.Settings()
    config.reset_settings_cache()


def test_limiter_can_be_disabled(monkeypatch):
    config.reset_settings_cache()
    monkeypatch.setenv("JWT_SECRET_KEY", "config-test-key-long-enough-0123456789abc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    assert config.Settings().rate_limit_enabled is False
    config.reset_settings_cache()


# ---------------------------------------------------------------------------
# the live application
# ---------------------------------------------------------------------------

def test_login_within_the_limit_behaves_normally(client, fake_db):
    """Requests under the limit must reach the handler unchanged.

    An unknown user still gets the usual 401, not a 429.
    """
    response = client.post(
        LOGIN_PATH, json={"username": "nobody", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 401
    assert "Retry-After" not in response.headers


def test_signup_within_the_limit_still_creates_an_account(client, fake_db):
    response = client.post(
        SIGNUP_PATH, json={"username": "ratefriendly", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["role"] == config.DEFAULT_SIGNUP_ROLE


def test_repeated_failed_logins_are_eventually_throttled(client, fake_db):
    """The real defence: brute force must stop before the budget is unbounded."""
    statuses = [
        client.post(
            LOGIN_PATH, json={"username": "victim", "password": f"guess-{i}-long"}
        ).status_code
        for i in range(12)
    ]

    assert 429 in statuses, f"login was never throttled: {statuses}"
    # Everything before the first 429 is a normal auth failure.
    first_429 = statuses.index(429)
    assert set(statuses[:first_429]) <= {401}
    # And once throttled it stays throttled within the window.
    assert set(statuses[first_429:]) == {429}


def test_throttled_response_carries_retry_after_and_no_store(client, fake_db):
    for _ in range(12):
        response = client.post(
            LOGIN_PATH, json={"username": "victim2", "password": GOOD_PASSWORD}
        )
        if response.status_code == 429:
            break

    assert response.status_code == 429
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 1
    assert response.headers.get("X-RateLimit-Remaining") == "0"
    assert response.headers.get("Cache-Control") == "no-store"
    # The body must not hint at whether the account exists.
    body = response.json()["detail"].lower()
    assert "too many requests" in body
    for leak in ("password", "username", "victim2"):
        assert leak not in body


def test_signup_throttling_is_independent_of_login(client, fake_db):
    """Exhausting signup must not lock out login, and vice versa."""
    for i in range(12):
        client.post(
            SIGNUP_PATH, json={"username": f"flood{i}", "password": GOOD_PASSWORD}
        )

    # Signup is now throttled...
    assert (
        client.post(
            SIGNUP_PATH, json={"username": "flooded", "password": GOOD_PASSWORD}
        ).status_code
        == 429
    )
    # ...but login has its own budget.
    assert client.post(
        LOGIN_PATH, json={"username": "someone", "password": GOOD_PASSWORD}
    ).status_code == 401


def test_throttling_does_not_affect_other_routes(client, fake_db):
    for _ in range(12):
        client.post(LOGIN_PATH, json={"username": "v", "password": GOOD_PASSWORD})

    # /status is not a limited path.
    assert client.get("/status").status_code == 200


# ---------------------------------------------------------------------------
# the bypass this implementation exists to prevent
# ---------------------------------------------------------------------------

def test_malformed_requests_consume_the_limit(client, fake_db):
    """A body that fails validation must still count.

    With slowapi's `@limiter.limit` decorator these return 422 *without*
    consuming the budget, because the decorator runs after validation — an
    attacker then has an unlimited channel through the same route. Verified
    against this stack before choosing middleware instead.
    """
    malformed = [
        client.post(LOGIN_PATH, json={"username": "x"}).status_code
        for _ in range(12)
    ]

    assert 429 in malformed, (
        f"malformed requests bypassed the limiter entirely: {malformed}"
    )


def test_non_json_body_also_consumes_the_limit(client, fake_db):
    statuses = [
        client.post(
            LOGIN_PATH, content=b"not json at all", headers={"Content-Type": "application/json"}
        ).status_code
        for _ in range(12)
    ]
    assert 429 in statuses


def test_empty_body_also_consumes_the_limit(client, fake_db):
    statuses = [client.post(LOGIN_PATH).status_code for _ in range(12)]
    assert 429 in statuses


def test_trailing_slash_cannot_evade_the_rule(fake_db):
    """"/auth/login/" is a distinct path to Starlette; it must still be limited."""
    limiter_app = _mini_app({("POST", LOGIN_PATH): "2/minute"})
    with TestClient(limiter_app, raise_server_exceptions=False) as test_client:
        statuses = [
            test_client.post(
                LOGIN_PATH + "/", json={"username": "abc", "password": "p"}
            ).status_code
            for _ in range(5)
        ]
    assert 429 in statuses, f"trailing slash evaded the limiter: {statuses}"


# ---------------------------------------------------------------------------
# recovery after the window
# ---------------------------------------------------------------------------

def test_budget_recovers_after_the_window(monkeypatch):
    """Once the window passes, a legitimate user can log in again.

    Time is advanced by patching the clock the limiter reads, so this is
    deterministic and does not sleep.
    """
    limiter_app = _mini_app({("POST", LOGIN_PATH): "2/minute"})
    body = {"username": "abc", "password": "p"}

    with TestClient(limiter_app, raise_server_exceptions=False) as test_client:
        assert test_client.post(LOGIN_PATH, json=body).status_code == 200
        assert test_client.post(LOGIN_PATH, json=body).status_code == 200
        assert test_client.post(LOGIN_PATH, json=body).status_code == 429

        # Advance past the one-minute window.
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 61)

        assert test_client.post(LOGIN_PATH, json=body).status_code == 200, (
            "the limiter did not release the budget after the window elapsed"
        )


def test_disabled_limiter_passes_everything_through():
    limiter_app = _mini_app({("POST", LOGIN_PATH): "1/minute"}, enabled=False)
    body = {"username": "abc", "password": "p"}
    with TestClient(limiter_app, raise_server_exceptions=False) as test_client:
        statuses = [
            test_client.post(LOGIN_PATH, json=body).status_code for _ in range(5)
        ]
    assert statuses == [200] * 5


# ---------------------------------------------------------------------------
# client identity and forwarding headers
# ---------------------------------------------------------------------------

def _scope(peer: str, forwarded: str | None = None):
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return {"type": "http", "client": (peer, 12345), "headers": headers}


def test_forwarded_header_is_ignored_without_trusted_proxies():
    """The default must not trust a client-supplied header.

    Otherwise an attacker rotates X-Forwarded-For and gets a brand new bucket
    for every request, defeating the limit completely.
    """
    key = client_identifier(_scope("10.0.0.1", "1.2.3.4"), trusted_proxy_count=0)
    assert key == "10.0.0.1"


def test_spoofed_forwarded_header_cannot_mint_new_buckets(fake_db):
    """End-to-end: rotating X-Forwarded-For must not evade the limit."""
    limiter_app = _mini_app({("POST", LOGIN_PATH): "2/minute"}, trusted_proxy_count=0)
    body = {"username": "abc", "password": "p"}

    with TestClient(limiter_app, raise_server_exceptions=False) as test_client:
        statuses = [
            test_client.post(
                LOGIN_PATH, json=body, headers={"X-Forwarded-For": f"9.9.9.{i}"}
            ).status_code
            for i in range(5)
        ]

    assert 429 in statuses, f"header rotation evaded the limiter: {statuses}"


def test_forwarded_header_is_read_from_the_right_when_configured():
    """With one trusted proxy, the last entry is the one our proxy wrote.

    The leftmost entry is attacker-controlled and must not be used.
    """
    key = client_identifier(
        _scope("10.0.0.1", "1.2.3.4, 203.0.113.9"), trusted_proxy_count=1
    )
    assert key == "203.0.113.9"


def test_short_forwarded_chain_falls_back_to_the_peer():
    """A chain shorter than the configured depth means something is wrong."""
    key = client_identifier(_scope("10.0.0.1", "1.2.3.4"), trusted_proxy_count=3)
    assert key == "10.0.0.1"


def test_missing_forwarded_header_falls_back_to_the_peer():
    key = client_identifier(_scope("10.0.0.1", None), trusted_proxy_count=1)
    assert key == "10.0.0.1"


def test_different_clients_have_independent_budgets(fake_db):
    limiter_app = _mini_app({("POST", LOGIN_PATH): "2/minute"})
    body = {"username": "abc", "password": "p"}

    # TestClient reports a fixed peer address, so exercise the key function
    # directly to prove separation.
    middleware = RateLimitMiddleware(
        limiter_app, rules={("POST", LOGIN_PATH): "2/minute"}
    )
    item = parse_limits("2/minute")[0]

    assert middleware.limiter.hit(item, "1.1.1.1", LOGIN_PATH) is True
    assert middleware.limiter.hit(item, "1.1.1.1", LOGIN_PATH) is True
    assert middleware.limiter.hit(item, "1.1.1.1", LOGIN_PATH) is False
    # A different client is unaffected.
    assert middleware.limiter.hit(item, "2.2.2.2", LOGIN_PATH) is True


# ---------------------------------------------------------------------------
# the limiter must not log credentials
# ---------------------------------------------------------------------------

def test_throttle_logging_does_not_include_credentials(client, fake_db, caplog):
    with caplog.at_level("DEBUG"):
        for _ in range(12):
            client.post(
                LOGIN_PATH,
                json={"username": "logvictim", "password": "super-secret-password"},
            )

    text = caplog.text
    assert "super-secret-password" not in text
    assert "logvictim" not in text
    # It should still record that throttling happened, for operations.
    assert "Rate limit exceeded" in text


# ---------------------------------------------------------------------------
# query-string redaction helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/uploads/vid_abcd1234.mp4", "/uploads/vid_abcd1234.mp4"),
        (
            "/uploads/vid_abcd1234.mp4?access_token=eyJhbGciOi.secret.sig",
            "/uploads/vid_abcd1234.mp4?access_token=REDACTED",
        ),
        (
            "/uploads/x.mp4?access_token=abc&t=12",
            "/uploads/x.mp4?access_token=REDACTED&t=12",
        ),
        ("/violations/vid_1?page=2", "/violations/vid_1?page=2"),
        ("/x?token=abc", "/x?token=REDACTED"),
        ("/x?signature=abc", "/x?signature=REDACTED"),
    ],
)
def test_redact_query_string(raw, expected):
    assert redact_query_string(raw) == expected


def test_redaction_keeps_the_token_value_out_entirely():
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    out = redact_query_string(f"/uploads/vid_abcd1234.mp4?access_token={token}")
    assert token not in out
    assert "payload" not in out
