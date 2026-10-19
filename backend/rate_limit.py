"""Rate limiting for the credential endpoints.

Phase 3.5. Closes the gap recorded in `docs/SECURITY_IMPLEMENTATION_PLAN.md`
item 36: `/auth/login` and `/auth/signup` were unthrottled, so credential
stuffing and account-enumeration attempts were bounded only by network speed.

Design notes, because two obvious approaches are wrong here:

1. **Middleware, not a decorator.** The usual FastAPI pattern is slowapi's
   `@limiter.limit(...)` decorator on the endpoint. That runs *after* request
   validation, so a request with a malformed body returns 422 without ever
   consuming the limit — an attacker gets unlimited requests through the same
   route. Verified against this stack before choosing. This middleware runs
   before routing and validation, so every request to a limited path is
   counted regardless of whether its body parses.

2. **The peer address, not `X-Forwarded-For`.** Any client can send that
   header, so honouring it without knowing the proxy depth lets an attacker
   mint a fresh bucket per request and bypass the limit entirely. It is used
   only when `TRUSTED_PROXY_COUNT` is explicitly configured, and then the
   entry is counted from the right-hand (proxy-appended) end rather than the
   client-controlled left.

The counting engine is `limits`, the maintained library that backs slowapi.
Storage is in-process memory.

**SCOPE LIMITATION — read before deploying.** `MemoryStorage` is
process-local. With more than one worker or instance, each holds its own
counters and the effective limit is multiplied by the number of processes.
This is honest protection for the current single-process deployment
(`backend/Dockerfile` runs one `uvicorn app:app` with no `--workers`), and it
is NOT distributed protection. `limits` supports Redis and Memcached; moving to
a shared store is a configuration change plus a deployment dependency, and is
deliberately not enabled here because no such store is provisioned. See
`docs/SECURITY_OPERATIONS.md`.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths whose limits are configured individually. Anything not listed here is
# not limited by this middleware.
LOGIN_PATH = "/auth/login"
SIGNUP_PATH = "/auth/signup"


def parse_limits(spec: str) -> list[RateLimitItem]:
    """Turn "5/minute;20/hour" into parsed rate-limit items."""
    return [parse(part.strip()) for part in spec.split(";") if part.strip()]


def client_identifier(request_scope: Scope, trusted_proxy_count: int) -> str:
    """Derive the rate-limit bucket key for a request.

    With `trusted_proxy_count == 0` (the default) this is the peer address and
    `X-Forwarded-For` is ignored completely.

    With a positive count, the address is taken `trusted_proxy_count` entries
    from the right of `X-Forwarded-For`. Proxies append, so the right-hand
    entries are the ones your own infrastructure wrote; the leftmost entry is
    whatever the client claimed and must never be trusted.
    """
    client = request_scope.get("client")
    peer = client[0] if client else "unknown"

    if trusted_proxy_count <= 0:
        return peer

    headers = Headers(scope=request_scope)
    forwarded = headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return peer

    # The last `trusted_proxy_count` entries were added by trusted hops; the
    # address immediately before them is the furthest one we can believe.
    index = len(chain) - trusted_proxy_count
    if index < 0:
        # Fewer entries than configured proxies: the chain is shorter than
        # expected, so fall back to the peer rather than trusting a client
        # value.
        return peer
    return chain[index] if index < len(chain) else peer


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing per-path request limits.

    Implemented at the ASGI layer rather than with `BaseHTTPMiddleware` so the
    check happens before the request body is read or validated, and so a
    rejection does not need to construct a full request object.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rules: dict[tuple[str, str], str],
        trusted_proxy_count: int = 0,
        enabled: bool = True,
    ) -> None:
        """
        Args:
            rules: {(METHOD, path): limit spec}, e.g. {("POST", "/auth/login"): "5/minute"}
            trusted_proxy_count: see `client_identifier`.
            enabled: when False the middleware passes everything through.
        """
        self.app = app
        self.enabled = enabled
        self.trusted_proxy_count = trusted_proxy_count
        self.storage = MemoryStorage()
        self.limiter = MovingWindowRateLimiter(self.storage)
        self.rules: dict[tuple[str, str], list[RateLimitItem]] = {
            (method.upper(), path): parse_limits(spec)
            for (method, path), spec in rules.items()
        }

    def reset(self) -> None:
        """Drop all counters. Intended for tests, not request handling."""
        self.storage.reset()

    def _rule_for(self, method: str, path: str) -> list[RateLimitItem] | None:
        return self.rules.get((method.upper(), path))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        # Trailing slashes are distinct paths to Starlette; normalise so
        # "/auth/login/" cannot slip past a rule registered for "/auth/login".
        raw_path = scope.get("path", "")
        path = raw_path.rstrip("/") or "/"
        limits_for_path = self._rule_for(scope.get("method", ""), path)

        if not limits_for_path:
            await self.app(scope, receive, send)
            return

        identity = client_identifier(scope, self.trusted_proxy_count)

        for item in limits_for_path:
            # The path is part of the key so login and signup budgets are
            # independent, and the identity is the client bucket.
            if not self.limiter.hit(item, identity, path):
                await self._reject(item, identity, path, scope, receive, send)
                return

        await self.app(scope, receive, send)

    async def _reject(
        self,
        item: RateLimitItem,
        identity: str,
        path: str,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        stats = self.limiter.get_window_stats(item, identity, path)
        retry_after = max(1, int(round(stats.reset_time - time.time())))

        # Log that a limit fired, with the path and a coarse reason only. The
        # request body (which holds the password) is never read here, and the
        # client identity is not logged, so this cannot become a credential or
        # IP-tracking log.
        logger.warning(
            "Rate limit exceeded for %s (%s); refused with 429", path, str(item)
        )

        response = JSONResponse(
            status_code=429,
            content={
                "detail": (
                    "Too many requests. Please wait before trying again."
                )
            },
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(item.amount),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(stats.reset_time)),
                # Caches must never serve or store a throttling decision.
                "Cache-Control": "no-store",
            },
        )
        await response(scope, receive, send)


def build_credential_rules(settings) -> dict[tuple[str, str], str]:
    """Rules for the two credential endpoints.

    Kept separate from the middleware so tests can build it with custom specs,
    and so the limits remain configuration rather than code.
    """
    return {
        ("POST", LOGIN_PATH): settings.rate_limit_login,
        ("POST", SIGNUP_PATH): settings.rate_limit_signup,
    }


def redact_query_string(path_with_query: str) -> str:
    """Replace sensitive query parameter values with a placeholder.

    Media playback URLs carry `access_token` in the query string, because a
    browser `<video>` element cannot send an `Authorization` header. Any log
    line that includes a full request target would therefore capture a usable
    (if short-lived) credential. This is applied by the access-log filter in
    `logging_setup.py`.

    Only application-controlled logging is covered. Reverse proxies, CDNs,
    load balancers, and browser history are outside this process and must be
    configured separately; see `docs/SECURITY_OPERATIONS.md`.
    """
    if "?" not in path_with_query:
        return path_with_query

    sensitive = {"access_token", "token", "signature", "sig", "key"}
    base, _, query = path_with_query.partition("?")
    parts: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        name, sep, _value = pair.partition("=")
        if sep and name.lower() in sensitive:
            parts.append(f"{name}=REDACTED")
        else:
            parts.append(pair)
    return f"{base}?{'&'.join(parts)}" if parts else base


__all__: Iterable[str] = (
    "RateLimitMiddleware",
    "build_credential_rules",
    "client_identifier",
    "parse_limits",
    "redact_query_string",
    "LOGIN_PATH",
    "SIGNUP_PATH",
)
