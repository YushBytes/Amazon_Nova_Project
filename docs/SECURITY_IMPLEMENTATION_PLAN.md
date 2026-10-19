# Security Implementation Plan — Status

**Phase 3 (Security Hardening) + Phase 3.5 (Security Closure)**
**Last updated:** 2026-10-19 (Phase 3.5) — reflects what is actually
implemented, verified by the test runs recorded in `docs/SECURITY_AUDIT.md` §4.

This document is a status record, not a proposal. Nothing below is marked
complete without a test or command backing it. Findings are identified as
`S1`–`S17` per `docs/SECURITY_AUDIT.md` (S14–S17 were added in Phase 3.5).

---

## 1. Implemented and verified

| # | Workstream | Finding | Where | Evidence |
| :-- | :-- | :-- | :-- | :-- |
| 1 | JWT key moved to environment config | S1 | `backend/config.py`, `backend/auth.py` | `test_security_config.py` (24) + live startup check |
| 2 | Production fails safely without a valid key | S1 | `config.Settings`, `config.load_jwt_secret` | process exits 1 with a safe message |
| 3 | Old hardcoded key permanently blocklisted | S1 | `config.KNOWN_INSECURE_SECRETS` | `test_production_rejects_the_old_hardcoded_secret` |
| 4 | Argon2id password hashing | S2 | `auth.hash_password` | `test_new_hashes_use_argon2id` |
| 5 | Transparent legacy SHA-256 migration | S2 | `auth.check_password`, `auth._upgrade_password_hash` | `test_legacy_account_is_migrated_on_successful_login` |
| 6 | Malformed stored hashes never authenticate | S2 | `auth.is_legacy_hash` (anchored regex) | 11 parametrised cases |
| 7 | Signup assigns the least-privileged role | S3 | `auth.signup`, `config.DEFAULT_SIGNUP_ROLE` | `test_security_signup_roles.py` (23) |
| 8 | Role claims originate from stored data | S3 | `auth.login`, `auth.create_access_token` | `test_login_role_comes_from_storage_not_the_request` |
| 9 | All 12 non-public routes require auth | S4 | `auth.get_current_user` on every route | 24 parametrised assertions |
| 10 | Role dependencies centralised | S4 | `auth.require_roles`, used by `projects.py` | role tests in `test_security_routes.py` |
| 11 | Video ownership recorded and enforced | S5 | `backend/video_repo.py`, `app.require_video_owner` | cross-user tests |
| 12 | `/submissions` scoped to the caller | S5 | `app.get_all_submissions` | `test_submissions_only_lists_the_callers_own_videos` |
| 13 | Project chat restricted to participants | S5 | `projects._require_participant` | `test_non_participant_cannot_read_a_project_brief_via_chat` |
| 14 | Ownership cannot be set via payload | S5 | `projects.create_project` | `test_project_ownership_cannot_be_set_through_the_payload` |
| 15 | Algorithm pinned; `alg=none` rejected | S6 | `auth._decode` | `test_unsigned_none_algorithm_token_is_rejected` |
| 16 | Expiry required; claims validated | S6 | `auth._decode` | expired / missing-`exp` / bad-`sub` / bad-`role` tests |
| 17 | Token scope separation (access vs media) | S6 | `auth.SCOPE_*` | both confusion directions tested |
| 18 | Uniform authentication error responses | S6 | `auth._CREDENTIALS_ERROR` | `test_authentication_errors_are_uniform` |
| 19 | Upload extension allowlist | S7 | `app._safe_extension` | 8 disallowed + 7 traversal-shaped cases |
| 20 | Server-generated storage filenames | S7 | `app.upload_reel` | `test_stored_filename_shape_is_server_controlled` |
| 21 | Streaming size limit, empty-file rejection | S7 | `app._stream_to_disk` | 413 with no partial file left |
| 22 | Public `StaticFiles` mount replaced | S7 | `app.serve_upload` | media token + ownership tests |
| 23 | Generic 500s; no provider detail leaked | S8 | `app.unhandled_exception_handler` | `test_error_responses_do_not_leak_internals` |
| 24 | Docs disabled in production by default | S8 | `config.Settings.expose_docs` | `test_docs_are_hidden_in_production_by_default` |
| 25 | CORS restricted; `*` rejected | S9 | `config.load_cors_origins` | `test_cors_wildcard_is_rejected` |
| 26 | Authorization no longer masked as 500 | S10 | `projects._fetch_project` | ownership tests assert 404 |
| 27 | Auth mounted as a router, not a sub-app | S11 | `app.include_router(auth_router)` | 16 paths in one OpenAPI schema |
| 28 | Credentials kept out of logs | S2, S8 | `logging` throughout | `test_login_does_not_log_the_password_or_hash` |
| 29 | `.env.example` template; `.env` ignored | config | `.env.example`, `.gitignore` | `git add --dry-run` refuses `.env` |
| 30 | Frontend sends bearer tokens | S4 | `frontend/app/lib/api.ts` | 9 tests in `api.test.ts` |
| 31 | Frontend validates the stored session | S13 | `AuthContext` calls `/auth/me` | typecheck + build |
| 32 | Signup UI states the real role contract | S3 | `frontend/app/login/page.tsx` | typecheck + build |

### Added in Phase 3.5

| # | Workstream | Finding | Where | Evidence |
| :-- | :-- | :-- | :-- | :-- |
| 47 | Rate limiting on `/auth/login` and `/auth/signup` | S14 | `backend/rate_limit.py` | `test_security_rate_limit.py` (30) |
| 48 | Limits configurable, validated at startup | S14 | `config.load_rate_limit_*` | `test_limits_are_configurable`, `test_malformed_limit_string_fails_at_startup` |
| 49 | 429 with `Retry-After`, `X-RateLimit-*`, `Cache-Control: no-store` | S14 | `RateLimitMiddleware._reject` | `test_throttled_response_carries_retry_after_and_no_store` |
| 50 | Malformed bodies cannot bypass the limit | S14 | ASGI middleware, not a route decorator | `test_malformed_requests_consume_the_limit` (+2 variants) |
| 51 | Trailing-slash paths cannot evade a rule | S14 | `RateLimitMiddleware.__call__` | `test_trailing_slash_cannot_evade_the_rule` |
| 52 | Budget recovers after the window | S14 | `MovingWindowRateLimiter` | `test_budget_recovers_after_the_window` (clock patched, not slept) |
| 53 | `X-Forwarded-For` ignored unless proxies configured | S14 | `rate_limit.client_identifier` | `test_spoofed_forwarded_header_cannot_mint_new_buckets` |
| 54 | Throttle logs omit username and password | S14 | `RateLimitMiddleware._reject` | `test_throttle_logging_does_not_include_credentials` |
| 55 | `users.username` unique index, duplicate-aware | S16 | `backend/db_init.py` | `test_security_db_integrity.py` (26) |
| 56 | Duplicate report exposes counts only | S16 | `find_duplicate_username_counts` | `test_duplicate_report_never_exposes_usernames` |
| 57 | Blocked migration modifies no account | S16 | `ensure_user_indexes` | `test_no_account_is_modified_or_removed_by_a_blocked_migration` |
| 58 | Signup handles `DuplicateKeyError` cleanly | S16 | `auth.signup` | `test_signup_survives_a_duplicate_key_race` |
| 59 | New usernames reject surrounding whitespace | S16 | `SignupRequest` validator | 4 parametrised cases |
| 60 | Existing untrimmed usernames still log in | S16 | exact matching preserved | `test_existing_untrimmed_username_can_still_log_in` |
| 61 | One `video_id` cannot acquire two owners | S5/S16 | unique index + `record_video` | `test_duplicate_video_id_cannot_create_a_second_owner` |
| 62 | Ownership cannot be claimed by supplying an id | S5 | server-generated ids | `test_ownership_cannot_be_claimed_by_supplying_an_id` |
| 63 | Media tokens redacted from this process's logs | S15 | `backend/logging_setup.py` | `test_security_media_tokens.py` (20) |
| 64 | Redaction handles URL objects, not just strings | S15 | `RedactQueryStringFilter._redact_args` | `test_access_log_filter_redacts_the_token` |
| 65 | Media TTL configurable and short | S15 | `MEDIA_TOKEN_TTL_MINUTES` | `test_media_token_lifetime_is_configurable` |
| 66 | Media token is read-only and single-resource | S15 | `auth._decode` scopes | `test_media_token_grants_read_only` |
| 67 | Tokens not persisted in records or browser storage | S15 | `/submissions` mints fresh | `test_ownership_record_does_not_store_the_token`; dead `localStorage` read removed from `HUDView` |
| 68 | Frontend handles 429 distinctly from 401 | S14 | `frontend/app/login/page.tsx` | `app/login/page.test.tsx` (8) |
| 69 | Least-privilege IAM template | S17 | `infra/iam_policy_least_privilege.json` | JSON validated; 7 statements; no invented identifiers |
| 70 | Zero frontend lint errors | — | 8 `any`/JSX errors fixed | `npm run lint`: 0 errors (was 8) |
| 71 | Operator runbook | all | `docs/SECURITY_OPERATIONS.md` | — |

---

## 2. Partially implemented

| # | Item | What was done | What remains |
| :-- | :-- | :-- | :-- |
| 33 | Unique indexes (S12/S16) | **Closed in Phase 3.5.** `db_init.initialize_database` creates both the `videos` and `users.username` indexes, detecting duplicates first and skipping safely. | If duplicates already exist the index is skipped (logged, counts only) until an operator resolves them — `SECURITY_OPERATIONS.md` §4.3. |
| 34 | Pre-existing uploads (S5) | New uploads record ownership; videos without a record are denied | Videos uploaded before this phase are **inaccessible**. Their uploader was never recorded, so they cannot be attributed retroactively. Operator must delete or backfill. |
| 35 | Agent role grant (S3) | Role is no longer self-assignable | Granting is a **manual, unaudited** database update. No API, by instruction. |

---

## 3. Deferred — not implemented

Each was excluded by the Phase 3 brief or depends on a decision outside it.
None is claimed as done.

| # | Item | Finding | Why deferred |
| :-- | :-- | :-- | :-- |
| ~~36~~ | ~~Rate limiting on auth endpoints~~ | S14 | **Closed in Phase 3.5** — see items 47-54. One caveat remains: counters are per-process, so the limit is multiplied by the worker count. Not distributed protection; `SECURITY_OPERATIONS.md` §3.2. |
| 37 | Refresh tokens / httpOnly cookies | S13 | Session-architecture change the brief excludes. |
| 38 | Server-side token revocation | S13 | Same. Rotating `JWT_SECRET_KEY` is the only break-glass today. |
| 39 | Role-management API or admin UI | S3 | Brief excludes adding one. |
| 40 | S3 bucket hardening (public-access block, encryption, versioning) | Phase 1 §10.3 | Still **not applied**: it is bucket configuration, not code. The required settings are now enumerated in `SECURITY_OPERATIONS.md` §7.4 and in the template's `_BUCKET_HARDENING_NOT_EXPRESSIBLE_HERE` block. No live AWS call was made. |
| 41 | IAM policy fixes | S17 | **Template provided** in Phase 3.5 (`infra/iam_policy_least_privilege.json`), derived from the boto3 calls the code actually makes. Applying it is an operator action, and the bucket naming conflict must be resolved first (`SECURITY_OPERATIONS.md` §7.1). The existing `infra/iam_policy.yaml` was deliberately left unchanged rather than edited with guessed resource names. |
| 42 | Content scanning of uploaded media | S7 | Brief excludes a scanning service. An extension allowlist is **not** claimed to prove a file is safe. |
| 43 | Password-strength rules beyond length | S2 | Minimum length only, on signup. |
| 44 | Multi-factor authentication | — | Not in scope. |
| 45 | Security headers (CSP, HSTS) | — | Belongs with deployment configuration. |
| 46 | Audit logging of security events | — | Events are logged, but there is no tamper-evident trail. |

---

## 4. Things this phase deliberately did not change

Preserved to stay within scope and avoid regressions:

- The S3 + DynamoDB frame pipeline, including `NovaFlowFrames` keyed on
  `video_id` alone. Authorization happens **before** any frame read, so no
  schema change was needed there.
- MongoDB as the account store. The new `videos` collection lives in the same
  database — no new datastore was introduced.
- External URLs. `/auth/login`, `/auth/signup`, and `/auth/me` are unchanged
  despite moving from a mounted sub-app to a router.
- The 24-hour default session lifetime.
- The MongoDB/GridFS pipeline (`audit_manager.py`, `verifier.py`), still
  pending the audit requested in Phase 2.
- `/execute`'s silent mock fallback in `orchestrator.py` — a correctness issue
  tracked for a later phase, now at least authenticated.

---

## 4A. Phase 3.5 scope decisions worth recording

| Decision | Why |
| :-- | :-- |
| ASGI middleware instead of slowapi's decorator | Measured: with the decorator, 10 malformed requests left a `3/minute` budget fully intact, so a malformed body was an unlimited bypass. `slowapi` was installed, tested for runtime compatibility, then **uninstalled** and not declared; the project depends on `limits` directly, which is the engine slowapi itself uses. |
| In-memory storage, no Redis | A shared store is the correct answer for multi-instance deployments, but provisioning one is a deployment decision. Shipping an unconfigured production dependency would be worse than a documented limitation. |
| `X-Forwarded-For` ignored by default | Trusting it without a known proxy depth lets a client mint a fresh bucket per request, defeating the limit entirely. |
| Usernames still matched exactly | Trimming or lower-casing on read would make existing `" alice"` / `"Alice"` accounts unreachable. New signups reject surrounding whitespace; normalising existing data is an operator decision. |
| `infra/iam_policy.yaml` left in place | Editing it would require inventing bucket names and an account id. A placeholder template was added alongside it instead. |
| 22 frontend lint warnings left | Unused imports and `<img>` suggestions in views unrelated to security. Fixing them would mean touching components outside this phase's scope; suppressing the rules would hide real issues later. |

---

## 5. Known limitations

1. **Rate limiting is per-process**, so the effective limit scales with the
   worker count. Accurate for the current single-process Dockerfile; not
   distributed protection (#36, `SECURITY_OPERATIONS.md` §3.2). There is also
   no per-account throttle, so an attacker rotating source addresses can still
   target one account slowly.
2. **No revocation.** A stolen token is valid until expiry unless the signing
   key is rotated (S13).
3. **Media tokens live in URLs.** Necessary so `<video src>` works; they are
   scoped to one video and expire in 15 minutes, but URLs can land in browser
   history, logs, and referrers. Accepted trade-off, documented in
   `app.serve_upload`.
4. **Argon2 parameters unbenchmarked.** Library defaults; not tuned against
   production hardware.
5. **Real-MongoDB integration tests are written but UNVERIFIED.**
   `tests/test_integration_mongodb.py` covers real unique-index enforcement,
   duplicate-key handling, conditional legacy-password migration, concurrent
   logins, and ownership scoping. They could **not be executed**: no MongoDB
   was reachable and the Docker daemon was not responding. 10 safety-guard
   tests (which need no database) do pass, proving the guard refuses any
   target it cannot confirm is disposable. Run
   `pytest -m requires_mongodb` with `MONGODB_TEST_URL` set.
6. **No live-service verification.** AWS, Bedrock, and MongoDB were mocked
   throughout both phases; nothing was deployed, and no live IAM role, bucket
   policy, or table was changed.
7. **Log redaction covers this process only.** Proxy, CDN, and platform access
   logs, plus browser history and `Referer`, are outside it
   (`SECURITY_OPERATIONS.md` §8.3).

---

*See `docs/SECURITY_AUDIT.md` for per-finding detail,
`docs/SECURITY_OPERATIONS.md` for the operator runbook, and
`docs/RELEASE_CHECKLIST.md` for the release gate.*
