# Security Operations Runbook

**NovaFlow / Brand Guardian**
**Last updated:** 2026-10-19 (Phase 3.5)

Procedures an operator must perform. Everything here requires credentials,
infrastructure access, or a data decision that the codebase cannot make for
itself.

Nothing in this document contains a real secret, account id, bucket name, or
username. Commands that would print a secret are written so they do not.

**Read first:** these procedures are ordered. Section 1 is a hard prerequisite
for the application starting in production at all.

---

## Contents

1. [JWT secret configuration and old-token invalidation](#1-jwt-secret)
2. [Production CORS configuration](#2-cors)
3. [Rate limiting: configuration and its real limits](#3-rate-limiting)
4. [Duplicate usernames and the unique index](#4-username-uniqueness)
5. [Video ownership index initialization](#5-ownership-indexes)
6. [Legacy videos with unknown ownership](#6-legacy-videos)
7. [AWS least-privilege configuration](#7-aws)
8. [Logs and media-token URLs](#8-logs)
9. [Staging verification](#9-staging)
10. [Rollback](#10-rollback)
11. [What is still not covered](#11-gaps)

---

<a name="1-jwt-secret"></a>
## 1. JWT secret configuration and old-token invalidation

**Why.** The signing key was previously hardcoded in `backend/auth.py`
(finding S1). Any token signed with it must be considered forgeable. The
application now **refuses to start** in production without a valid key.

### 1.1 Generate a key locally

Run on a trusted machine. This prints the value once; paste it straight into
your secret manager and do not save it to a file or shell history.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Requirements enforced by `backend/config.py`:

- at least 32 characters;
- not one of `config.KNOWN_INSECURE_SECRETS` (which permanently includes the
  old hardcoded value, so it cannot be reintroduced even by accident).

### 1.2 Configure it

Set `JWT_SECRET_KEY` in your platform's secret store — not in a file, not in
the image, not in `.env`:

| Platform | Where |
| :-- | :-- |
| AWS ECS / App Runner | Secrets Manager or SSM Parameter Store, injected as an env var |
| AWS Lambda | Lambda environment variable encrypted with a KMS key |
| Kubernetes | a `Secret`, mounted as an env var |
| Local development | leave it **unset**; an ephemeral per-process key is generated |

Also set `NOVAFLOW_ENV=production`, or none of the production checks apply.

### 1.3 Invalidate tokens signed with the old key

Changing the key is the invalidation. Every previously issued token fails
signature verification immediately, because the accepted algorithm and key are
pinned in `auth._decode`.

Consequences to plan for:

- every signed-in user is logged out at their next request;
- the frontend handles this: `AuthContext` calls `GET /auth/me` on load and
  clears the stored session on a 401;
- outstanding media tokens (`?access_token=…`) also stop working, so an
  in-progress video playback will fail and need a page refresh.

Do this during a maintenance window if that matters. There is no way to
invalidate selectively — see §11.

### 1.4 Verify without printing the secret

```bash
# Must FAIL (exit non-zero) with a clear error:
NOVAFLOW_ENV=production python -c "import app"

# Must also fail, proving the blocklist works:
NOVAFLOW_ENV=production JWT_SECRET_KEY='brand-guardian-dev-secret-change-in-prod' \
  python -c "import app"

# With the real key in the environment, must succeed and print nothing secret:
NOVAFLOW_ENV=production python -c "import app; print('startup OK')"
```

`Settings.__repr__` redacts the key, so it cannot leak through a traceback.
**Never** run `python -c "import config; print(config.get_settings().jwt_secret)"`.

---

<a name="2-cors"></a>
## 2. Production CORS configuration

The default is `http://localhost:3000`, which is wrong everywhere except a
developer's machine.

```bash
CORS_ALLOWED_ORIGINS=https://app.your-domain.example
# several origins, comma-separated, exact scheme and host:
CORS_ALLOWED_ORIGINS=https://app.your-domain.example,https://staging.your-domain.example
```

`*` is **rejected** by `config.load_cors_origins`: credentialed requests are
enabled, and a wildcard would let any site call the API with a victim's token.
This is deliberate — do not work around it.

---

<a name="3-rate-limiting"></a>
## 3. Rate limiting: configuration and its real limits

### 3.1 Settings

| Variable | Default | Notes |
| :-- | :-- | :-- |
| `RATE_LIMIT_ENABLED` | `true` | Leave on. |
| `RATE_LIMIT_LOGIN` | `5/minute;20/hour` | Applies to `POST /auth/login`. |
| `RATE_LIMIT_SIGNUP` | `3/minute;10/hour` | Applies to `POST /auth/signup`. |
| `TRUSTED_PROXY_COUNT` | `0` | See §3.3. |

A malformed limit string fails at startup rather than at the first login.

### 3.2 THE IMPORTANT LIMITATION: counters are per-process

Storage is in-process memory (`limits.storage.MemoryStorage`). **The effective
limit is multiplied by the number of workers or instances you run.** Four
workers with `5/minute` means 20 attempts per minute per client.

This is honest protection for the current single-process deployment
(`backend/Dockerfile` runs one `uvicorn app:app` with no `--workers`). It is
**not** distributed protection, and nothing in this repository claims it is.

Before scaling out, pick one:

| Option | Trade-off |
| :-- | :-- |
| Keep one process, scale vertically | Simplest; the limit stays accurate. |
| Divide the limits by the worker count | Crude but immediate; penalises users unevenly as instances come and go. |
| Move to a shared store | Correct. `limits` supports Redis and Memcached. Requires provisioning that store and changing `RateLimitMiddleware` to use it — a deliberate change, not a config flip. It was **not** enabled here because no such store is provisioned, and shipping an unconfigured production dependency would be worse than the documented limitation. |
| Limit at the edge | An ALB/WAF/CloudFront or API Gateway rule in front of the app. Often the best answer, and it also covers traffic that never reaches the app. |

### 3.3 Reverse proxies and `X-Forwarded-For`

With `TRUSTED_PROXY_COUNT=0` (default) the header is **ignored** and the peer
address is used. That is correct when the app is reached directly.

Behind a proxy, the peer address is the proxy, so every client shares one
bucket and a single user can lock everyone out. Set
`TRUSTED_PROXY_COUNT` to the number of proxies you actually run; the address is
then read that many entries from the **right** of the chain, because proxies
append and the leftmost entry is whatever the client claimed.

> Setting this higher than your real proxy depth lets a client forge its own
> bucket key and defeat the limit entirely. Count carefully, and verify with
> `test_forwarded_header_is_read_from_the_right_when_configured`.

### 3.4 What is not limited

Only the two credential endpoints. Authenticated API traffic, uploads, and
analysis triggers are **not** rate limited. Upload volume is bounded by
`MAX_UPLOAD_BYTES` per request but not by request count; Bedrock spend per
video is bounded by the 60-second clip limit in `extractor.py`, not by a quota.

---

<a name="4-username-uniqueness"></a>
## 4. Duplicate usernames and the unique index

**Why.** `signup` does a read-then-write existence check, which is not atomic.
Without a unique index, two simultaneous registrations can both succeed.

### 4.1 Check for duplicates first

Creating a unique index on a collection that already holds duplicates **fails**.
`db_init` detects this and refuses, reporting counts only — never the
usernames.

```bash
cd backend
python -c "
import anyio, db_init
print(anyio.run(db_init.find_duplicate_username_counts))
"
# -> {'groups': 0, 'extra_documents': 0}  means you are clear
```

`groups` is how many usernames are shared; `extra_documents` is how many
accounts need a decision.

### 4.2 If there are no duplicates

Nothing to do. Application startup calls `db_init.initialize_database`, which
creates the index. `create_index` is idempotent.

To do it deliberately and have failures raised rather than logged:

```bash
cd backend
python -c "
import anyio, db_init
print(anyio.run(lambda: db_init.initialize_database(strict=True)))
"
```

### 4.3 If there ARE duplicates

The application keeps running; it logs an error and skips the index. **No
account is modified.** Resolve them by hand — this is a data decision, and
nothing in the code will make it for you.

Identify them with your own database access (output contains usernames, so
treat it as personal data and do not paste it into a shared channel):

```javascript
db.users.aggregate([
  { $group: { _id: "$username", count: { $sum: 1 }, ids: { $push: "$_id" } } },
  { $match: { count: { $gt: 1 } } }
])
```

Then, per group, choose deliberately:

| Option | When | Caution |
| :-- | :-- | :-- |
| Rename the newer account(s) | The accounts belong to different people | The renamed user must be told; their old name no longer works |
| Delete an unused duplicate | It has no videos and never logged in | Check `videos` for `owner` matching it first |
| Merge | Same person, two records | No merge tooling exists. Ownership records reference `owner` by username string, so a merge means updating `videos.owner` too |

**Never** delete accounts merely to make the index creation succeed.

Note that matching is **case-sensitive and exact**: `Alice` and `alice` are
different accounts today, and so are `alice` and `" alice"`. Phase 3.5 rejects
new usernames with surrounding whitespace, but does not rewrite existing ones,
because trimming a stored username would make that account unreachable.
Normalising case is an available decision with the same data implications as a
merge; it was deliberately not done in code.

---

<a name="5-ownership-indexes"></a>
## 5. Video ownership index initialization

Created automatically at startup by `db_init.initialize_database`:

| Index | Purpose |
| :-- | :-- |
| `videos.video_id` (unique) | One id cannot acquire two owners |
| `videos.owner` | Scoped listing for `GET /submissions` |

Both are idempotent. The unique index can fail only if the collection already
holds two records with the same `video_id`, which the application cannot
produce. If it does fail, investigate before proceeding — it implies a direct
write or a restored backup.

Verify:

```javascript
db.videos.getIndexes()
```

---

<a name="6-legacy-videos"></a>
## 6. Legacy videos with unknown ownership

**The situation.** Before Phase 3, uploads were stored as
`uploads/{video_id}.{ext}` with no record of who uploaded them, and
`NovaFlowFrames` keys only on `video_id`. The uploader's identity was never
recorded anywhere, so it **cannot be recovered from the data**.

Those videos are now **inaccessible** through the API: `require_video_owner`
treats a missing ownership record as a denial, not a pass. That is the correct
default — the alternative is serving one user's footage to another.

Find them (file names only; no personal data):

```bash
# On the application host:
ls backend/uploads/
```

```javascript
// Ids that DO have ownership:
db.videos.distinct("video_id")
```

Anything on disk whose id is absent from that list is a legacy orphan.

### Choose one, explicitly

**Option A — leave inaccessible (default, safest).**
No action. Files stay on disk, unreachable through the API. Choose this if you
cannot establish ownership, or while you decide. Cost: disk usage, and the data
is retained, which may matter for a retention policy.

**Option B — verified manual backfill.**
Only where you can establish the owner from evidence *outside* this system —
an upload confirmation email, a support ticket, a client who can identify their
own footage. Never from a guess, and never from "they are the only user".

```javascript
// Per video, after verifying the owner by external evidence:
db.videos.insertOne({
  video_id: "<EXACT_ID_FROM_FILENAME>",
  owner: "<VERIFIED_USERNAME>",
  stored_filename: "<EXACT_FILENAME>",
  original_filename: null,
  extension: "mp4",
  size_bytes: <ACTUAL_SIZE>,
  status: "UPLOADED",
  created_at: new Date("<UPLOAD_DATE_IF_KNOWN>"),
  backfilled: true,          // marks this as asserted, not observed
  backfill_note: "<WHO_VERIFIED_AND_HOW>"
})
```

The `backfilled` flag matters: it records that ownership was asserted by an
operator rather than captured at upload. Keep it.

**Option C — delete after an authorized retention decision.**
Only with explicit authorization from whoever owns the data policy, and only
after checking there is no legal-hold or retention obligation.

```bash
# Take a backup FIRST, somewhere outside the application host.
# Then remove only ids confirmed absent from db.videos.
```

Also remove the corresponding `NovaFlowFrames` rows and `frames/{video_id}/`
S3 objects, or you keep the derived analysis of deleted footage.

> Nothing in the code does any of this automatically. No ownership is ever
> silently granted, and no file is ever automatically deleted.

---

<a name="7-aws"></a>
## 7. AWS least-privilege configuration

Use `infra/iam_policy_least_privilege.json` as the template. It is a
**template**: every `${PLACEHOLDER}` must be replaced. No account id, bucket
name, or table ARN was invented.

### 7.1 Resolve the bucket naming conflict first

The repository contains two schemes, and this must be settled before writing a
policy:

| Source | Bucket(s) |
| :-- | :-- |
| Application code (`S3_BUCKET_NAME` default) | `novaflow-media` |
| `infra/init_s3.py` | `brand-guardian-raw`, `brand-guardian-final` |

The application only ever reads and writes the single `S3_BUCKET_NAME` bucket,
under the prefixes `frames/` and `raw/`. The `brand-guardian-*` buckets are
**never read by any code path**. Decide which you are actually using, set
`S3_BUCKET_NAME` accordingly, and scope the policy to that one bucket.

### 7.2 What the application actually needs

Derived from the boto3 calls in the code, not from the old policy:

| Service | Actions | Scope |
| :-- | :-- | :-- |
| Bedrock | `bedrock:InvokeModel` | `amazon.nova-pro-v1:0` only |
| S3 | `GetObject`, `PutObject` | `${S3_BUCKET_NAME}/frames/*`, `${S3_BUCKET_NAME}/raw/*` |
| S3 | `ListBucket` | bucket, condition-limited to those two prefixes |
| DynamoDB | `PutItem`, `GetItem`, `UpdateItem`, `Query`, `Scan` | frames table |
| DynamoDB | `PutItem`, `GetItem`, `UpdateItem`, `Scan` | projects table |
| CloudWatch Logs | `CreateLogStream`, `PutLogEvents` | this app's log group |

Corrections to the previous `infra/iam_policy.yaml`:

- `bedrock:InvokeModel` was on `Resource: '*'` — every model in the account.
  Now scoped to the one model actually invoked.
- **DynamoDB permissions were missing entirely.** The application could not
  have worked under that role.
- Bucket ARNs were hardcoded to `brand-guardian-*`, which no code reads.
- `bedrock:InvokeModelWithResponseStream` was granted; no code streams.

Deliberately **not** granted (see `_NOT_GRANTED` in the template):
Nova Canvas (remediation is disabled), `bedrock:ApplyGuardrail`
(`safety.py` is never imported), Step Functions (client built, never used),
`s3:DeleteObject`, `dynamodb:DeleteItem`, `s3:CreateBucket`,
`dynamodb:CreateTable`, `logs:CreateLogGroup`.

> Never broaden a permission to make a failing call succeed. If a call fails,
> first confirm the application is supposed to make it.

### 7.3 Separate development from production

Separate roles, separate buckets and tables, separate Bedrock model access.
A development role must never be able to read production footage. Do not share
one role across environments "for convenience".

### 7.4 Bucket hardening (not expressible in an identity policy)

`infra/init_s3.py` creates buckets with **none** of these. Apply them to the
bucket itself:

- **Public access block**: all four settings true.
- **Default encryption**: SSE-S3 minimum, or SSE-KMS with a CMK.
- **Versioning**: enabled, so an overwritten reel is recoverable.
- **Bucket policy**: explicit `Deny` when `aws:SecureTransport` is `false`, so
  the TLS requirement does not depend on the caller's identity policy.
- **Object ownership**: `BucketOwnerEnforced`, which disables ACLs.
- **Lifecycle**: expire or transition `frames/` and `raw/` per your retention
  decision (§6).
- **Access logging or CloudTrail data events**: neither is configured, so today
  there is **no record of who read a reel**.

### 7.5 DynamoDB

Tables are not created by any IaC in this repository. Create
`${DYNAMODB_FRAMES_TABLE}` (partition `video_id` S, sort `frame_number` N) and
`${DYNAMODB_PROJECTS_TABLE}` (partition `project_id` S, sort `sk` S) with
encryption at rest enabled and point-in-time recovery if the data matters.

> No live AWS call was made while preparing any of this, and no live IAM role,
> bucket policy, or table was changed.

---

<a name="8-logs"></a>
## 8. Logs and media-token URLs

### 8.1 The problem

A browser `<video>` element cannot send an `Authorization` header, so video
playback uses a short-lived token in the query string:

```
GET /uploads/vid_xxxxxxxx.mp4?access_token=<token>
```

The request target is therefore a credential for 15 minutes
(`MEDIA_TOKEN_TTL_MINUTES`), scoped to that one video and that one user.

### 8.2 What the application does

`backend/logging_setup.py` installs a filter that rewrites sensitive query
values to `REDACTED` before any record is formatted, on `uvicorn.access`,
`uvicorn.error`, `httpx`, `httpx2`, and `httpcore`. It handles both string and
URL-object arguments. The unhandled-exception handler logs `request.url.path`
only, never the query.

### 8.3 What it CANNOT do — your responsibility

This filter only affects logging **inside this process**. The token is still
visible to:

| Place | What to do |
| :-- | :-- |
| ALB / NLB / CloudFront access logs | Disable query-string logging, or redact at the log pipeline |
| API Gateway execution logs | Do not log full request paths |
| Nginx / Apache in front of the app | `log_format` without `$query_string`, or a `map` to strip it |
| CloudWatch Logs Insights, Datadog, Splunk | Add a redaction rule at ingest |
| Browser history | Unavoidable while playback uses a query token |
| `Referer` header to third parties | Send `Referrer-Policy: no-referrer` or `same-origin` from the frontend |
| CDN edge caches | Ensure tokenised media is not cached shared; the 429 path already sets `Cache-Control: no-store` |

Keep `MEDIA_TOKEN_TTL_MINUTES` low. The structural fix — signed cookies or a
streaming proxy that takes a header — is a design change, not a setting.

### 8.4 What is never logged

Verified by tests: passwords, password hashes, JWTs, signing keys, media
tokens, and model analysis bodies (which describe user video content). The
rate limiter logs the path and the rule that fired, never the client identity
or request body.

---

<a name="9-staging"></a>
## 9. Staging verification

Do this on staging before production. It needs real credentials and a real
database, so none of it was performed while writing this.

### 9.1 Prerequisites

- [ ] `JWT_SECRET_KEY` set in the secret store (§1)
- [ ] `NOVAFLOW_ENV=production`
- [ ] `CORS_ALLOWED_ORIGINS` set to the staging frontend origin (§2)
- [ ] MongoDB reachable; `MONGODB_URL` and `MONGODB_DB_NAME` set
- [ ] S3 bucket and both DynamoDB tables exist (§7.5)
- [ ] IAM role attached, from the template (§7)
- [ ] Bedrock model access granted for `amazon.nova-pro-v1:0` in the region
- [ ] FFmpeg present in the runtime (the Dockerfile installs it)

### 9.2 Startup

```bash
# Fails safely without the key:
NOVAFLOW_ENV=production python -c "import app"            # expect non-zero exit

# With it:
python -c "import app; print(len(app.app.openapi()['paths']))"   # expect 16
```

Check the startup log for `Database initialization results:`. If
`user_index` is `False`, you have duplicate usernames — go to §4.3.

### 9.3 Smoke test

Run against staging with a disposable account. Expected results:

| Check | Expect |
| :-- | :-- |
| `GET /status` | 200 |
| `GET /docs` | 404 (docs off in production) |
| `POST /auth/signup` with `role: "agent"` | 200, response role `client` |
| `POST /auth/login`, correct password | 200 with a token |
| `POST /auth/login` x6 rapidly, wrong password | 401s then **429** with `Retry-After` |
| `GET /submissions` with no token | 401 |
| `POST /upload-reel` with a `.html` file | 400 |
| `POST /upload-reel` with a real `.mp4` | 200, `filename` matches `vid_[0-9a-f]{8}\.mp4` |
| `GET /uploads/<file>` without a token | 401 |
| `GET /uploads/<file>?access_token=<from the upload response>` | 200, `video/mp4`, `nosniff` |
| Second account requests the first account's `video_id` | 404 on `/violations` and `/analyze` |
| `POST /analyze/<own id>` | 200, then `/violations` reports frames as they are audited |

Then confirm no credential reached the logs:

```bash
# Expect no output:
grep -iE "access_token=ey|\$argon2|JWT_SECRET" <staging log>
```

### 9.4 Frontend

- [ ] Login works and persists across a refresh
- [ ] An expired or invalid token signs the user out (clear `localStorage`, set a bogus token, reload)
- [ ] Repeated failed logins show the throttling message with a wait time, not a credential error
- [ ] Upload → Launch Audit runs, and the status strip never claims completion
- [ ] Signup shows "New accounts are created as Client" and the Agent option is disabled

---

<a name="10-rollback"></a>
## 10. Rollback

### 10.1 Code

`git revert` the Phase 3.5 commit, or deploy the previous image. **Never**
force-push or rewrite history.

Reverting re-opens every gap it closed — unthrottled credential endpoints,
no unique index, no log redaction. Prefer fixing forward.

### 10.2 What does NOT roll back

| Change | Why it persists |
| :-- | :-- |
| Argon2id password hashes | Migrated on login. Older code can still *verify* them only if it has the Argon2 path — Phase 3 code does. Pre-Phase-3 code **cannot**, and those users would be locked out. |
| `users.username` unique index | Stays until dropped: `db.users.dropIndex("username_1")` |
| `videos` ownership records | Harmless to older code, which ignores the collection |
| A rotated `JWT_SECRET_KEY` | Sessions stay invalidated |

**Do not roll back past Phase 3** once logins have migrated hashes, without
first confirming the target version understands Argon2.

### 10.3 Emergency throttle

To lock down the credential endpoints without a deploy:

```bash
RATE_LIMIT_LOGIN=1/minute;5/hour
RATE_LIMIT_SIGNUP=1/hour
```

Restart. To invalidate every session immediately, rotate `JWT_SECRET_KEY`.

---

<a name="11-gaps"></a>
## 11. What is still not covered

Stated plainly so none of it is mistaken for done.

| Gap | Impact |
| :-- | :-- |
| Rate limiting is per-process | Multiplied by worker count; not distributed (§3.2) |
| No token revocation | A stolen token is valid until expiry unless the key is rotated |
| Tokens in `localStorage` | Readable by any successful XSS |
| Media tokens in URLs | Reach browser history and external logs (§8.3) |
| No per-account login throttle | Limits are per-client; an attacker rotating IPs can still target one account |
| No MFA | — |
| No security headers | CSP, HSTS, `Referrer-Policy` are not set by the app |
| No audit trail | Security events are logged but not tamper-evident; no record of who read a reel |
| No upload content scanning | An extension allowlist does **not** prove a file is a safe video |
| Argon2 parameters unbenchmarked | Library defaults; not tuned to production hardware |
| Real-MongoDB tests unverified | Written and safety-guarded, but never executed: no MongoDB or Docker daemon was available. Run `pytest -m requires_mongodb` against a disposable database |
| No live AWS verification | Nothing in Phase 3 or 3.5 made a real AWS, Bedrock, or MongoDB call |
| Secret scan of full Git history | Not performed (release checklist 2.14) |

---

*See `docs/SECURITY_AUDIT.md` for findings and evidence,
`docs/SECURITY_IMPLEMENTATION_PLAN.md` for implemented-versus-deferred status,
and `docs/RELEASE_CHECKLIST.md` for the release gate.*
