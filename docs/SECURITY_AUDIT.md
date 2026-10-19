# NovaFlow / Brand Guardian — Security Audit and Remediation

**Phase:** 3 (Security Hardening), extended by Phase 3.5 (Security Closure)
**Date:** 2026-10-19 (Phase 3), updated 2026-10-19 (Phase 3.5)
**Scope:** `backend/` and the frontend code that talks to it. The S3 + DynamoDB
analysis pipeline, MongoDB account store, and Next.js frontend architecture are
unchanged.
**Baseline commit:** `2028dd5`

Every finding below was re-verified against the code as it actually stood at
the start of this phase, not carried over from the Phase 1 audit. Two Phase 1
claims turned out to be imprecise and are corrected here (S7, S8).

| Tag | Meaning |
| :-- | :-- |
| **[VERIFIED]** | Reproduced during this phase with a command or test. |
| **[INFERRED]** | Established by reading the code; not executed. |
| **[OPEN]** | Needs an operator action or a decision outside this phase. |

No secret value appears in this document.

---

## 1. Summary

| ID | Finding | Severity | Status |
| :-- | :-- | :-- | :-- |
| S1 | Hardcoded JWT signing key | **Critical** | Fixed |
| S2 | Unsalted SHA-256 password storage | **High** | Fixed, with migration |
| S3 | Privilege escalation via public signup | **Critical** | Fixed |
| S4 | Six routes with no authentication | **High** | Fixed |
| S5 | Cross-user access to videos, results, and projects (IDOR) | **High** | Fixed |
| S6 | Weak token validation | **Medium** | Fixed |
| S7 | Unrestricted upload extension + public file serving | **High** | Fixed |
| S8 | Internal error detail returned to clients | **Medium** | Fixed |
| S9 | CORS wildcard with credentials | **Medium** | Fixed |
| S10 | Authorization decision masked as a 500 | **Medium** | Fixed |
| S11 | Duplicate `/auth/docs` surface from a mounted sub-app | **Low** | Fixed |
| S12 | No username uniqueness constraint | **Low** | Partially mitigated |
| S13 | Tokens in `localStorage`, no revocation | **Medium** | Accepted, documented |
| S14 | No rate limiting on the credential endpoints | **High** | Fixed (Phase 3.5) |
| S15 | Media tokens recorded in application logs | **Medium** | Fixed (Phase 3.5) |
| S16 | `users.username` had no unique index | **Low** | Fixed (Phase 3.5) |
| S17 | Overbroad / incomplete IAM policy | **Medium** | Template provided; operator action |

---

## 2. Findings

### S1 — Hardcoded JWT signing key — **Critical** — Fixed

**Verified.** `backend/auth.py:15` contained the literal signing key
`"brand-guardian-dev-secret-change-in-prod"` with no environment override.

**Impact.** Anyone who could read the repository could forge a valid token for
any username and any role, defeating authentication entirely. The repository
was public for roughly seven minutes during Phase 0 before being made private,
though `backend/auth.py` had never been pushed at that point (the remote held
only five root files), so this key was never publicly exposed via GitHub.

**Remediation.**
- `backend/config.py` (new) resolves the key from `JWT_SECRET_KEY`.
- Production (`NOVAFLOW_ENV=production`) **refuses to start** without it.
- A blocklist (`KNOWN_INSECURE_SECRETS`) rejects the previously hardcoded value
  and common placeholders in every environment, so it cannot be reintroduced.
- A minimum length of 32 characters is enforced.
- In development an **ephemeral random key** is generated per process: local
  work needs no configuration, yet no guessable constant is ever used. Sessions
  do not survive a restart, which is the signal to configure a real key.
- `Settings.__repr__` redacts the key, so it cannot leak through a traceback.

**Evidence.** `tests/test_security_config.py` (24 tests), plus a live process check:

```
$ NOVAFLOW_ENV=production python -c "import app"      # JWT_SECRET_KEY unset
config.ConfigurationError: JWT_SECRET_KEY is required when NOVAFLOW_ENV=production.
exit=1

$ JWT_SECRET_KEY=brand-guardian-dev-secret-change-in-prod NOVAFLOW_ENV=production python -c "import app"
config.ConfigurationError: JWT_SECRET_KEY is a known-insecure placeholder value and was rejected.
exit=1
```

**[OPEN] Operator action.** Generate and set a fresh `JWT_SECRET_KEY` before
deploying. Any token minted with the old key must be considered forgeable;
changing the key invalidates all existing sessions, which is the desired
outcome here.

---

### S2 — Unsalted SHA-256 password storage — **High** — Fixed

**Verified.** `auth.hash_password` was `hashlib.sha256(password.encode()).hexdigest()`.

**Impact.** SHA-256 is a fast, general-purpose digest. Unsalted, identical
passwords produce identical hashes, so a single precomputed table cracks every
matching account at once, and GPU attacks run at billions of guesses per second.

**Remediation.** Argon2id via `argon2-cffi==25.1.0` (maintained reference
implementation), default parameters `t=3, m=64 MiB, p=4`. Salts are generated
per hash and embedded in the encoded form.

**Legacy migration.** Existing records are migrated transparently:

1. `check_password` recognises exactly two formats — an Argon2 encoded hash
   (`$argon2…`) or exactly 64 lowercase hex characters (the old digest, matched
   with an anchored regex).
2. For a legacy record the password is verified with the legacy digest using a
   constant-time comparison.
3. On success only, the hash is replaced with Argon2id. The update is
   conditional on the hash value just verified
   (`{"_id": …, "password": old_hash}`), so two concurrent logins cannot clobber
   each other, and a record changed in the meantime is left alone.
4. On failure the record is untouched.
5. Anything else — `None`, empty, truncated, uppercase hex, a bcrypt hash, a
   dict shaped like a NoSQL injection — is rejected outright rather than being
   guessed at.
6. An upgrade failure is logged and swallowed: the user is already
   authenticated and must not be locked out by a storage hiccup.

No forced password reset is required.

**Compatibility note.** Password length policy (minimum 8) applies to
`SignupRequest` only. `LoginRequest` deliberately has no minimum, because
enforcing one would permanently lock out any pre-existing account whose
password is shorter. This was caught by a test during implementation.

**Evidence.** `tests/test_security_auth.py` — Argon2id prefix, per-hash salting,
legacy verify-and-upgrade, failed login leaves the record intact, repeated
logins idempotent, 11 malformed stored values rejected, short legacy password
still accepted, and assertions that neither the password nor the hash reaches
the logs.

---

### S3 — Privilege escalation via public signup — **Critical** — Fixed

**Verified.** `POST /auth/signup` took `role` from the request body and only
checked membership in `{"client", "agent"}` (`auth.py:56,97,103`).

**Impact.** Any anonymous visitor could create an **agent** account, the
privileged role: agents accept projects from the marketplace and obtain
pre-signed S3 upload URLs. This was a complete authorization bypass requiring
nothing but a JSON field.

**Remediation.**
- The server always assigns `DEFAULT_SIGNUP_ROLE` (`client`).
- A `role` in the body is **ignored**, not rejected, so the existing frontend
  keeps working; the response reports the role actually assigned. This contract
  is documented on `SignupRequest`.
- `create_access_token` raises if asked to mint a token for a role outside the
  model, so an unknown role can never become a claim.
- `login` reads the role from stored account data and refuses to issue a token
  if the stored role is unrecognised.
- `GET /auth/me` re-reads the role from the database, so a stale token cannot
  keep asserting a role the account no longer holds.
- The frontend no longer posts a role at signup, disables the Agent option on
  the registration tab, and states that new accounts are Clients.

**Evidence.** `tests/test_security_signup_roles.py` (23 tests) — seven
privileged role strings rejected, the stored record and the issued token both
carry the default role, an escalated signup is then refused by an agent-only
route, extra body fields (`is_admin`, `permissions`) ignored, and existing
agent accounts verified as **not** downgraded.

**[OPEN] Granting the agent role.** There is deliberately no API for this (the
brief excludes a role-management feature). An operator with database access
updates the record directly:

```
db.users.updateOne({username: "<user>"}, {$set: {role: "agent"}})
```

The change takes effect at the user's next login, or immediately for
`/auth/me`. This is a documented limitation: the grant path is manual and
unaudited.

---

### S4 — Routes with no authentication — **High** — Fixed

**Verified** by enumerating the live app. Six of sixteen routes were open, plus
a public static mount.

Full inventory and final state:

| Method | Path | Auth | Roles | Ownership | Intentionally public |
| :-- | :-- | :-- | :-- | :-- | :-- |
| GET | `/status` | none | — | — | **yes** (liveness) |
| POST | `/auth/signup` | none | — | — | **yes** (registration) |
| POST | `/auth/login` | none | — | — | **yes** |
| GET | `/auth/me` | bearer | any | self | no |
| POST | `/execute` | bearer | any | — | no *(was open)* |
| POST | `/upload-reel` | bearer | any | records owner | no *(was open)* |
| GET | `/uploads/{file}` | scoped media token | — | owner | no *(was fully public)* |
| POST | `/analyze/{video_id}` | bearer | any | owner | no *(was open)* |
| GET | `/violations/{video_id}` | bearer | any | owner | no *(was open)* |
| GET | `/submissions` | bearer | any | owner-scoped list | no *(was open)* |
| POST | `/projects/` | bearer | client | — | no |
| GET | `/projects/marketplace` | bearer | any | — | no |
| PATCH | `/projects/{id}/accept` | bearer | agent | status guard | no |
| POST | `/projects/{id}/upload-init` | bearer | agent | assigned agent | no |
| POST | `/projects/{id}/review` | bearer | client | owning client | no |
| POST | `/projects/{id}/chat` | bearer | any | participant | no *(had none)* |

`/uploads/{file}` is authorized in the handler rather than by a dependency,
because a browser `<video>` element cannot send an `Authorization` header. It
requires a short-lived (15 minute) token scoped to one `video_id`.

**Remediation.** Shared dependencies live in `auth.py`: `get_current_user` and
`require_roles(...)`. `projects.py` now imports them instead of defining its own,
so every router enforces authentication identically.

**Evidence.** `tests/test_security_routes.py` parametrises all 12 protected
routes against both an anonymous caller and a forged token — 24 assertions —
plus explicit tests that the three public routes stay public.

---

### S5 — Cross-user access (IDOR) — **High** — Fixed

**Verified.** Nothing in the system recorded who uploaded a video. Uploads were
stored as `uploads/{video_id}.{ext}`, and DynamoDB's `NovaFlowFrames` keys only
on `video_id`. There was therefore no data available to answer "whose video is
this?", and no route asked.

**Impact.** A user who knew or guessed another user's `video_id` could read
their violations, download their footage, and start billable Bedrock analysis on
it. `GET /submissions` listed *every* file on disk to *every* caller. Any
authenticated user could read any project's brief through `/projects/{id}/chat`.

**Remediation.** `backend/video_repo.py` (new) adds a `videos` collection to the
MongoDB database that already stores accounts — no new database or storage
system, and the S3 + DynamoDB pipeline is untouched. Ownership is recorded at
upload time, the only moment the uploader's identity is known.

- `require_video_owner` gates `/analyze`, `/violations`, and media access.
- A **missing** ownership record is a denial, not a pass.
- Denials return **404**, not 403, so the endpoints cannot be used to discover
  which ids exist.
- `video_id` is validated against `^vid_[0-9a-f]{8}$` before any lookup.
- `/submissions` queries by owner and can only return the caller's own rows.
- `_require_participant` gates `/projects/{id}/chat` to the owning client or the
  assigned agent.
- Project creation takes `client_id` from the token, never the body.
- An upload whose ownership record cannot be written is rolled back off disk, so
  no unattributable file is left behind.

**Evidence.** `tests/test_security_routes.py` and
`tests/test_security_uploads.py` — Bob cannot read Alice's violations (and the
analysis store is not even consulted), cannot start analysis on her video,
cannot download it; a video with no ownership record is denied; a media token
for one video cannot read another; a non-participant gets 404 from chat and the
brief text is absent from the response; and ownership cannot be set through the
request payload.

**[OPEN] Pre-existing uploads.** Videos uploaded before this phase have no
ownership record and are therefore inaccessible through these routes. There is
no safe way to attribute them retroactively — the information was never
recorded. Delete them, or have an operator insert `videos` documents if the
owner is known from another source.

---

### S6 — Weak token validation — **Medium** — Fixed

**Verified.** `decode_token` checked only that `sub` and `role` were present.
The role value was not validated against the role model, `exp` was not required,
and every failure produced the same bare `401 "Invalid token"`.

**Remediation.** `_decode` now:
- pins `algorithms=[configured]`, so a token advertising a different algorithm —
  including `alg=none` — is rejected before any signature check;
- sets `require_exp` and `verify_exp`, so a token with no expiry is invalid;
- requires `sub` to be a non-empty string;
- requires `role` to be a member of `VALID_ROLES` for access tokens;
- separates token **scopes**, so a media token cannot authenticate an API route
  and a session token cannot fetch media;
- returns one uniform 401 with `WWW-Authenticate: Bearer` for every failure.

A missing `scope` claim is treated as `access`, so tokens minted before this
change keep working for their remaining lifetime.

**Evidence.** `tests/test_security_auth.py` — forged signature, expired, seven
malformed shapes, missing `exp`, six invalid `sub` values, nine invalid `role`
values, a hand-assembled `alg=none` token, an HS512 token while HS256 is
configured, both scope-confusion directions, and a test asserting that expired,
forged, and malformed tokens produce byte-identical responses.

---

### S7 — Unrestricted upload extension and public file serving — **High** — Fixed

**This corrects the Phase 1 audit.** Phase 1 (§10.1) described this as path
traversal. That is **not** accurate. `file.filename.split('.')[-1]` returns
everything after the *last* dot, and the result is interpolated as
`{video_id}.{extension}` — so the fragment always stays appended to a
server-generated prefix and remains relative to the upload directory. Verified
across several payload shapes: none escaped.

**The real defect**, verified end to end against the running app:

```
POST /upload-reel  (no credentials)  filename="clip.mp4.html"
  -> 200, stored as vid_cfb50d1f.html
GET /uploads/vid_cfb50d1f.html
  -> 200, content-type: text/html; charset=utf-8
  -> body: <script>alert(document.domain)</script>
```

An **unauthenticated** caller could store arbitrary content with an arbitrary
extension and have it served back as `text/html` from the application's own
origin — stored XSS, plus free file hosting. Empty files were accepted. There
was no size limit, no content check, and `uploads/` was a public `StaticFiles`
mount.

**Remediation.**
- Authentication required (S4).
- Extension taken via `Path(filename).suffix`, lowercased, required to be
  alphanumeric and present in `ALLOWED_VIDEO_EXTENSIONS`
  (default `mp4,mov,avi,webm,mkv`). Anything else is **rejected**, not sanitised.
- Stored filename is entirely server-generated: `vid_<8 hex>.<allowed ext>`.
  The client filename is kept only as `original_filename` display metadata.
- `_resolve_within_uploads` re-checks containment after joining — defence in
  depth even though the name is server-generated.
- Content type checked as a **secondary** signal; a matching type cannot
  override the extension allowlist.
- Size enforced **while streaming** in 1 MiB chunks (default 100 MiB), so a
  large body is never buffered in memory; exceeding it returns 413 and the
  partial file is deleted.
- Empty uploads rejected (400).
- The `StaticFiles` mount is replaced by `GET /uploads/{file}`, which requires a
  media token scoped to that video, verifies ownership, and responds with an
  explicit `video/mp4` type plus `X-Content-Type-Options: nosniff`.

**Evidence.** `tests/test_security_uploads.py` (44 tests) — eight disallowed
extensions rejected including `.html` and `.svg`; seven traversal-shaped names
rejected; a containment test asserting nothing is ever written outside the
upload directory and every stored name matches `vid_[0-9a-f]{8}\.mp4`; empty and
oversized rejected with no partial file left; `video/mp4` cannot smuggle a
`.html` file; and the media-access tests above.

**Not claimed.** An extension allowlist and a content-type check do **not**
prove a file is a safe video. No content scanning or deep container validation
was added, and none is claimed.

---

### S8 — Internal error detail returned to clients — **Medium** — Fixed

**Verified.** `app.py` returned `str(e)` to the client in `/violations`
(`detail=str(e)`), and `projects.py` did the same in `/review`. Botocore
exceptions embed table names, bucket names, ARNs, and region detail.

**Phase 1 correction:** Phase 1 also listed `print()`-based logging of model
analysis text as an exposure. That specific issue was already fixed in Phase 2,
when `analyzer.py` moved to `logging` and stopped logging analysis bodies.

**Remediation.**
- A catch-all `@app.exception_handler(Exception)` logs the traceback
  server-side and returns a flat `{"detail": "Internal server error"}`.
- `/violations` catches `BotoCoreError`/`ClientError` and returns 502 with no
  provider detail.
- `projects._fetch_project` does the same.
- Authentication failures return one uniform message (S6).
- API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled in production
  unless `EXPOSE_API_DOCS=true`, so the route surface is not published.

**Evidence.** `test_error_responses_do_not_leak_internals` asserts a seeded
internal failure returns neither the exception text, nor the class name, nor a
traceback. `test_status_does_not_leak_configuration` asserts the health probe
exposes no secret or connection string.

---

### S9 — CORS wildcard with credentials — **Medium** — Fixed

**Verified.** `app.py:30` used `allow_origins=["*"]` with all methods and
headers, while the mounted auth sub-app separately allowed
`http://localhost:3000` with `allow_credentials=True` — two conflicting
policies on one service.

**Remediation.** One policy on the parent app: exact origins from
`CORS_ALLOWED_ORIGINS` (default `http://localhost:3000`), credentials enabled,
methods restricted to those actually used, headers restricted to
`Authorization` and `Content-Type`. `config.load_cors_origins` **rejects** `*`
outright, since a wildcard cannot safely coexist with credentialed requests.

**Evidence.** `test_cors_wildcard_is_rejected`, `test_cors_defaults_to_the_local_frontend`.

---

### S10 — Authorization decision masked as a 500 — **Medium** — Fixed

**Verified.** In `projects.py`, both `/review` and `/upload-init` raised their
ownership `HTTPException` *inside* a `try` whose `except Exception` converted it
into `500 "Database error"`. The request was still refused, but the status code
was wrong, the reason was hidden, and a genuine storage failure was
indistinguishable from an authorization denial.

**Remediation.** Storage access is separated from the authorization decision
(`_fetch_project` raises 502 for a real storage failure; the ownership check
then raises 404 outside any `try`).

**Evidence.** `tests/test_security_routes.py` ownership tests now assert a clean
404 rather than accepting a 500. The Phase 2 test that previously allowed
`(403, 500)` was updated to require 404, with the reason recorded in the test.

---

### S11 — Duplicate `/auth/docs` surface — **Low** — Fixed

**Verified.** `app.mount("/auth", auth_app)` mounted a second FastAPI
application, which exposed its own `/auth/docs`, `/auth/redoc`, and
`/auth/openapi.json`, and kept the auth routes out of the parent schema
(so they were invisible in the main API docs and in any route audit based on it).

**Remediation.** Auth is now an `APIRouter` included with `prefix="/auth"`.
External URLs are byte-identical (`/auth/login`, `/auth/signup`, `/auth/me`), but
there is one OpenAPI schema, one CORS policy, and no second docs surface.
`oauth2_scheme`'s `tokenUrl` was also corrected from `/login` to `/auth/login`.

---

### S14 — No rate limiting on the credential endpoints — **High** — Fixed (Phase 3.5)

**Verified.** A grep for any limiter across the backend returned nothing:
`/auth/login` and `/auth/signup` were bounded only by network speed.

**Impact.** Unlimited credential stuffing against any known username, and
unlimited account creation. The uniform-401 response (S6) stops enumeration
telling an attacker *which* usernames exist, but nothing stopped them guessing
passwords as fast as the network allowed.

**Remediation.** `backend/rate_limit.py` adds ASGI middleware enforcing
per-path, per-client limits (defaults `5/minute;20/hour` for login,
`3/minute;10/hour` for signup, all configurable). Returns 429 with
`Retry-After`, `X-RateLimit-*`, and `Cache-Control: no-store`.

Two design points, both chosen after testing the alternative:

1. **Middleware, not a route decorator.** The idiomatic FastAPI approach is
   slowapi's `@limiter.limit(...)`. Measured against this stack, a request with
   a malformed body returns 422 **without consuming the budget** — 10 malformed
   requests left a `3/minute` budget fully intact, giving an attacker an
   unlimited channel through the same route. The middleware runs before routing
   and validation, so every request counts.
2. **The peer address, not `X-Forwarded-For`.** With `TRUSTED_PROXY_COUNT=0`
   (default) the header is ignored entirely, because a client can set it freely
   and would otherwise mint a fresh bucket per request. When proxies are
   configured, the address is read from the right of the chain.

**Evidence.** `tests/test_security_rate_limit.py` (30 tests): throttling after
repeated failures, `Retry-After` present, malformed/non-JSON/empty bodies all
consume the budget, a trailing slash cannot evade the rule, the budget recovers
after the window (clock patched, not slept), login and signup budgets are
independent, other routes unaffected, header rotation does not evade, and the
throttle log contains neither the username nor the password.

**[OPEN] Scope limitation.** Counters are in-process
(`limits.storage.MemoryStorage`), so the effective limit is multiplied by the
worker count. This is accurate for the current single-process Dockerfile and is
**not** distributed protection. Options are documented in
`docs/SECURITY_OPERATIONS.md` §3.2; a shared store was deliberately not enabled
because none is provisioned.

**[OPEN]** Limits are per-client, not per-account, so an attacker rotating
source addresses can still target one account slowly.

---

### S15 — Media tokens recorded in application logs — **Medium** — Fixed (Phase 3.5)

**Verified.** No redaction existed anywhere. Because playback uses
`?access_token=…`, the request target is itself a credential, and any log line
containing it captured a usable token. Reproduced in the test suite: the httpx
client logger emitted the full tokenised URL.

**Remediation.** `backend/logging_setup.py` installs a filter that rewrites
sensitive query values to `REDACTED` before a record is formatted, across
`uvicorn.access`, `uvicorn.error`, `httpx`, `httpx2`, and `httpcore`. It
handles **URL objects as well as strings** — the first implementation missed
httpx's `URL` type, which a test caught. The media TTL is now configurable
(`MEDIA_TOKEN_TTL_MINUTES`, default 15) and the unhandled-exception handler
logs `request.url.path` only.

**Evidence.** `tests/test_security_media_tokens.py` (20 tests): token scoped to
one video and one user, rejected for another video or user, not usable as a
session credential, not usable to read arbitrary files (five traversal probes),
forged and expired tokens rejected, response carries `nosniff` and an explicit
`video/mp4`, the access-log filter redacts while preserving the useful part of
the line, the filter is idempotent, and no token appears in logs during a real
request or an induced failure. Also: the ownership record does not store the
token, and `/submissions` mints fresh ones rather than persisting any.

**[OPEN] Not covered, by nature.** Logs written by a reverse proxy, CDN, load
balancer, or hosting platform, plus browser history and `Referer`. Those are
deployment concerns; see `docs/SECURITY_OPERATIONS.md` §8.3.

---

### S16 — `users.username` had no unique index — **Low** — Fixed (Phase 3.5)

**Verified.** `create_index` appeared only in `video_repo.py`; nothing created
an index on `users`. `signup` relied on a non-atomic read-then-write check, so
concurrent registrations could both succeed.

**Remediation.** `backend/db_init.py` provides repeatable, non-destructive
initialization, run from the application lifespan:

- duplicates are detected **before** attempting the index, because creating a
  unique index over existing duplicates fails;
- the diagnostic reports **counts only**, never the usernames, so it is safe to
  log;
- no account is deleted, merged, or renamed to make the migration succeed —
  verified by a test that compares the collection before and after;
- `strict=False` (startup) logs and continues; `strict=True` (operator) raises.

`signup` now also catches `DuplicateKeyError` and returns the same clean 400 as
the checked path, so the losing side of a race is not an unhandled 500. New
usernames with surrounding whitespace are rejected, while existing untrimmed
records remain reachable (a test covers this) because rewriting a stored
username would lock that account out.

**Evidence.** `tests/test_security_db_integrity.py` (26 tests).

**[OPEN]** Username matching remains case-sensitive and exact, so `Alice` and
`alice` are distinct accounts. Normalising is an operator decision with the
same data implications as a merge; see `docs/SECURITY_OPERATIONS.md` §4.3.

---

### S17 — Overbroad and incomplete IAM policy — **Medium** — Template provided

**Verified** against the boto3 calls the code actually makes.
`infra/iam_policy.yaml` granted `bedrock:InvokeModel` on `Resource: '*'`
(every model in the account), granted `bedrock:InvokeModelWithResponseStream`
which no code path uses, scoped S3 to `brand-guardian-*` buckets that **no code
reads**, and omitted **DynamoDB entirely** — meaning the application could not
have functioned under that role.

**Remediation.** `infra/iam_policy_least_privilege.json`: a template with
explicit placeholders, scoping Bedrock to `amazon.nova-pro-v1:0`, S3 to the
`frames/` and `raw/` prefixes of one configured bucket, `ListBucket` to those
prefixes by condition, DynamoDB to the two named tables with only the verb set
in use, and logs to this application's group. It adds an explicit `Deny` when
`aws:SecureTransport` is false, and records in `_NOT_GRANTED` every permission
deliberately withheld and why.

No account id, bucket name, or table ARN was invented. The existing
`infra/iam_policy.yaml` was **not** rewritten, because the repository contains
two conflicting bucket naming schemes and resolving that is an operator
decision (`docs/SECURITY_OPERATIONS.md` §7.1).

**[OPEN] Operator action.** Fill in the placeholders, resolve the bucket
naming, create the DynamoDB tables (no IaC does), and apply the bucket
hardening listed in §7.4 — public-access block, encryption, versioning, TLS
bucket policy, and access logging, **none** of which
`infra/init_s3.py` configures. No live AWS call was made and no live policy was
changed.

---

### S12 — No username uniqueness constraint — **Low** — Partially mitigated

`signup` does a read-then-write existence check with no unique index, so two
simultaneous registrations of the same username can both succeed.

**Mitigated:** `video_repo.ensure_indexes` creates a unique index on
`videos.video_id` at startup.
**Superseded by S16 (Phase 3.5).** `backend/db_init.py` now creates the
`users.username` unique index safely: it detects duplicates first, reports
counts only, and skips rather than failing or deleting data. The manual step
below remains valid for an operator who prefers to do it explicitly:

```
db.users.createIndex({username: 1}, {unique: true})
```

Impact is low — the duplicate accounts would have separate password hashes and
neither gains the other's access.

---

### S13 — Token storage and revocation — **Medium** — Accepted, documented

Tokens are JWTs held in `localStorage`, readable by any successful XSS, and
bearer JWTs cannot be revoked before expiry. The default lifetime is 24 hours.

Changing this means introducing refresh tokens, httpOnly cookies, or a
server-side session store — a session-architecture change the brief explicitly
excludes. **Accepted for this phase**, with three partial mitigations:

- `/auth/me` re-reads the role from the database, so a privilege *change* takes
  effect without waiting for expiry.
- `ACCESS_TOKEN_EXPIRE_MINUTES` is configurable; lower it to shrink the window.
- Rotating `JWT_SECRET_KEY` invalidates every outstanding token immediately,
  which is the available break-glass revocation.

Removing the stored-XSS vector (S7) materially reduces the practical risk.

---

## 3. Explicitly out of scope

Not addressed in **Phase 3**, by instruction. Items marked done were
subsequently closed in Phase 3.5:

- **DONE (S14):** rate limiting on `/auth/login` and `/auth/signup`.
- Refresh tokens / httpOnly cookies (S13).
- A role-management API or admin dashboard (S3 grant path stays manual).
- S3 bucket hardening and the IAM policy gaps from Phase 1 §10.3 — a
  least-privilege **template** is now provided (S17), but applying it,
  resolving the bucket naming conflict, and hardening the bucket remain
  operator actions. No live AWS change was made.
- Content scanning of uploaded media.
- Nova Canvas remediation, reconstruction, Nova Act, Step Functions.

---

## 4. Verification summary

All commands run on 2026-10-19.

Phase 3 (2026-10-19):

| Check | Command | Result |
| :-- | :-- | :-- |
| Backend suite | `pytest -q` | **277 passed, 1 skipped** |
| Security tests only | `pytest tests/test_security_*.py -q` | **202 passed** |
| Clean install | `pip install -r requirements.txt` (fresh venv) | exit 0 |
| Production fail-safe | `NOVAFLOW_ENV=production` without the key | exit 1, safe error |
| Frontend tests | `npm test` | **42 passed** |
| Frontend lint | `npm run lint` | 30 problems (8 errors, 22 warnings) |

Phase 3.5 (2026-10-19), the current state:

| Check | Command | Result |
| :-- | :-- | :-- |
| Backend suite | `pytest -q` | **363 passed, 14 skipped** |
| Rate limiting | `pytest tests/test_security_rate_limit.py -q` | **30 passed** |
| DB integrity | `pytest tests/test_security_db_integrity.py -q` | **26 passed** |
| Media tokens | `pytest tests/test_security_media_tokens.py -q` | **20 passed** |
| Real-MongoDB integration | `pytest -m requires_mongodb` | **13 skipped, NOT VERIFIED** (no MongoDB and no responding Docker daemon); 10 safety-guard tests pass without a database |
| Clean install | `pip install -r requirements.txt` (fresh venv) | exit 0, 18 modules import |
| Import + routes | `python -c "import app"` | 16 OpenAPI paths |
| Frontend tests | `npm test` | **50 passed** |
| Frontend types | `npm run typecheck` | exit 0 |
| Frontend lint | `npm run lint` | **22 problems (0 errors, 22 warnings)** |
| Frontend build | `npm run build` | exit 0, 7/7 pages |

The 14 skips are 1 x `requires_ffmpeg` (the executable is not installed on this
machine) and 13 x `requires_mongodb` (no database available). Both are
environmental prerequisites, not application defects. The remaining 22 frontend
lint findings are all warnings - 17 unused variables and 5 `<img>` suggestions
- none security-relevant; they are left documented rather than suppressed.

**Not verified.** No live AWS, Bedrock, or MongoDB call was made at any point;
all three are mocked. No deployment was performed. Argon2 parameters were not
benchmarked on production hardware.

---

## 5. Required operator actions

| # | Action | Why |
| :-- | :-- | :-- |
| 1 | Generate and set `JWT_SECRET_KEY` (≥32 chars) in each environment | Production will not start without it (S1) |
| 2 | Treat any previously issued token as forgeable | They were signed with a key present in the source (S1) |
| 3 | Set `CORS_ALLOWED_ORIGINS` to the real frontend origin | Default is localhost only (S9) |
| 4 | Decide what to do with pre-Phase-3 uploads | They have no ownership record and are now inaccessible (S5) |
| 5 | Create the `users.username` unique index | Not done automatically (S12) |
| 6 | Grant the agent role by direct database update where needed | No API exists by design (S3) |
| 7 | Review the rate limits and the per-process caveat | Implemented in Phase 3.5 (S14); see `docs/SECURITY_OPERATIONS.md` §3 |
| 8 | Fill in `infra/iam_policy_least_privilege.json` and resolve the bucket naming conflict | The old policy omitted DynamoDB entirely (S17) |
| 9 | Apply S3 bucket hardening | `infra/init_s3.py` configures none of it |
| 10 | Configure proxy/CDN log redaction for `?access_token=` | The app cannot redact logs written outside this process (S15) |
| 11 | Run `pytest -m requires_mongodb` against a disposable database | Written and guarded, but never executed |

---

*Related: `docs/SECURITY_OPERATIONS.md` for the operator runbook,
`docs/SECURITY_IMPLEMENTATION_PLAN.md` for what was implemented versus
deferred, `docs/RELEASE_CHECKLIST.md` for the release gate, and
`docs/PROJECT_AUDIT.md` for the original Phase 1 audit (§10 of which is
corrected here by S7 and S8).*
