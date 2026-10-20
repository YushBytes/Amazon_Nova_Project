# CLAUDE.md — NovaFlow / Brand Guardian

Guidance for Claude Code sessions in this repository.

**Read `docs/PROJECT_AUDIT.md` before changing anything.** It is an evidence-based audit completed 2026-10-18 with file:line references for every finding. This file summarises what matters operationally; the audit is the authority.

Facts below are marked **[VERIFIED]** (proven during the audit) or **[UNVERIFIED]** (read from source but never executed — most of the backend falls here, because required dependencies and AWS credentials were unavailable). Do not upgrade an [UNVERIFIED] claim without running something.

---

## 1. Critical warnings — read first

### 1.1 Repository root — correct now, but a stray Desktop repo still exists **[VERIFIED 2026-10-18, Phase 0]**

```
git rev-parse --show-toplevel  →  C:/Users/ayush/OneDrive/Desktop/Amazon Nova
```

The project is now a correctly-rooted repository at `C:/Users/ayush/OneDrive/Desktop/Amazon Nova`, tracking `origin/main`. The `Amazon_Nova-main/` zip-extraction layer was flattened in Phase 0, so project files sit at the repository root and the root `.gitignore` governs the whole project.

**The original hazard is contained but not gone.** A `git init` had been run in `C:/Users/ayush/OneDrive/Desktop`, and that repository **still exists** — deliberately preserved and untouched: 0 commits, 0 refs, 0 remotes, and roughly 37,400 untracked files including the user's resumes, personal PDFs, and unrelated projects. It was never committed to and never pushed. Working inside the project directory is safe because the nested `.git` takes precedence, but a session whose working directory is the Desktop would still be operating in that repository.

**Therefore, these practices remain mandatory:**
- **Verify `git rev-parse --show-toplevel` before every commit** — it must print the project root, never the Desktop.
- **Stage files explicitly by path.** Never `git add -A`, `git add .`, or `git commit -a`.
- Never commit from, or add a remote to, the Desktop repository.

### 1.2 The remote now holds the full project, and is private **[VERIFIED 2026-10-18, Phase 0]**

`YushBytes/Amazon_Nova_Project` is **PRIVATE** (confirmed by an unauthenticated API probe returning 404) and `main` now contains the complete project — 136 tracked files.

History is linear, with the original commit preserved as its root:

```
e50ed57  docs: audit existing project and define implementation roadmap
7e1649c  chore: recover repository at project root and import project files
e059a9c  Add files via upload          <- original remote commit, preserved
```

Before Phase 0 the remote held only 5 root-level files; `backend/`, `frontend/`, `infra/`, `docs/`, and `frontend_backup/` had never been pushed. Four of those five files remain byte-identical to `e059a9c`; only `.gitignore` was intentionally extended. **Never force-push** — published history must be preserved.

The repository was made private *before* the backend was first pushed, so `backend/auth.py` and its hardcoded development JWT secret have never been publicly exposed. That secret is still a live defect (§5) and Roadmap Phase 3 replaces it.

### 1.3 The Phase 1 critical defects are fixed **[VERIFIED by test]**

Fixed in Phase 2 (`2028dd5`), each with a regression test that was confirmed to
fail beforehand. Kept here so the history is not lost and the patterns are not
reintroduced.

| Former defect | Location | Resolution |
| :-- | :-- | :-- |
| `json` used but never imported at module scope | `backend/analyzer.py` | Module-scope imports added; each frame now reaches exactly one terminal state (`audited` / `audit_failed`), so a failure can no longer read as a clean frame. |
| async `decode_token` called without `await` | `backend/projects.py` | `get_current_user` is async and awaits it; Phase 3 then centralised it in `auth.py`. |
| Remediation returned its input unchanged | `backend/audit_manager.py` *(file deleted in Phase 5)* | The disguised mock was removed in Phase 2; the whole module went in Phase 5. Real Nova Canvas remediation is a later phase and is **not** implemented — do not re-add a stub that reports success. |

**Phase 4** connected the pipeline end to end: a validated job state machine,
real extraction/analysis stages, validated model output, and persisted results.
See `docs/PIPELINE_ARCHITECTURE.md`.

**Still true:** the pipeline has never been run against live AWS or Bedrock.
Every external service is mocked in tests, so detection accuracy is
**unmeasured** and no claim is made about it. See §3.

### 1.4 The README was rewritten in Phase 5 **[VERIFIED]**

`README.md` previously claimed LLaVA/Ollama analysis (no such code exists), Nova Act automation (not implemented), MongoDB GridFS as the primary store (orphaned), and Next.js 14 (it is 16.1.6), and published test credentials for users no seed script creates. Phase 5 replaced it with an account of the system that matches the code: the real storage layout, the real route surface, the real setup steps, and an explicit limitations section. The fabricated credentials table is gone.

**`docs/planning.md` is still NOT a description of the system** — it is an unimplemented design target (no Critic, no Step Functions, no HITL). Use `docs/PROJECT_AUDIT.md` for evidence, and note that the audit predates Phase 5, so files it cites as dead may no longer exist.

---

## 2. Verified architecture

### 2.1 Layout

```
Amazon Nova/                         <- project root (flattened in Phase 0; holds .gitignore)
├── backend/          FastAPI service, 23 Python modules + tests/
├── frontend/         Next.js 16 App Router dashboard (the live one)
├── infra/            CloudFormation storage template + IAM policy template (not deployed)
├── docs/             planning.md (aspirational) + the audit deliverables
├── analyzer_nova.py  Standalone Nova Pro single-frame CLI (the one working AI path)
├── prompts.py        USED BY analyzer_nova.py — diverges from backend/prompts.py
└── test_frame.jpg    Default input for analyzer_nova.py
```

`frontend_backup/` (72 files) was deleted in Phase 5 after confirming zero references.

⚠️ **Root `prompts.py` and root `test_frame.jpg` are live, not duplicates.** `analyzer_nova.py` sits at the repository root, so `from prompts import SYSTEM_PROMPT` resolves to the **root** copy, and its `__main__` block reads the **root** `test_frame.jpg`. `docs/IMPLEMENTATION_ROADMAP.md` Phase 5 task 1 calls both "duplicates that diverge from the backend copies" and lists them for deletion; **that instruction is wrong and deleting them would break `analyzer_nova.py`.** They were verified and retained. The prompt divergence itself (the root copy asks for prose, `backend/prompts.py` asks for the structured JSON the frontend consumes) is real and still needs a decision.

### 2.2 Stack **[VERIFIED from source]**

- **Frontend:** Next.js 16.1.6, React 19.2.3, TypeScript 5.9.3 (`strict: true`), Tailwind CSS v4, Framer Motion, lucide-react.
- **Backend:** FastAPI + Uvicorn, `python-jose` JWT (HS256), `motor` (async MongoDB), `boto3`.
- **AI:** Amazon Bedrock, model `amazon.nova-pro-v1:0`. *(Docs say "Nova 2 Pro"; code says `nova-pro-v1:0`. Unresolved.)*
- **Storage:** S3 for **both** source videos (`sources/`) and frame JPEGs (`frames/`), one bucket named by `S3_BUCKET_NAME`; DynamoDB (`NovaFlowFrames`, `NovaFlowProjects`) for metadata; MongoDB for accounts (`novaflow.users`) and ownership plus job state (`novaflow.videos`). Local `backend/uploads/` is **read-only legacy** — pre-Phase-4.1 records only; new uploads never land there.
- **Video:** `ffmpeg-python` + the FFmpeg binary, 1 fps extraction.
- **Deploy:** AWS Amplify (frontend only); `backend/Dockerfile` exists with no deployment definition.

### 2.3 Storage reality vs. documentation **[VERIFIED]**

The pipeline uses **S3 + DynamoDB** for media and frame metadata. **MongoDB holds user accounts (`novaflow.users`) plus video ownership and job state (`novaflow.videos`) — never video or frame bytes.**

**The second, dead pipeline is gone.** Phase 5 deleted `audit_manager.py`, `verifier.py` and `check_count.py` (the MongoDB/GridFS path): they were unreachable from the API and structurally unrunnable, because they read `meta["gridfs_id"]` and nothing in the repository ever wrote a GridFS frame or a `frame_metadata` document. `backend/tests/test_dependencies.py` now asserts those modules stay deleted and that **no backend module imports `gridfs`**, so the second store cannot reappear unnoticed. Do not reintroduce it without a decision.

### 2.4 Request flow **[VERIFIED — rewritten after Phases 3/4/4.1]**

The original audit's flow diagram described a pre-Phase-3 system (unauthenticated
uploads to local disk, mocked `/submissions`). That is no longer accurate. Current
surface — 18 paths:

```
Browser (:3000)
  ├─ POST /auth/signup|login ──► MongoDB novaflow.users (Argon2id, rate-limited)
  ├─ GET  /auth/me
  ├─ POST /upload-reel ───────► S3 sources/{id}/source.{ext} + ownership record
  ├─ POST /analyze/{id}[/retry] ► job queued, run by a leased worker
  ├─ GET  /jobs/{id} ─────────► job state, stage, counts
  ├─ GET  /violations/{id} ───► DynamoDB NovaFlowFrames (paginated)
  ├─ GET  /submissions ───────► the caller's own videos
  ├─ GET  /uploads/{file}?access_token=… ► S3 stream, scoped media token
  └─ /projects/* (6 routes)

  ⚠ POST /execute — returns placeholder text; the feature behind it is not
                    implemented (§9 question 7). Not a secret leak, but it must
                    not be presented as working.
```

**Every route that touches user data requires a JWT and an ownership check.**
Unauthorized access returns **404, not 403**, so ids cannot be enumerated.

### 2.5 Known architectural inconsistencies **[VERIFIED]**

- **Bounding-box schemas (partly resolved):** `backend/prompts.py` uses `bounding_box: [ymin,xmin,ymax,xmax]` and the frontend consumes that array form (`HUDView.tsx`), so it is the contract. The `box_2d` variant died with `audit_manager.py`; `detections.parse_frame_analysis` still tolerates it on input. ⚠️ `.agent/skills/video-audit/SKILL.md` still documents `{x,y,width,height}` + `timestamp_ms` and **is wrong** — correcting it is Phase 10.3.
- ~~**Bucket mismatch**~~ **RESOLVED in Phase 5.** `infra/init_s3.py` and `infra/iam_policy.yaml` — which created and granted the unused `brand-guardian-*` buckets — were deleted. `infra/cloudformation/storage.yaml` is now the only provisioning path and provisions the bucket the code actually uses, and `infra/iam_policy_least_privilege.json` is the only policy. It covers DynamoDB.
- ~~**Env var collision**~~ **RESOLVED in Phase 5.** `MONGO_URI` died with `audit_manager.py`; `MONGODB_URL` is the only connection-string variable, and a test asserts no module reads the old name or hardcodes a connection string.
- ~~**`requirements.txt` omissions**~~ **RESOLVED in Phase 2**; every direct import is declared and pinned, and `test_dependencies.py` asserts it. Phase 5 removed `Pillow`, whose only importer was the deleted `audit_manager.py` — **frame remediation (Phase 8) must re-add it.**

---

## 3. Commands

### Verified working **[VERIFIED 2026-10-20, Phase 5]**

A `.venv` exists at the repository root with the backend dependencies installed,
and `frontend/node_modules` is installed. Use the venv interpreter.

```bash
git rev-parse --show-toplevel                      # ALWAYS run before committing
python -m py_compile backend/*.py analyzer_nova.py prompts.py

# Backend tests — 624 passed, 42 skipped
./.venv/Scripts/python.exe -m pytest backend/tests -q

# Frontend — all four pass; lint reports 0 errors / 18 warnings
cd frontend && npm test          # 74 tests (Vitest)
cd frontend && npm run typecheck # tsc --noEmit, exit 0
cd frontend && npm run lint      # "eslint app" — now has a target
cd frontend && npm run build     # routes: / , /_not-found , /login
```

⚠️ **Rebuild after deleting a route.** `npm run typecheck` reads generated
validators under `frontend/.next/types/`, which still reference a deleted
`app/<route>/page.tsx` until `npm run build` regenerates them. A stale failure
there is not a real type error.

### Requires prerequisites this host lacks **[UNVERIFIED]**

```bash
cd backend && uvicorn app:app --host 0.0.0.0 --port 8000   # needs MongoDB + AWS
pytest -m requires_mongodb                                 # needs MONGODB_TEST_URL
pytest -m requires_ffmpeg                                  # needs the FFmpeg binary

# CLI utilities — require the FFmpeg binary (NOT installed on this host)
cd backend && python extractor.py <video.mp4> [video_id]
cd backend && python analyzer.py <image.jpg | video_id>
cd backend && python video_stitcher.py <video_id> [out.mp4]
cd backend && python recover_jobs.py            # dry run; --apply to requeue
```

### Does not exist **[VERIFIED 2026-10-20]**

- **No CI.** No `.github/` directory. Phase 10.2.
- No Playwright/end-to-end test covering login → upload → analyze → review.
- No backend linter or type checker is configured — no `pyproject.toml`,
  `setup.cfg`, `.flake8`, ruff or mypy config. `requirements-dev.txt` holds only
  pytest, pytest-asyncio and httpx. **Do not claim a Python lint or type pass:
  there is no tool to run.**
- `cfn-lint` is not installed, so `infra/cloudformation/storage.yaml` has only
  ever been validated structurally offline (YAML parse, resource types, and
  Ref/GetAtt/Condition resolution). `aws cloudformation validate-template` is an
  AWS API call and has not been run.

When a phase adds tests, run them and **quote the real output**. Never write "tests pass" without it.

### Environment on this machine **[VERIFIED 2026-10-20]**

Python 3.14.6 in `.venv` (Dockerfile targets 3.11 — mismatch) · Node v24.18.1 · npm 11.16.0 · Docker 29.8.0 · git 2.51.0 (`core.autocrlf=false` set locally) · gh 2.97.0 as `YushBytes` · **FFmpeg NOT INSTALLED** · **no MongoDB reachable** · `frontend/node_modules` installed.

---

## 4. Environment variables

**Names only — never write values into any file, commit, or report.** No `.env` exists on disk. `.env.example` at the repository root is the placeholder template; copy it to `.env` (git-ignored) and fill it in.

Security configuration (Phase 3, all via `backend/config.py`):

| Name | Required? | Default |
| :-- | :-- | :-- |
| `JWT_SECRET_KEY` | **Yes in production** (≥ 32 chars; blocklisted placeholders rejected) | ephemeral random key in development |
| `NOVAFLOW_ENV` | No | `development` |
| `JWT_ALGORITHM` | No | `HS256` (HS256/384/512 only) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | `1440` |
| `CORS_ALLOWED_ORIGINS` | No (set it in production) | `http://localhost:3000`; `*` rejected |
| `EXPOSE_API_DOCS` | No | `false` in production |
| `MAX_UPLOAD_BYTES` | No | `104857600` (100 MiB) |
| `ALLOWED_VIDEO_EXTENSIONS` | No | `mp4,mov,avi,webm,mkv` |
| `S3_BUCKET_NAME` | **Yes in production** (Phase 5; validated against the S3 naming rules) | `novaflow-media` in development — a placeholder nobody owns, and **refused in production** |

Pre-existing application configuration:

| Name | Read at | Default in code |
| :-- | :-- | :-- |
| `AWS_REGION` | `aws_resources.aws_region()` only | `us-east-1` (falls back to `AWS_DEFAULT_REGION`) |
| `DYNAMODB_TABLE_NAME` | `aws_resources.frames_table_name()` only | `NovaFlowFrames` |
| `DYNAMODB_PROJECTS_TABLE` | `aws_resources.projects_table_name()` only | `NovaFlowProjects` |
| `MONGODB_URL` | `db.py` | `mongodb://localhost:27017` |
| `MONGODB_DB_NAME` | `db.py` | `novaflow` |
| `JOB_LEASE_SECONDS` | `video_repo` | `300` |
| `RECOVER_JOBS_ON_STARTUP` | `app.py` | `true` |
| `API_URL` | `app.py:82` | `http://localhost:8000` |
| `BEDROCK_GUARDRAIL_ID` | `safety.py:13` | **none** |
| `BEDROCK_GUARDRAIL_VERSION` | `safety.py:14` | `1` |
| `NEXT_PUBLIC_API_URL` | 4 frontend files | `http://localhost:8000` |

AWS credentials come from the boto3 default chain, not from these variables. The JWT signing key is read from `JWT_SECRET_KEY`; it is no longer in the source.

**Storage contract (Phase 5).** Every AWS resource name resolves in exactly one place, `backend/aws_resources.py`, and the bucket resolves further into `config.load_s3_bucket()`. A second `os.getenv("S3_BUCKET_NAME", ...)` anywhere, or a second hardcoded `novaflow-media`, is a regression — `test_no_module_reimplements_the_bucket_fallback` fails on it. The table defaults are legitimate because they are the names `infra/cloudformation/storage.yaml` actually creates; the bucket default is not, because bucket names are globally unique, which is why production requires it explicitly. **There is no local-disk fallback for new uploads in any environment.**

`.gitignore` correctly covers `.env*`. **Never commit a `.env`.** When reporting, name variables and redact values.

---

## 5. Security posture

**Phase 3 and Phase 3.5 (2026-10-19) hardened this area. Full detail: `docs/SECURITY_AUDIT.md`; operator procedures: `docs/SECURITY_OPERATIONS.md`.**

**No live secrets exist in the repository** [VERIFIED] — pattern scans over the working tree and each phase diff found no AWS key ids, credentialed connection strings, API keys, or tokens, and no `.env` is on disk. A full Git-history scan is still outstanding (release checklist 2A.60).

### Conventions you must follow

- **Never hardcode a secret.** All security configuration is read through `backend/config.py`. There is no `auth.SECRET_KEY` any more, and a test asserts it never comes back.
- **`JWT_SECRET_KEY` is required in production.** `NOVAFLOW_ENV=production` without a valid key raises `ConfigurationError` and the app refuses to start. In development an ephemeral random key is generated per process, so local work needs no setup but sessions do not survive a restart.
- **Known-insecure values are blocklisted.** `config.KNOWN_INSECURE_SECRETS` permanently rejects the secret that used to be hardcoded, plus common placeholders, in every environment. Do not remove entries from that list.
- **Passwords use Argon2id** via `argon2-cffi`. Call `auth.hash_password` / `auth.check_password`; never add another hashing path. Legacy SHA-256 records migrate transparently on successful login.
- **Length policy belongs on signup, not login.** `SignupRequest` enforces a minimum; `LoginRequest` deliberately does not, or pre-existing accounts with short passwords would be locked out.
- **Roles come from stored account data, never from a request.** `config.VALID_ROLES` is the whole model (`client`, `agent`). Public signup always assigns `config.DEFAULT_SIGNUP_ROLE`; a `role` field in the body is ignored. The agent role is granted only by an operator updating MongoDB directly — there is no API for it by design.
- **Protect new routes with the shared dependencies** in `auth.py`: `Depends(get_current_user)` for authentication, `Depends(require_roles("agent"))` for role limits. Do not write a per-router copy.
- **Check ownership for anything user-owned.** Use `app.require_video_owner` for videos and `projects._require_participant` for projects. A missing ownership record is a **denial**, not a pass. Return **404, not 403**, so endpoints cannot be used to enumerate ids.
- **Never interpolate a client-supplied filename into a path.** `app._safe_extension` validates against an allowlist and the stored name is always server-generated (`vid_<8 hex>.<ext>`); the original survives only as display metadata.
- **Never return exception text to a client.** The catch-all handler in `app.py` logs the traceback and returns a flat `{"detail": "Internal server error"}`. Catch `BotoCoreError`/`ClientError` around AWS calls and return a generic 502.
- **Never log a password, hash, token, signing key, or model analysis body.** Log counts, ids, and exception *types*. Tests assert this.
- **Never put a secret in a `NEXT_PUBLIC_*` variable** — those are compiled into the browser bundle and are public.
- **A job is `completed` only when analysis finished AND its results were persisted.** Never set that state from anywhere but `pipeline.run_job`'s final transition, and never infer it on the client. An empty detections list means nothing unless `job.state === "completed"` - that distinction is the whole point of the state machine.
- **Add new job states to `job_state.py`'s transition table**, not as free-text. `video_repo.transition` validates every move and is conditional on the current state, which is what prevents two workers advancing one job.
- **Report named stages, never percentages.** The frame total is unknown until extraction finishes, so any earlier percentage is invented.
- **Treat model output as untrusted.** Parse it through `detections.parse_frame_analysis`. A missing confidence or an invalid box must stay absent rather than being defaulted - a fabricated `0.0` is worse than nothing.
- **A pipeline stage must raise, not return `None`, on failure.** `extractor.ExtractionError` messages are shown to the video's owner, so they must carry no path, bucket, or exception text.
- **Media access uses a scoped token.** A browser `<video>` element cannot send an `Authorization` header, so `GET /uploads/{file}` takes a short-lived token (`MEDIA_TOKEN_TTL_MINUTES`, default 15) bound to one `video_id`. Session tokens and media tokens are not interchangeable.
- **Never log a full URL or request target.** Playback URLs carry a token in the query string. `backend/logging_setup.py` redacts sensitive query values on the request-emitting loggers; if you add a logger that prints a URL, register it there. Log `request.url.path`, never `request.url`.
- **Rate-limit new credential-style endpoints** by adding a rule in `rate_limit.build_credential_rules`. Use middleware, not a route decorator: a decorator runs after request validation, so a malformed body bypasses the counter entirely (measured, see `rate_limit.py`). Counters are per-process.
- **Never trust `X-Forwarded-For`** unless `TRUSTED_PROXY_COUNT` is set to the real proxy depth; read the chain from the right, never the client-controlled left.
- **Database initialization must stay non-destructive.** `backend/db_init.py` detects duplicates before creating a unique index, reports counts only (never usernames), and skips rather than failing. Never delete or merge accounts to make a migration succeed.
- **Usernames are matched exactly.** Do not add trimming or case-folding on read: existing `" alice"` / `"Alice"` records would become unreachable. New signups reject surrounding whitespace instead.

### Known remaining gaps

- **Rate limiting is per-process.** `/auth/login` and `/auth/signup` are throttled, but counters live in process memory, so the effective limit is multiplied by the worker count. Accurate for the single-process Dockerfile; **not** distributed protection. No per-account throttle either.
- **No token revocation.** Tokens live in `localStorage` and are valid until expiry; rotating `JWT_SECRET_KEY` is the only break-glass.
- **Media tokens travel in URLs.** Redacted from this process's logs, but still reach browser history, `Referer`, and any proxy/CDN log — which this application cannot redact.
- **Analysis runs in an in-process `BackgroundTasks` worker, so there is no crash recovery.** A restart orphans running jobs in `extracting`/`analyzing`; `pipeline.find_stuck_jobs()` detects them but nothing resumes them. A durable queue is the real fix and is out of scope.
- **Source videos live on the application host's local disk**, not S3. A replaced container loses them.
- **Frame sampling is 1 fps**, so a violation visible for under ~1 second can be missed.
- **There is no brand-rule configuration** - one static prompt in `prompts.py`. No per-client policies, and no legal/regulatory evaluation.
- **Pre-Phase-3 uploads have no ownership record** and are therefore inaccessible. The uploader was never recorded, so it cannot be recovered from the data. Options in `docs/SECURITY_OPERATIONS.md` §6.
- **AWS hardening is a template, not applied.** `infra/iam_policy_least_privilege.json` still has `${PLACEHOLDER}` values an operator must substitute, and `infra/cloudformation/storage.yaml` — which does express the bucket hardening (public-access block, SSE-S3, versioning, TLS-only policy, lifecycle) — **has never been deployed**. The bucket naming conflict was resolved in Phase 5 (§2.5), but nothing in AWS has been verified.
- **Real-MongoDB integration tests are unverified.** Written and safety-guarded in `tests/test_integration_mongodb.py`, but never executed — no database or Docker daemon was available. Run `pytest -m requires_mongodb`.
- Upload validation does **not** prove a file is a safe video; there is no content scanning.
- 22 frontend lint **warnings** remain (unused vars, `<img>` usage); zero errors. None security-relevant.

When working in this repo: never print a secret value, never commit one, and if you find one, report its **file and variable name** with the value redacted and recommend rotation — do not delete user files or change cloud permissions yourself.

---

## 6. Phase workflow — mandatory rules

Work proceeds phase by phase per `docs/IMPLEMENTATION_ROADMAP.md`. Every session must follow these:

1. **Work only on the current assigned phase.** Do not pull work forward, even when it looks trivial.
2. **Inspect the relevant existing code before modifying it** — the file and its callers.
3. **Preserve working behavior.** Avoid rewrites. Do not replace a working component because another technology is available.
4. **Implement real functionality.** Never present a mock, hardcoded value, or silent fallback as genuine AI output. Two patterns are the ones to never reintroduce: a function that returns its input unchanged while the caller records success (was `audit_manager.remediate_frame`, deleted in Phase 5), and a stub that returns `"Mock Response"` as if it were a plan (still live at `orchestrator.py:17`, reachable through `POST /execute`, pending §9 question 7). If something cannot be implemented, leave it visibly unimplemented and say so.
5. **Write or update tests for every meaningful change.**
6. **Run relevant tests, lint, type checks, and builds** when applicable.
7. **Review the final diff** for regressions, secrets, generated files, and accidental changes.
8. **Report exact verification results and remaining blockers**, quoting real command output.
9. **Never mark an acceptance criterion complete without evidence.** "The code exists" is not evidence.
10. **Do not begin the next phase** until the current one is reviewed and explicitly accepted.
11. **Commit and push at each phase checkpoint**, subject to §7.
12. **Never force-push, amend published history, commit credentials, or silently overwrite unrelated changes.**

### Current phase status

Phases 0 through 5 are **complete, committed, and pushed**.

| Phase | What landed |
| :-- | :-- |
| 0 | Repository correctly rooted, flattened, private, tracking `origin/main`. |
| 1 | Evidence-based audit and roadmap. |
| 2 | The four critical defects fixed, each with a regression test. |
| 3 / 3.5 | Authentication, authorization, ownership, upload handling; rate limiting, safe DB init, media-token log redaction, least-privilege IAM template. |
| 4 | End-to-end pipeline and job state machine. |
| 4.1 | Durable S3 source storage, worker leases, crash recovery, CloudFormation storage template. |
| 5 | Dead code removed, storage configuration reconciled to one contract, README rewritten. |

**Phase 5 detail.** Deleted: the orphaned MongoDB/GridFS pipeline (`audit_manager.py`, `verifier.py`, `check_count.py`), `debug_probe.py`, `aws_config.py`, `frontend_backup/` (72 files), the stray `frontend/Video Compliance Dashboard/`, the orphan `/agents` and `/logs` duplicate routes, `infra/init_s3.py`, and `infra/iam_policy.yaml` — 82 files. `S3_BUCKET_NAME` is now required in production and validated; `MONGO_URI` and `Pillow` are gone with their only consumer. Regression tests assert the deleted modules stay deleted, that nothing imports `gridfs`, and that nothing reimplements the bucket fallback.

**Phase 5 did NOT make the application production-ready.** It made the architecture coherent. Everything in §5 "Known remaining gaps" still stands, and nothing has still ever run against real AWS or Bedrock.

**Phase 6 has not been started.** Remaining: 6 (backend reliability / durable worker), 7 (frontend completion), 8 (remediation — needs §9 q4), 9 (reconstruction — needs §9 q3), 10 (tests, CI, docs, release). Do not start one until it is explicitly assigned.

---

## 7. Git safety rules

- **Verify `git rev-parse --show-toplevel`** is the project root before every commit. See §1.1.
- **Stage explicitly by path.** Never `git add -A` / `git add .` — the stray Desktop repository still exists (§1.1).
- Read `git status` and `git diff --cached` before committing.
- `git fetch` and inspect remote state before pushing. On conflict, **stop and report** — never force.
- **Never force-push.** The remote's published history (rooted at `e059a9c`) must be preserved; local `main` descends from it, so a normal fast-forward push works.
- If a push fails for auth/network/permission reasons, **preserve the local commit** and report the exact error. Never claim work was pushed when it was not.
- Do not create another GitHub repository. Do not delete branches or rewrite history.
- `core.autocrlf=false` is set locally and `.gitattributes` declares binary assets, so Git stores bytes as they appear on disk. Repo-wide text line-ending normalization (`* text=auto eol=lf`) was deliberately deferred in Phase 0 — see the rationale in `.gitattributes`.

---

## 8. Conventions

Observed in the existing code, for consistency. Several are **bad practices the roadmap replaces** — flagged as such; do not propagate them into new code.

**Backend (Python)**
- 4-space indent; module-level constants in `SCREAMING_SNAKE_CASE`; `load_dotenv()` at module top.
- boto3 clients are built at module scope — convenient for CLI use, but it makes testing harder and means no shared timeout config. New code should accept an injected client.
- Each pipeline module has an `if __name__ == "__main__":` CLI block with `sys.argv` parsing. Keep this; it is genuinely useful.
- `logging` is used in `app.py`, `auth.py`, `config.py`, `projects.py`, `video_repo.py`, `analyzer.py`, `extractor.py`, `pipeline.py` and `source_store.py`. ⚠️ `video_stitcher.py` and `safety.py` still use `print()`. **Use `logging` in new code.**
- ⚠️ **Bare `except Exception` that logs and continues** is still common in the older modules, and was the direct cause of the §1.3 defects being silent. **Catch specific exceptions and record a terminal failure state.**
- Raw exception strings are **no longer** returned to clients: `app.py` has a catch-all handler returning a generic 500, and AWS errors become a generic 502. Keep it that way.
- Pydantic `BaseModel` for request/response schemas — follow this, and extend it to **validating AI model output**, which is currently unvalidated everywhere.

**Frontend (TypeScript/React)**
- Next.js App Router; `"use client"` on interactive components; default exports for components.
- Tailwind utility classes with a `cn()` helper — canonically `app/lib/utils.ts:4`, but ⚠️ `HUDView.tsx:20` **duplicates it**. Import the shared one.
- CSS custom properties for theming in `app/globals.css` (`--background`, `--primary`, `--accent`, `--critical`) — well structured; keep it.
- `process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'` inline at each call site. ⚠️ Repeated in 5 places; centralise in new code.
- ⚠️ **`any` types are used liberally** in `HUDView.tsx` despite `strict: true`. Type new code properly.
- ⚠️ **`alert()` for errors** (`HUDView.tsx:208`). Browser modals block the page and discard the real error — use inline error UI.

---

## 9. Unresolved questions

These block specific phases. Do not decide them unilaterally.

1. ~~Delete the dead MongoDB/GridFS pipeline?~~ **RESOLVED in Phase 5 — deleted**, per the recommendation recorded here. A regression test keeps it deleted.
2. Keep MongoDB for users, or consolidate onto DynamoDB? *(Still open. MongoDB now also holds ownership and job state, so this is a larger decision than it was.)*
3. Is video **reconstruction** in release scope, or is detection + reporting enough for v1?
4. Is **remediation** (Nova Canvas) in release scope? It is currently a disguised mock and must be built properly or removed.
5. Which bounding-box schema is canonical? *(De facto: the array form in `prompts.py`.)*
6. Correct model ID — `amazon.nova-pro-v1:0`, or a "Nova 2 Pro" id? Docs and code disagree.
7. Is Nova Act / browser automation in scope at all? If not, `automation.py`, `orchestrator.py`, `/execute`, and the "Neural Command" UI panel can all go. *(Still open, so Phase 5 retained all of them. `backend/safety.py` is retained for the same reason: it is never imported, but its `intercept_action` is the human-in-the-loop gate for exactly this feature, so deleting it would pre-empt this decision. `backend/s3_trigger.py`, an unreferenced Lambda stub with no deployment definition, was retained as uncertain rather than deleted.)*
8. ~~Are the `brand-guardian-*` buckets in `infra/` intentional, or leftovers?~~ **RESOLVED in Phase 5 — leftovers.** No application code has ever read them. The two files that created and granted them (`infra/init_s3.py`, `infra/iam_policy.yaml`) were deleted. ⚠️ **No AWS bucket was touched.** If such buckets were ever created in a real account, deleting them there is a separate operator action.
9. Backend deployment target — ECS, Lambda, App Runner? Only the frontend has a deployment config.

---

## 10. Things to never do in this repository

- Never `git add -A` / `git add .` / `git commit -a` — stage by explicit path; the stray Desktop repository still exists (§1.1).
- Never force-push, rewrite history, or create another GitHub repository.
- Never commit a `.env`, a credential, `node_modules/`, `backend/uploads/`, `temp_frames_*/`, or build artifacts.
- Never present a mock, hardcoded value, or silent fallback as real AI output.
- Never reintroduce the MongoDB/GridFS pipeline, a second `os.getenv("S3_BUCKET_NAME")`, or a hardcoded `novaflow-media` — three tests fail on these by design (§2.3, §4).
- Never claim a test passed, a build succeeded, or a push completed without the actual output.
- Never mark an audit or roadmap item verified because the code exists.
- Never introduce a new database, framework, AI provider, or cloud service without an explicit decision.
- Never run the Bedrock or video pipeline against real AWS without confirming cost is acceptable — frame-by-frame Nova Pro calls bill per frame.
- Never print a secret value into a report, a commit message, or the terminal.
