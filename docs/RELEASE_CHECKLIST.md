# NovaFlow / Brand Guardian — Release Checklist

**Created:** 2026-10-18 (Phase 1 audit)
**Status:** Initial checklist. **Every item is unchecked** — nothing has been verified as release-ready.

---

## How to use this checklist

- Check an item **only** when you have evidence: real command output, a test result, a screenshot, or a verified manual run.
- Record the evidence in the **Evidence** column. "The code exists" is not evidence.
- An item that cannot be verified stays unchecked. Do not check it with a caveat.
- `N/A` is permitted only when a maintainer has formally descoped the feature — note the decision.
- The roadmap phase that delivers each item is listed, so an unchecked item points at the work that remains.

---

## 1. Core functionality

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 1.1 | ☐ A user can sign up and log in; the session persists and is server-validated | 3 | |
| 1.2 | ☐ A supported video uploads successfully with auth enforced | 3 | |
| 1.3 | ☐ Unsupported/oversized/over-length files are rejected with a clear message | 3 | |
| 1.4 | ☒ Upload creates a durable job/audit record | 4 | **Closed in Phase 4** - see 2C.1-2C.7 |
| 1.5 | ☐ Frames are extracted and stored, with a verified count | 4 | |
| 1.6 | ☐ Frames are analysed by Amazon Nova Pro and **detections are persisted** *(currently impossible — audit §9.1)* | 2, 4 | |
| 1.7 | ☐ Violations appear in the dashboard with correct bounding boxes, timestamps, and confidence | 4 | |
| 1.8 | ☒ Job status shown in the UI reflects reality, including failures | 4 | **Closed in Phase 4** - see 2C.73-2C.86 |
| 1.9 | ☐ No fabricated status or fictional data in any released view *(currently `/submissions` derives status from `hash()` — audit §9.6)* | 4, 7 | |
| 1.10 | ☐ A user can request remediation for a selected violation | 8 | |
| 1.11 | ☐ Remediation performs a **real** edit; output verifiably differs from the input *(currently a disguised mock — audit §9.3)* | 8 | |
| 1.12 | ☐ Low-confidence detections route to human review rather than auto-remediating | 8 | |
| 1.13 | ☐ Corrected video is reconstructed from **remediated** frames *(currently stitches originals — audit §9.4)* | 9 | |
| 1.14 | ☐ Output video preserves source framerate and audio | 9 | |
| 1.15 | ☐ Corrected output is verified (the "Critic") before delivery *(does not exist — audit §9.5)* | 9 | |
| 1.16 | ☐ Audit history is real, queryable, and append-only | 9 | |
| 1.17 | ☐ Role separation (client vs agent) is enforced **server-side**, not just in nav | 3, 7 | |
| 1.18 | ☐ All 12 workflow stages in audit §5 are classified "Implemented and verified" or formally descoped | 2-9 | |

---

## 2. Security

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 2.1 | ☐ No hardcoded secrets in source *(currently `auth.py:15`)* | 3 | |
| 2.2 | ☐ JWT signing key is env-sourced; the app refuses to start without it | 3 | |
| 2.3 | ☐ Passwords hashed with a password KDF, not plain SHA-256 *(currently `auth.py:24`)* | 3 | |
| 2.4 | ☐ Signup cannot self-assign the `agent` role *(currently it can — audit §10.2)* | 3 | |
| 2.5 | ☐ Every route reading or writing user data requires authentication *(10 of 16 currently do not)* | 3 | |
| 2.6 | ☐ Authorization enforces per-user ownership of videos and jobs | 3 | |
| 2.7 | ☐ Path-traversal test passes for uploads *(currently vulnerable — `app.py:75`)* | 3 | |
| 2.8 | ☐ Server-side file type, size, and duration validation | 3 | |
| 2.9 | ☐ Uploaded media is not publicly served without auth or expiry *(currently is — `app.py:38`)* | 3 | |
| 2.10 | ☐ CORS restricted to known origins *(currently `["*"]`)* | 3 | |
| 2.11 | ☐ Rate limiting on auth endpoints | 3 | |
| 2.12 | ☐ Raw exception strings are not returned to clients | 3 | |
| 2.13 | ☐ No secrets or user content in logs at info level | 3, 6 | |
| 2.14 | ☐ Secret scan clean on the full repository **and** full Git history | 10 | |
| 2.15 | ☐ Dependency audit clean (`pip-audit`, `npm audit`) with no unresolved high findings | 10 | |
| 2.16 | ☐ All dependencies version-pinned *(currently none in `requirements.txt`)* | 2 | |
| 2.17 | ☐ S3 bucket public access **verified blocked**; encryption enabled *(unverified — audit §13 item 4)* | 5 | |
| 2.18 | ☐ IAM policy follows least privilege; covers DynamoDB; Bedrock scoped to specific models *(currently `Resource: '*'` and no DynamoDB access)* | 5 | |
| 2.19 | ☐ Test credentials removed from `README.md` | 10 | |
| 2.20 | ☐ AI-supplied coordinates validated before use in any edit | 8 | |

---

## 2A. Phase 3 security hardening

Added 2026-10-19. Evidence is in `docs/SECURITY_AUDIT.md` §4 unless stated.
Items marked ☒ were verified by an automated test or a recorded command; items
left ☐ require an operator action or a live environment this phase could not
reach.

### Required environment configuration

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.1 | ☒ `JWT_SECRET_KEY` read from the environment, never hardcoded | `test_auth_module_has_no_hardcoded_secret` |
| 2A.2 | ☒ Production refuses to start without a valid signing key | live check: exit 1 |
| 2A.3 | ☒ Previously hardcoded key permanently blocklisted | `test_production_rejects_the_old_hardcoded_secret` |
| 2A.4 | ☒ `.env.example` committed with placeholders only; real `.env` ignored | `git add --dry-run` refuses `.env` |
| 2A.5 | ☐ A fresh `JWT_SECRET_KEY` generated and set in the deployment environment | operator action |
| 2A.6 | ☐ `CORS_ALLOWED_ORIGINS` set to the real frontend origin | operator action; default is localhost |
| 2A.7 | ☐ All pre-Phase-3 tokens treated as invalid after key rotation | operator action |

### Authentication and passwords

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.8 | ☒ Passwords stored with Argon2id | `test_new_hashes_use_argon2id` |
| 2A.9 | ☒ Per-password salting (identical passwords differ) | `test_hash_is_salted_so_identical_passwords_differ` |
| 2A.10 | ☒ Legacy SHA-256 records migrate on successful login | `test_legacy_account_is_migrated_on_successful_login` |
| 2A.11 | ☒ A failed login never rewrites the stored hash | `test_failed_login_leaves_a_legacy_record_untouched` |
| 2A.12 | ☒ Malformed stored hashes never authenticate | 11 parametrised cases |
| 2A.13 | ☒ Short pre-existing passwords still work (no lockout) | `test_login_accepts_a_short_legacy_password` |
| 2A.14 | ☒ Login cannot be used to enumerate usernames | `test_unknown_user_and_wrong_password_look_identical` |
| 2A.15 | ☐ Legacy password migration observed against real MongoDB | needs a live database |
| 2A.16 | ☐ Argon2 parameters benchmarked on production hardware | library defaults in use |

### Token validation

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.17 | ☒ Invalid signatures rejected | `test_token_signed_with_a_different_key_is_rejected` |
| 2A.18 | ☒ Expired tokens rejected | `test_expired_token_is_rejected` |
| 2A.19 | ☒ Malformed tokens rejected | 7 parametrised shapes |
| 2A.20 | ☒ `alg=none` rejected; algorithm pinned | `test_unsigned_none_algorithm_token_is_rejected` |
| 2A.21 | ☒ Expiry claim required | `test_token_without_exp_is_rejected` |
| 2A.22 | ☒ Unknown role claims rejected | 9 parametrised values |
| 2A.23 | ☒ Authentication failures return one uniform response | `test_authentication_errors_are_uniform` |

### Roles and route authorization

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.24 | ☒ Public signup cannot grant a privileged role | `test_security_signup_roles.py` (23) |
| 2A.25 | ☒ Escalated signup is still refused by agent-only routes | `test_escalated_signup_cannot_reach_agent_only_routes` |
| 2A.26 | ☒ Existing agent accounts not downgraded | `test_existing_agent_account_is_not_downgraded` |
| 2A.27 | ☒ Every registered route reviewed and classified | `SECURITY_AUDIT.md` S4 table (16 routes) |
| 2A.28 | ☒ All 12 non-public routes reject anonymous callers | 24 parametrised assertions |
| 2A.29 | ☒ Role restrictions enforced server-side | role tests in `test_security_routes.py` |
| 2A.30 | ☒ Request-body role fields cannot elevate privilege | `test_role_cannot_be_elevated_through_the_request_body` |
| 2A.31 | ☒ Intentionally public routes still public | `/status`, `/auth/login`, `/auth/signup` tests |
| 2A.32 | ☒ Rate limiting on `/auth/login` and `/auth/signup` | **Closed in Phase 3.5** - see 2B.1-2B.15 |

### Resource ownership

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.33 | ☒ Video ownership recorded at upload | `test_upload_records_the_caller_as_owner` |
| 2A.34 | ☒ Cross-user video read denied | `test_another_user_cannot_read_violations` |
| 2A.35 | ☒ Cross-user analysis trigger denied | `test_another_user_cannot_start_analysis` |
| 2A.36 | ☒ Videos with no ownership record denied | `test_video_with_no_ownership_record_is_denied` |
| 2A.37 | ☒ `/submissions` returns only the caller's uploads | `test_submissions_only_lists_the_callers_own_videos` |
| 2A.38 | ☒ Project chat limited to participants | `test_non_participant_cannot_read_a_project_brief_via_chat` |
| 2A.39 | ☒ Ownership cannot be set through a payload | `test_project_ownership_cannot_be_set_through_the_payload` |
| 2A.40 | ☒ Denials return 404, not 403, to prevent id enumeration | ownership tests |
| 2A.41 | ☐ Pre-Phase-3 uploads deleted or backfilled | operator decision; currently inaccessible |

### Upload validation

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.42 | ☒ Extension allowlist enforced | 8 disallowed extensions rejected |
| 2A.43 | ☒ Traversal-shaped filenames rejected | 7 parametrised payloads |
| 2A.44 | ☒ Nothing is ever written outside the upload directory | `test_no_file_written_outside_the_upload_directory` |
| 2A.45 | ☒ Storage filenames fully server-generated | `test_stored_filename_shape_is_server_controlled` |
| 2A.46 | ☒ Empty uploads rejected | `test_empty_upload_is_rejected` |
| 2A.47 | ☒ Size limit enforced while streaming; partial file removed | `test_oversized_upload_is_rejected` |
| 2A.48 | ☒ Malformed multipart requests rejected | `test_malformed_multipart_request_is_rejected` |
| 2A.49 | ☒ Content type is a secondary check, not authoritative | `test_content_type_alone_cannot_bypass_the_extension_allowlist` |
| 2A.50 | ☒ Stored media no longer served publicly | `test_media_without_a_token_is_refused` |
| 2A.51 | ☒ Media tokens scoped to one video and one user | 3 media-scope tests |
| 2A.52 | ☐ Content/malware scanning of uploads | **not implemented**; excluded from scope |

### Errors, logging, and secret scanning

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.53 | ☒ No stack traces or exception text in responses | `test_error_responses_do_not_leak_internals` |
| 2A.54 | ☒ Passwords, hashes, and tokens absent from logs | 2 logging tests |
| 2A.55 | ☒ Health probe leaks no configuration | `test_status_does_not_leak_configuration` |
| 2A.56 | ☒ API docs off in production unless opted in | `test_docs_are_hidden_in_production_by_default` |
| 2A.57 | ☒ CORS wildcard rejected by configuration | `test_cors_wildcard_is_rejected` |
| 2A.58 | ☒ Secret scan clean over the Phase 3 diff | recorded in the Phase 3 report |
| 2A.59 | ☒ No secrets in `NEXT_PUBLIC_*` variables | `.env.example` documents the rule |
| 2A.60 | ☐ Secret scan over the full Git history | still outstanding (see item 2.14) |

### Dependencies and regression results

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2A.61 | ☒ `argon2-cffi` declared and pinned | `requirements.txt` |
| 2A.62 | ☒ Clean install from the manifest succeeds | fresh venv, exit 0 |
| 2A.63 | ☒ Backend suite green | **277 passed, 1 skipped** |
| 2A.64 | ☒ Security suite green | **202 passed** |
| 2A.65 | ☒ Frontend tests green | **42 passed** |
| 2A.66 | ☒ Frontend typecheck clean | `npm run typecheck` exit 0 |
| 2A.67 | ☒ Frontend build succeeds | `npm run build` exit 0, 7/7 pages |
| 2A.68 | ☒ Frontend lint no worse than baseline | 30 problems vs 33 before |
| 2A.69 | ☒ Upload-to-analysis flow still compatible | `HUDView.test.tsx` (15) still green |
| 2A.70 | ☒ `users.username` unique index created safely | **Closed in Phase 3.5** - `db_init` creates it duplicate-aware; see 2B.16-2B.23 |

---

## 2B. Phase 3.5 security closure

Added 2026-10-19. Evidence in `docs/SECURITY_AUDIT.md` §4 unless stated.
☒ = verified by an automated test or recorded command. ☐ = requires an
operator action, credentials, or a live environment this phase could not reach.

### Rate limiting (S14)

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2B.1 | ☒ `POST /auth/login` is rate limited | `test_repeated_failed_logins_are_eventually_throttled` |
| 2B.2 | ☒ `POST /auth/signup` is rate limited | `test_signup_throttling_is_independent_of_login` |
| 2B.3 | ☒ Login limits are at least as strict as signup per minute, with an hourly ceiling | `test_defaults_are_stricter_for_login_than_for_general_traffic` |
| 2B.4 | ☒ 429 carries `Retry-After`, `X-RateLimit-*` and `Cache-Control: no-store` | `test_throttled_response_carries_retry_after_and_no_store` |
| 2B.5 | ☒ Requests within the limit behave exactly as before | `test_login_within_the_limit_behaves_normally` |
| 2B.6 | ☒ Malformed bodies consume the budget (no bypass) | `test_malformed_requests_consume_the_limit` + non-JSON + empty variants |
| 2B.7 | ☒ A trailing slash cannot evade a rule | `test_trailing_slash_cannot_evade_the_rule` |
| 2B.8 | ☒ The budget recovers after the window | `test_budget_recovers_after_the_window` |
| 2B.9 | ☒ Spoofed `X-Forwarded-For` cannot mint new buckets | `test_spoofed_forwarded_header_cannot_mint_new_buckets` |
| 2B.10 | ☒ Limits are configurable and validated at startup | `test_limits_are_configurable`, `test_malformed_limit_string_fails_at_startup` |
| 2B.11 | ☒ Throttle logs contain no username or password | `test_throttle_logging_does_not_include_credentials` |
| 2B.12 | ☒ Other routes are unaffected by throttling | `test_throttling_does_not_affect_other_routes` |
| 2B.13 | ☐ Decide how limits behave across multiple workers/instances | **Counters are per-process**; the limit is multiplied by worker count. Not distributed protection. `SECURITY_OPERATIONS.md` §3.2 |
| 2B.14 | ☐ Set `TRUSTED_PROXY_COUNT` to the real proxy depth | Default 0 ignores `X-Forwarded-For`; behind a proxy all clients otherwise share one bucket |
| 2B.15 | ☐ Per-account login throttle | **Not implemented**: limits are per-client, so a rotating attacker can still target one account slowly |

### Account and ownership integrity (S16)

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2B.16 | ☒ Duplicate usernames detected before index creation | `test_duplicates_are_counted_accurately` |
| 2B.17 | ☒ Duplicate report exposes counts only, never usernames | `test_duplicate_report_never_exposes_usernames` |
| 2B.18 | ☒ A blocked migration modifies no account | `test_no_account_is_modified_or_removed_by_a_blocked_migration` |
| 2B.19 | ☒ Index creation is idempotent and repeatable | `test_index_creation_is_idempotent` |
| 2B.20 | ☒ Startup survives duplicates without crashing | `test_initialize_database_is_safe_with_duplicates` |
| 2B.21 | ☒ Signup handles `DuplicateKeyError` as a clean 400 | `test_signup_survives_a_duplicate_key_race` |
| 2B.22 | ☒ New usernames reject surrounding whitespace | 4 parametrised cases |
| 2B.23 | ☒ Existing untrimmed usernames still log in | `test_existing_untrimmed_username_can_still_log_in` |
| 2B.24 | ☒ New uploads persist the authenticated owner | `test_upload_persists_the_authenticated_owner` |
| 2B.25 | ☒ Ownership cannot be claimed by supplying an id | `test_ownership_cannot_be_claimed_by_supplying_an_id` |
| 2B.26 | ☒ One `video_id` cannot acquire two owners | `test_duplicate_video_id_cannot_create_a_second_owner` |
| 2B.27 | ☒ A failed ownership write leaves no accessible upload | `test_failed_ownership_write_leaves_no_accessible_upload` |
| 2B.28 | ☒ Missing ownership metadata denies access by default | `test_missing_ownership_metadata_denies_access` |
| 2B.29 | ☒ Ownership enforced on every video route | `test_ownership_is_enforced_on_every_video_route` |
| 2B.30 | ☐ Run duplicate-username detection against the real database | `SECURITY_OPERATIONS.md` §4.1 |
| 2B.31 | ☐ Resolve any duplicates found, by hand | No account is ever deleted or merged automatically — §4.3 |
| 2B.32 | ☐ Decide what happens to pre-Phase-3 videos | Keep inaccessible / verified backfill / authorized deletion — §6. **No ownership is ever silently granted** |

### Media tokens and logging (S15)

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2B.33 | ☒ Token is scoped to one video and one user | `test_media_token_names_exactly_one_video`, `test_wrong_user_token_is_rejected` |
| 2B.34 | ☒ Token grants read-only (not usable as a session credential) | `test_media_token_grants_read_only` |
| 2B.35 | ☒ A token for one video cannot fetch another | `test_token_for_one_video_cannot_fetch_another` |
| 2B.36 | ☒ Token cannot read arbitrary files | `test_token_cannot_fetch_arbitrary_files` (5 probes) |
| 2B.37 | ☒ Forged, expired and missing tokens rejected | 3 tests |
| 2B.38 | ☒ Lifetime is short and configurable | `test_media_token_lifetime_is_configurable` |
| 2B.39 | ☒ Response sets explicit `video/*` and `nosniff` | `test_media_response_sets_safe_headers` |
| 2B.40 | ☒ Tokens redacted from this process's logs | `test_access_log_filter_redacts_the_token` |
| 2B.41 | ☒ Redaction handles URL objects, not only strings | same test (caught a real gap) |
| 2B.42 | ☒ Ordinary log lines are untouched by the filter | `test_filter_leaves_ordinary_log_lines_untouched` |
| 2B.43 | ☒ No token in logs during a real request or an induced failure | 2 tests |
| 2B.44 | ☒ Tokens are not persisted in ownership records | `test_ownership_record_does_not_store_the_token` |
| 2B.45 | ☒ Token-bearing URLs no longer read from browser storage | dead `novaflow_submissions` read removed from `HUDView` |
| 2B.46 | ☐ Configure proxy/CDN/platform log redaction for `?access_token=` | **The app cannot do this** — §8.3 |
| 2B.47 | ☐ Set a `Referrer-Policy` so tokenised URLs are not sent onward | §8.3 |
| 2B.48 | ☐ Accept or design away tokens-in-URLs (browser history) | Structural; signed cookies or a streaming proxy would be the fix |

### AWS least privilege (S17)

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2B.49 | ☒ Required permissions derived from actual boto3 calls | `infra/iam_policy_least_privilege.json`, validated JSON |
| 2B.50 | ☒ Bedrock scoped to `amazon.nova-pro-v1:0` (was `Resource: '*'`) | template `BedrockInvokeNovaProOnly` |
| 2B.51 | ☒ DynamoDB permissions specified (were **missing entirely**) | template `DynamoDb*Table` |
| 2B.52 | ☒ S3 scoped to the `frames/` and `raw/` prefixes | template `S3FrameAndReelObjects` |
| 2B.53 | ☒ TLS required via `aws:SecureTransport` deny | template `DenyUnencryptedTransport` |
| 2B.54 | ☒ Withheld permissions recorded with reasons | template `_NOT_GRANTED` |
| 2B.55 | ☒ No account id, bucket name or table ARN invented | placeholders only; verified no 12-digit literals |
| 2B.56 | ☐ Resolve the bucket naming conflict (`novaflow-media` vs `brand-guardian-*`) | §7.1 — must be settled before writing a policy |
| 2B.57 | ☐ Fill in the placeholders and attach the role | §7 |
| 2B.58 | ☐ Separate development and production roles/resources | §7.3 |
| 2B.59 | ☐ Apply S3 public-access block | §7.4 — expressed in `infra/cloudformation/storage.yaml`, never deployed |
| 2B.60 | ☐ Enable bucket encryption and versioning | §7.4 |
| 2B.61 | ☐ Add a bucket-level TLS deny policy | §7.4 |
| 2B.62 | ☐ Enable access logging or CloudTrail data events | §7.4 — today there is **no record of who read a reel** |
| 2B.63 | ☐ Create the DynamoDB tables (no IaC does) | §7.5 |

### Configuration and operations

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2B.64 | ☒ All 25 code-read env vars documented in `.env.example` | automated cross-check against source |
| 2B.65 | ☒ `.env.example` holds placeholders only; `JWT_SECRET_KEY` empty | inspected |
| 2B.66 | ☒ Real `.env`, keys, and credential files cannot be committed | `git add --dry-run` refuses `.env`; `.gitignore` covers keys |
| 2B.67 | ☒ Operator runbook exists | `docs/SECURITY_OPERATIONS.md` |
| 2B.68 | ☐ Generate and configure `JWT_SECRET_KEY` in the deployment | §1 — production will not start without it |
| 2B.69 | ☐ Accept that rotating the key logs every user out | §1.3 |
| 2B.70 | ☐ Set `CORS_ALLOWED_ORIGINS` to the real frontend origin | §2 |

### Tests and verification

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2B.71 | ☒ Backend suite green | **363 passed, 14 skipped** |
| 2B.72 | ☒ Rate-limit tests green | **30 passed** |
| 2B.73 | ☒ DB-integrity tests green | **26 passed** |
| 2B.74 | ☒ Media-token tests green | **20 passed** |
| 2B.75 | ☒ Frontend tests green | **50 passed** |
| 2B.76 | ☒ Frontend typecheck clean | `npm run typecheck` exit 0 |
| 2B.77 | ☒ **Zero** frontend lint errors (was 8) | `npm run lint`: 22 problems, 0 errors |
| 2B.78 | ☒ Frontend build succeeds | `npm run build` exit 0, 7/7 pages |
| 2B.79 | ☒ Clean install from the manifest succeeds | fresh venv, 18 modules import |
| 2B.80 | ☒ Phase 2 upload-to-analysis flow still compatible | `HUDView.test.tsx` (15) still green |
| 2B.81 | ☒ Real-MongoDB integration tests written and safety-guarded | 10 guard tests pass without a database |
| 2B.82 | ☐ **Real-MongoDB integration tests EXECUTED** | **NOT VERIFIED**: no MongoDB reachable, Docker daemon not responding. 13 tests skip. Run `pytest -m requires_mongodb` with `MONGODB_TEST_URL` set |
| 2B.83 | ☐ Staging smoke test performed | §9.3 — needs real credentials |
| 2B.84 | ☐ Live AWS/Bedrock/MongoDB verification | **Nothing live was called in Phase 3 or 3.5**; all mocked |
| 2B.85 | ☐ Remaining 22 frontend lint warnings addressed | 17 unused vars + 5 `<img>`; none security-relevant, left documented rather than suppressed |

---

## 2C. Phase 4 end-to-end pipeline

Added 2026-10-19. Evidence in `docs/PIPELINE_ARCHITECTURE.md` §10 unless
stated. ☒ = verified by an automated test or recorded command, with every
external service mocked. ☐ = needs live infrastructure or an operator action.

**A mocked pipeline test is not evidence that live AWS or Bedrock works.**

### Upload and job registration

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.1 | ☒ Authenticated upload creates a durable job record | `test_upload_creates_a_job_in_the_uploaded_state` |
| 2C.2 | ☒ A new upload is `uploaded`, not `active` | same test (`is_active: false`) |
| 2C.3 | ☒ Ownership persisted from the token, never the request | Phase 3 tests, still green |
| 2C.4 | ☒ Failed ownership write leaves no accessible orphan | `test_failed_ownership_write_leaves_no_accessible_upload` |
| 2C.5 | ☒ Unsupported format / empty / oversized uploads rejected | `test_security_uploads.py` (44), still green |
| 2C.6 | ☒ Storage keys and filenames fully server-generated | `test_s3_keys_and_frame_numbers_are_server_generated` |
| 2C.7 | ☐ Source videos stored durably | **Not done**: uploads live on the app host's local disk; a replaced container loses them |

### State model

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.8 | ☒ Six validated states with documented transitions | `test_pipeline_state.py` (28) |
| 2C.9 | ☒ `completed` reachable only from `analyzing` | `test_completed_is_only_reachable_from_analyzing` |
| 2C.10 | ☒ Queueing work can never report completion | `test_analyze_queues_the_job_without_claiming_completion` |
| 2C.11 | ☒ Every non-terminal state can reach `failed` | `test_every_non_terminal_state_can_fail` |
| 2C.12 | ☒ Illegal transitions refused and persist nothing | `test_illegal_transition_is_refused_and_persists_nothing` |
| 2C.13 | ☒ Transitions are conditional (no double-advance) | `test_transition_with_a_wrong_expected_state_is_refused` |
| 2C.14 | ☒ Progress updates cannot change state | `test_record_progress_never_changes_state` |
| 2C.15 | ☒ Pre-Phase-4 records remain readable | `test_coerce_handles_legacy_and_junk_values`, `test_job_view_of_a_legacy_record_is_safe` |
| 2C.16 | ☒ Timestamps are UTC ISO strings | `test_timestamps_are_utc_iso_strings` |
| 2C.17 | ☒ Stages are named, never fabricated percentages | `test_stage_labels_are_not_percentages` |
| 2C.18 | ☒ `job_view` omits owner and storage details | `test_job_view_omits_server_side_storage_details` |

### Frame extraction

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.19 | ☒ Failure raises instead of returning silently | `test_pipeline_extraction.py` (17) |
| 2C.20 | ☒ Corrupt input, no video stream, bad duration all fail | 4 tests |
| 2C.21 | ☒ Over-duration clip reports both lengths | `test_overlong_video_is_rejected_with_a_clear_reason` |
| 2C.22 | ☒ Frame count capped; truncation recorded | `test_frame_count_is_capped` |
| 2C.23 | ☒ Zero frames extracted is a failure | `test_no_frames_extracted_is_a_failure` |
| 2C.24 | ☒ FFmpeg non-zero exit and timeout handled | 2 tests |
| 2C.25 | ☒ Timestamps map to the source video | `test_frame_timestamps_map_to_the_source_video` |
| 2C.26 | ☒ S3 failure aborts rather than reusing a frame number | `test_s3_failure_aborts_rather_than_reusing_a_frame_number` |
| 2C.27 | ☒ Subprocess released on success and failure | 2 tests |
| 2C.28 | ☒ Error text carries no path or credential | `test_extraction_errors_never_carry_a_path_or_credential` |
| 2C.29 | ☐ Real FFmpeg extraction | **Not verified**: binary absent. One test marked `requires_ffmpeg` skips |

### Nova Pro analysis

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.30 | ☒ Configured Nova Pro model, centralised, not env-switchable | `test_request_uses_the_configured_nova_model` |
| 2C.31 | ☒ Request carries the frame and the system prompt | `test_request_body_carries_the_frame_and_the_system_prompt` |
| 2C.32 | ☒ Responses validated, not blindly trusted | `test_pipeline_analysis.py` (58) |
| 2C.33 | ☒ Multi-object / fenced / prose-wrapped JSON handled | 3 tests |
| 2C.34 | ☒ Malformed response is a failure, not a clean frame | `test_malformed_response_is_a_failure_not_a_clean_frame` |
| 2C.35 | ☒ Unusable bounding boxes dropped, detection kept | 11 parametrised cases |
| 2C.36 | ☒ Confidence kept only when meaningful | 11 parametrised cases |
| 2C.37 | ☒ Unknown risk level becomes null, not a guess | `test_unknown_risk_level_becomes_none_not_a_guess` |
| 2C.38 | ☒ Throttling retried with bounded backoff | `test_throttling_is_retried_then_succeeds`, `test_retries_are_bounded` |
| 2C.39 | ☒ Permanent errors not retried | 4 parametrised codes |
| 2C.40 | ☒ Frame content never logged | `test_analysis_failure_never_logs_frame_content` |
| 2C.41 | ☐ Live Bedrock inference | **Not verified**: no model was invoked |
| 2C.42 | ☐ Detection accuracy / precision / recall | **Not measured**. No claim is made |
| 2C.43 | ☐ Configurable brand rules | **Not implemented**: one static prompt; documented as a limitation |

### Persistence

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.44 | ☒ Frame metadata and detections persisted to DynamoDB | `test_successful_analysis_is_persisted_to_dynamodb` |
| 2C.45 | ☒ Frame images in S3, never in DynamoDB | `_store_frame`; `test_s3_keys_...` |
| 2C.46 | ☒ Model id and pipeline version stored for traceability | same test |
| 2C.47 | ☒ Writes idempotent; a retry overwrites, not appends | `test_rerunning_a_completed_job_does_not_duplicate_counts` |
| 2C.48 | ☒ Failed frame stores an empty detection list | `test_bedrock_error_is_not_recorded_as_a_successful_audit` |
| 2C.49 | ☒ Absent optional fields omitted, not defaulted | `test_violations_omits_absent_optional_fields` |
| 2C.50 | ☒ Result queries paginate | `test_violations_paginates`, scan pagination in `check_pending_frames` |
| 2C.51 | ☒ Persistence failure does not report success | `test_completion_write_failure_does_not_report_success` |
| 2C.52 | ☒ No new AWS actions needed (IAM template unchanged) | `docs/PIPELINE_OPERATIONS.md` §3 |
| 2C.53 | ☐ DynamoDB tables created | **No IaC**. Required schema documented in Operations §3 |
| 2C.54 | ☐ Live S3 / DynamoDB writes | **Not verified**: mocked throughout |

### Execution, failure, retry

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.55 | ☒ Job durably queued before execution begins | `test_analyze_queues_the_job_without_claiming_completion` |
| 2C.56 | ☒ Worker refuses a job it cannot claim | `test_worker_refuses_a_job_it_cannot_claim` |
| 2C.57 | ☒ Concurrent duplicate processing prevented | `test_concurrent_workers_cannot_both_run_one_job` |
| 2C.58 | ☒ Duplicate submission returns 409 | `test_duplicate_analyze_is_refused_with_409` |
| 2C.59 | ☒ Failure at each stage persists a safe terminal state | 4 stage-failure tests |
| 2C.60 | ☒ Internal detail never reaches the client | `test_unexpected_extraction_crash_does_not_leak_internals` |
| 2C.61 | ☒ Retry re-runs and can succeed | `test_retry_of_a_failed_job_reruns_and_can_succeed` |
| 2C.62 | ☒ Retries bounded by MAX_ATTEMPTS, then 429 | `test_retry_is_bounded_by_max_attempts` |
| 2C.63 | ☒ Stale error cleared on re-queue | `test_requeue_increments_attempt_and_clears_the_old_error` |
| 2C.64 | ☒ Stuck jobs are detectable | `test_find_stuck_jobs_reports_orphans` |
| 2C.65 | ☐ Crash recovery | **Not provided**, and not claimed. In-process worker; see Operations §6 |

### API

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.66 | ☒ 18 routes registered; OpenAPI generates | `python -c "import app"` → 18 paths |
| 2C.67 | ☒ Every pipeline route authenticates | `test_job_endpoint_requires_authentication` + Phase 3 suite |
| 2C.68 | ☒ Cross-user access denied with 404, not 403 | 4 cross-user tests |
| 2C.69 | ☒ Unknown resource indistinguishable from forbidden | same |
| 2C.70 | ☒ Store failure returns 502 with no provider detail | `test_violations_store_failure_is_reported_safely` |
| 2C.71 | ☒ Malformed cursor returns 400 | `test_violations_rejects_a_malformed_cursor` |
| 2C.72 | ☒ No duplicate or conflicting routes | route listing reviewed |

### Frontend

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.73 | ☒ Upload sends authentication and adopts the real job id | `HUDView.test.tsx` (15) |
| 2C.74 | ☒ Polls the job endpoint while non-terminal | `HUDView.job.test.tsx` (24) |
| 2C.75 | ☒ Polling stops on completion, failure, and unmount | 3 tests |
| 2C.76 | ☒ Requests never overlap | `test never issues overlapping job requests` |
| 2C.77 | ☒ Does not poll before a job exists | `does not poll before a job exists` |
| 2C.78 | ☒ Shows real stages and counts, never a percentage | `shows real extracted counts, never a percentage` |
| 2C.79 | ☒ Completed-with-zero-violations shown as complete | `reports a completed audit with zero violations as complete` |
| 2C.80 | ☒ Failed-with-zero-detections NOT shown as compliant | `does NOT claim completion when analysis failed with zero detections` |
| 2C.81 | ☒ Results loaded only after the backend reports completion | `does not load results while the job is non-terminal` |
| 2C.82 | ☒ Backend error summary displayed, not an invented one | `shows the backend's error summary, not an invented one` |
| 2C.83 | ☒ 401 / 403 / 404 / 429 each handled correctly | 4 tests |
| 2C.84 | ☒ Network interruption does not fabricate completion | `a network failure does not fabricate completion` |
| 2C.85 | ☒ Retry offered only when the backend permits it | 4 retry tests |
| 2C.86 | ☒ No media token persisted in browser storage | dead `localStorage` read removed in Phase 3.5 |

### Checks run

| # | Item | Evidence |
| :-- | :-- | :-- |
| 2C.87 | ☒ Backend suite green | **500 passed, 15 skipped** |
| 2C.88 | ☒ Pipeline suites green | state 28, e2e 34, extraction 17+1 skip, analysis 58 |
| 2C.89 | ☒ Frontend tests green | **74 passed** (4 files) |
| 2C.90 | ☒ Frontend typecheck clean | `npm run typecheck` exit 0 |
| 2C.91 | ☒ Frontend lint: 0 errors | 22 warnings, same as the Phase 3.5 baseline |
| 2C.92 | ☒ Frontend build succeeds | `npm run build` exit 0, 7/7 pages |
| 2C.93 | ☒ App imports; OpenAPI generates | 18 paths |
| 2C.94 | ☒ All 30 env vars documented | automated cross-check against source |
| 2C.95 | ☐ Live end-to-end run against AWS | **Not performed**. Staging procedure in Operations §8 |
| 2C.96 | ☐ Deployment | **Not performed** |

---

## 3. Reliability

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 3.1 | ☐ Jobs survive a process restart, or fail visibly — never stuck pending | 6 | |
| 3.2 | ☐ No pipeline exit path leaves a job without a terminal status *(currently two silent drop-outs — audit §9.10)* | 4, 6 | |
| 3.3 | ☐ Explicit timeouts on every external call *(currently none)* | 6 | |
| 3.4 | ☐ Retries with backoff for throttling | 6 | |
| 3.5 | ☐ `/analyze` is idempotent; duplicate jobs are prevented | 4, 6 | |
| 3.6 | ☐ Frame-key collision bug fixed *(a failed store causes key reuse — audit §10.4)* | 4 | |
| 3.7 | ☐ Temp directories and uploads are cleaned up *(cleanup currently commented out)* | 6 | |
| 3.8 | ☐ Bounded memory and disk use for large videos | 6 | |
| 3.9 | ☐ No full-table `Scan` in a request-serving path | 6 | |
| 3.10 | ☐ `/health` reflects real dependency status *(currently a static literal)* | 6 | |
| 3.11 | ☐ Structured logging with per-job correlation ids | 6 | |
| 3.12 | ☐ Data integrity verified across every pipeline stage | 9 | |
| 3.13 | ☐ A malformed or corrupt video fails gracefully with a clear user-facing error | 3, 4 | |

---

## 4. Tests and CI

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 4.1 | ☐ A test suite exists *(currently **zero tests** — audit §8.2)* | 2 | |
| 4.2 | ☐ Backend unit tests pass; output quoted | 2+ | |
| 4.3 | ☐ Frontend component tests pass; output quoted | 7 | |
| 4.4 | ☐ Integration tests cover the full pipeline with AWS stubbed | 10 | |
| 4.5 | ☐ At least one end-to-end test: login → upload → analyze → review | 10 | |
| 4.6 | ☐ Regression test for each critical defect in audit §9.1-9.4 | 2, 8, 9 | |
| 4.7 | ☐ No test calls real AWS or incurs cost; credential-dependent tests are skipped by default and documented | 10 | |
| 4.8 | ☐ Coverage meets the agreed target | 10 | |
| 4.9 | ☐ `pytest` green on a clean checkout | 10 | |
| 4.10 | ☐ `npm run lint` green, with a real target *(currently `"eslint"` with no path)* | 7 | |
| 4.11 | ☐ `npx tsc --noEmit` green; a `typecheck` script exists | 7 | |
| 4.12 | ☐ `npm run build` green | 7 | |
| 4.13 | ☐ CI workflow exists and gates merges *(no `.github/` today)* | 10 | |
| 4.14 | ☐ CI runs lint, types, tests, build, secret scan, and dependency audit | 10 | |
| 4.15 | ☐ CI green on the release commit | 10 | |

---

## 5. Documentation

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 5.1 | ☐ `README.md` rewritten; every claim verifiable *(currently materially inaccurate — audit §11.1)* | 10 | |
| 5.2 | ☐ Setup instructions complete: prerequisites, deps, env vars, AWS, FFmpeg, run commands | 10 | |
| 5.3 | ☐ A fresh clone can be set up from the README alone — **verified by doing it** | 10 | |
| 5.4 | ☐ Architecture documentation matches the verified implementation | 10 | |
| 5.5 | ☐ Architecture diagram included | 10 | |
| 5.6 | ☐ `.env.example` lists every variable by name, with no values | 3 | |
| 5.7 | ☐ API documentation accurate; `/openapi.json` includes auth routes *(currently excluded by `app.mount` — audit §6.2)* | 3, 10 | |
| 5.8 | ☐ `docs/planning.md` labelled as a design target, with unimplemented parts marked | 10 | |
| 5.9 | ☐ `SKILL.md` bounding-box schema matches the code *(currently matches nothing — audit §11.2)* | 4 | |
| 5.10 | ☐ `CLAUDE.md` updated to the post-release state | 10 | |
| 5.11 | ☐ `docs/PROJECT_AUDIT.md` findings all resolved or formally accepted as known limitations | 10 | |
| 5.12 | ☐ `CONTRIBUTING.md` present | 10 | |
| 5.13 | ☐ Known limitations documented honestly | 10 | |

---

## 6. GitHub hygiene

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 6.1 | ☐ Repository rooted at the project directory, **not** the user's Desktop *(audit §12.1 — **BLOCKER**)* | 0 | |
| 6.2 | ☐ `git status` shows no personal or unrelated files *(currently 37,558 untracked at the repo root)* | 0 | |
| 6.3 | ☐ Remote verified as `YushBytes/Amazon_Nova_Project`; no second repository created | 0 | |
| 6.4 | ☐ The remote's original commit `e059a9c` preserved in history; no force-push used | 0 | |
| 6.5 | ☐ The full codebase is actually pushed *(only 5 of ~130 files are on the remote — audit §12.3)* | 0 | |
| 6.6 | ☐ `.gitignore` covers uploads, temp dirs, build artifacts, caches, and OS/editor files | 0 | |
| 6.7 | ☐ `.gitattributes` present *(`core.autocrlf=true` with no `.gitattributes` causes spurious diffs)* | 0 | |
| 6.8 | ☐ `.claude/settings.local.json` untracked and ignored | 0 | |
| 6.9 | ☐ `LICENSE` present | 0 | |
| 6.10 | ☐ No large generated files, lockfile orphans, or build output tracked *(e.g. the stray 196 KB `package-lock.json` — audit §2.3)* | 5 | |
| 6.11 | ☐ Dead code and duplicates removed, each justified | 5 | |
| 6.12 | ☐ Commit history is clean and meaningful | 10 | |
| 6.13 | ☐ Repository visibility is an intentional decision, made after the Phase 3 security fixes | 0, 10 | |
| 6.14 | ☐ Release tagged | 10 | |

---

## 7. Deployment readiness

| # | Item | Phase | Evidence |
| :-- | :-- | :-- | :-- |
| 7.1 | ☐ Backend deployment target chosen and configured *(only `amplify.yml` for the frontend exists today)* | 5, 10 | |
| 7.2 | ☐ `docker build` succeeds and the container starts *(would currently fail — `boto3` missing from `requirements.txt`)* | 2, 10 | |
| 7.3 | ☐ Python version consistent between the Dockerfile and development *(3.11 vs 3.14.6)* | 2 | |
| 7.4 | ☐ FFmpeg available in every runtime environment | 2 | |
| 7.5 | ☐ All required env vars documented and set in the deployment environment | 3, 10 | |
| 7.6 | ☐ IaC covers S3, DynamoDB, and IAM *(DynamoDB tables currently exist only implicitly in code)* | 5 | |
| 7.7 | ☐ Bedrock model access granted for every model used, in the deployment region | 4, 8 | |
| 7.8 | ☐ Frontend builds and deploys on Amplify | 10 | |
| 7.9 | ☐ Frontend and backend communicate in the deployed environment — no hardcoded `localhost` *(currently `app.py:177`)* | 4, 10 | |
| 7.10 | ☐ A smoke test passes against the deployed environment | 10 | |
| 7.11 | ☐ Monitoring and alerting configured, or the gap explicitly accepted | 10 | |
| 7.12 | ☐ Bedrock cost per job understood and bounded | 10 | |
| 7.13 | ☐ Rollback plan documented | 10 | |

---

## 8. Final release gate

All must hold, each with evidence recorded above:

- [ ] Every critical and high finding in `docs/PROJECT_AUDIT.md` §9 and §10 is resolved or formally accepted as a documented limitation.
- [ ] Every roadmap phase 0-10 is accepted.
- [ ] CI is green on the release commit.
- [ ] No mock, hardcoded value, or silent fallback is presented anywhere as real AI output.
- [ ] No feature in the UI or documentation is non-functional.
- [ ] A full manual end-to-end run has been completed, with evidence.
- [ ] Every blocker in audit §13 is resolved.
- [ ] A maintainer has reviewed and signed off.

**Sign-off:** _____________________  **Date:** ____________
