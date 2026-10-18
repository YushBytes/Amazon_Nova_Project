# Security Implementation Plan — Status

**Phase 3 (Security Hardening)**
**Last updated:** 2026-10-19 — reflects what is actually implemented, verified
by the test runs recorded in `docs/SECURITY_AUDIT.md` §4.

This document is a status record, not a proposal. Nothing below is marked
complete without a test or command backing it. Findings are identified as
`S1`–`S13` per `docs/SECURITY_AUDIT.md`.

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

---

## 2. Partially implemented

| # | Item | What was done | What remains |
| :-- | :-- | :-- | :-- |
| 33 | Unique indexes (S12) | `videos.video_id` unique index created at startup | `users.username` unique index is **not** created automatically — it would fail on an existing deployment holding duplicates. Operator step documented in `SECURITY_AUDIT.md` §5. |
| 34 | Pre-existing uploads (S5) | New uploads record ownership; videos without a record are denied | Videos uploaded before this phase are **inaccessible**. Their uploader was never recorded, so they cannot be attributed retroactively. Operator must delete or backfill. |
| 35 | Agent role grant (S3) | Role is no longer self-assignable | Granting is a **manual, unaudited** database update. No API, by instruction. |

---

## 3. Deferred — not implemented

Each was excluded by the Phase 3 brief or depends on a decision outside it.
None is claimed as done.

| # | Item | Finding | Why deferred |
| :-- | :-- | :-- | :-- |
| 36 | Rate limiting on auth endpoints | — | Out of scope. **Genuinely missing**: credential stuffing is unthrottled. Highest-priority follow-up. |
| 37 | Refresh tokens / httpOnly cookies | S13 | Session-architecture change the brief excludes. |
| 38 | Server-side token revocation | S13 | Same. Rotating `JWT_SECRET_KEY` is the only break-glass today. |
| 39 | Role-management API or admin UI | S3 | Brief excludes adding one. |
| 40 | S3 bucket hardening (public-access block, encryption, versioning) | Phase 1 §10.3 | Cloud configuration; no live AWS calls permitted this phase. |
| 41 | IAM policy fixes (no DynamoDB permissions; `bedrock:InvokeModel` on `*`) | Phase 1 §10.3 | Same. |
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

## 5. Known limitations

1. **No rate limiting.** The most significant remaining gap (#36).
2. **No revocation.** A stolen token is valid until expiry unless the signing
   key is rotated (S13).
3. **Media tokens live in URLs.** Necessary so `<video src>` works; they are
   scoped to one video and expire in 15 minutes, but URLs can land in browser
   history, logs, and referrers. Accepted trade-off, documented in
   `app.serve_upload`.
4. **Argon2 parameters unbenchmarked.** Library defaults; not tuned against
   production hardware.
5. **The in-memory MongoDB fake is not MongoDB.** Tests exercise application
   logic, not real driver or index behaviour.
6. **No live-service verification.** AWS, Bedrock, and MongoDB were mocked
   throughout; nothing was deployed.

---

*See `docs/SECURITY_AUDIT.md` for per-finding detail and the operator action
list, and `docs/RELEASE_CHECKLIST.md` for the release gate.*
