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
| Remediation returned its input unchanged | `backend/audit_manager.py` *(file deleted in Phase 5)* | The disguised mock was removed in Phase 2; the whole module went in Phase 5. **Phase 8 resolved the scope question** (§9 q4) and built the reviewed proposal workflow; automated image generation is still **not** implemented, and `image_provider.NullImageProvider.generate` raises rather than returning anything. Do not re-add a stub that reports success. |

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
├── backend/          FastAPI API + durable worker, 25 Python modules + tests/
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
- **Storage:** S3 for source videos (`sources/`), frame JPEGs (`frames/`), agent reels (`raw/`), replacement images (`remediation-assets/`, Phase 9) and reconstructed outputs (`remediated/`, Phase 9) — one bucket named by `S3_BUCKET_NAME`; DynamoDB (`NovaFlowFrames`, `NovaFlowProjects`) for metadata; MongoDB for accounts (`novaflow.users`), ownership plus job state (`novaflow.videos`), and remediation records (`novaflow.remediations`, Phase 8). ⚠️ **Nothing writes to `sources/` except an upload** — a reconstruction's output is a separate object under its own video id. Local `backend/uploads/` is **read-only legacy** — pre-Phase-4.1 records only; new uploads never land there.
- **Video:** `ffmpeg-python` + the FFmpeg binary, 1 fps extraction.
- **Deploy:** AWS Amplify (frontend only); `backend/Dockerfile` exists with no deployment definition. **Two backend processes** now: the API (`uvicorn app:app`) and the worker (`python backend/worker.py`). Both run from the same image; neither has a deployment definition. §9 question 9 is still open.

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
  ├─ POST /analyze/{id}[/retry] ► job queued DURABLY, then the API is done
  │                               (a separate worker process claims and runs it)
  ├─ GET  /health/live|ready|worker ► probes; live touches no dependency
  ├─ GET  /jobs/{id} ─────────► job state, stage, counts
  ├─ GET  /violations/{id} ───► DynamoDB NovaFlowFrames (paginated)
  ├─ GET  /submissions ───────► the caller's own videos
  ├─ GET  /uploads/{file}?access_token=… ► S3 stream, scoped media token
  ├─ /remediations/* (10 routes) ► propose, review, decide, build, check
  │        POST /{id}/asset   attach an owner-supplied replacement image
  │        POST /{id}/execute build a NEW output video from it (Phase 9)
  │        GET  /{id}/status  state + the OUTPUT's analysis + any verdict
  │        GET  /{id}/output  a short-lived scoped playback URL
  │        ⚠ /execute from `approved` with no asset is still 503: no image
  │          provider exists, and the record is left `approved`, not failed.
  └─ /projects/* (6 routes)

  ⚠ POST /execute — returns placeholder text; the feature behind it is not
                    implemented (§9 question 7). Not a secret leak, but it must
                    not be presented as working.
```

**Every route that touches user data requires a JWT and an ownership check.**
Unauthorized access returns **404, not 403**, so ids cannot be enumerated.

### 2.4.1 Frontend surface **[VERIFIED 2026-10-21, Phase 7]**

Three routes only — `/`, `/login`, and the generated `/_not-found`. The shell
at `/` switches views in client state, so no view below it is deep-linkable.

| View | Endpoints it calls | Role |
| :-- | :-- | :-- |
| `/login` | `POST /auth/login`, `POST /auth/signup` | public |
| Dashboard | `/upload-reel`, `/analyze/{id}`, `/analyze/{id}/retry`, `/jobs/{id}`, `/violations/{id}` | agent |
| Your Videos | `/submissions` | agent, client |
| Audit detail | `/jobs/{id}`, `/violations/{id}`, `/analyze/{id}`, `/analyze/{id}/retry` | agent, client |
| Compliance Log | `/submissions` | agent, client |
| Infrastructure | `/health/worker` | agent |
| Account | `/auth/me` (via the auth context) | agent, client |

**Every fabricated panel was removed in Phase 7**, each with a regression
test asserting the specific invented strings cannot return. The worst was
`ProjectDetailView`'s unconditional "AUDIT PASSED / verified by NovaFlow
Neural Agent #xyz / Compliance & quality standards met" card, rendered for
every video regardless of job state — a direct violation of the rule in §5
that an incomplete or failed analysis must never be labelled compliant.
`frontend/README.md` lists all of them.

Three panels now state that a capability does not exist rather than
simulating it: custom audit instructions / brand rules (the route behind it,
`POST /execute`, returns placeholder text — §9 q7), account management (no
endpoint exists; signing out does not revoke the token), and per-worker
telemetry (`/health/worker` infers from the queue; it does not contact the
worker). **Do not wire a control to `POST /execute`** while §9 q7 is open.

Not deep-linkable views, a detail view that loads once instead of polling, and
client-side-only search over the already-fetched list are known limitations —
see `frontend/README.md` "Known limitations".

### 2.5 Known architectural inconsistencies **[VERIFIED]**

- **Bounding-box schemas (partly resolved):** `backend/prompts.py` uses `bounding_box: [ymin,xmin,ymax,xmax]` and the frontend consumes that array form (`HUDView.tsx`), so it is the contract. The `box_2d` variant died with `audit_manager.py`; `detections.parse_frame_analysis` still tolerates it on input. ⚠️ `.agent/skills/video-audit/SKILL.md` still documents `{x,y,width,height}` + `timestamp_ms` and **is wrong** — correcting it is Phase 10.3.
- ~~**Bucket mismatch**~~ **RESOLVED in Phase 5.** `infra/init_s3.py` and `infra/iam_policy.yaml` — which created and granted the unused `brand-guardian-*` buckets — were deleted. `infra/cloudformation/storage.yaml` is now the only provisioning path and provisions the bucket the code actually uses, and `infra/iam_policy_least_privilege.json` is the only policy. It covers DynamoDB.
- ~~**Env var collision**~~ **RESOLVED in Phase 5.** `MONGO_URI` died with `audit_manager.py`; `MONGODB_URL` is the only connection-string variable, and a test asserts no module reads the old name or hardcodes a connection string.
- ~~**`requirements.txt` omissions**~~ **RESOLVED in Phase 2**; every direct import is declared and pinned, and `test_dependencies.py` asserts it. Phase 5 removed `Pillow`, whose only importer was the deleted `audit_manager.py`. ⚠️ Phase 8 did **not** re-add it: nothing builds an inpainting mask, because no provider consumes one. **A real image provider must re-add and pin it** — see `docs/REMEDIATION.md` §6 step 3.

---

## 3. Commands

### Verified working **[VERIFIED 2026-10-20, Phase 5]**

A `.venv` exists at the repository root with the backend dependencies installed,
and `frontend/node_modules` is installed. Use the venv interpreter.

```bash
git rev-parse --show-toplevel                      # ALWAYS run before committing
python -m py_compile backend/*.py analyzer_nova.py prompts.py

# Backend tests — 1169 passed, 26 skipped in 54.51s (Phase 9.1)
#   The 26 remaining skips ALL need MONGODB_TEST_URL. A skip is NOT a pass.
#   FFmpeg 9.0.1 is now installed on this host, so the 29 formerly-skipped
#   FFmpeg tests RUN: 46 are marked requires_ffmpeg today (the file grew in
#   Phase 9.1). Before Phase 9.1 this read 1111 passed / 55 skipped, where
#   the 55 were 26 MongoDB + 16 extraction + 13 reconstruction.
./.venv/Scripts/python.exe -m pytest backend/tests -q
./.venv/Scripts/python.exe -m pytest backend/tests -m requires_ffmpeg -q   # 46 tests

# Frontend — all four pass; lint reports 0 errors / 0 warnings
cd frontend && npm test          # 387 tests (Vitest), 18 files
cd frontend && npm run typecheck # tsc --noEmit, exit 0
cd frontend && npm run lint      # "eslint app"
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

# CLI utilities — require the FFmpeg binary, which IS now installed here
# (9.0.1), but these entry points still need AWS credentials and a reachable
# MongoDB, so they remain unverified end to end.
cd backend && python extractor.py <video.mp4> [video_id]
cd backend && python analyzer.py <image.jpg | video_id>
cd backend && python video_stitcher.py <video_id> [out.mp4]
cd backend && python recover_jobs.py            # dry run; --apply to requeue
cd backend && python worker.py                  # the durable worker
cd backend && python worker.py --status         # queue depth; needs MongoDB
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

### Environment on this machine **[VERIFIED 2026-10-23]**

Python 3.14.6 in `.venv` (Dockerfile targets 3.11 — mismatch) · Node v24.18.1 · npm 11.16.0 · Docker 29.8.0 · git 2.51.0 (`core.autocrlf=false` set locally) · gh 2.97.0 as `YushBytes` · **FFmpeg 9.0.1 INSTALLED** (`ffmpeg` and `ffprobe` both on PATH, gyan.dev essentials build) · **no MongoDB reachable** · `frontend/node_modules` installed.

⚠️ **That is this development host, not a deployment claim.** FFmpeg is a
**runtime prerequisite** of `extractor.py` and `reconstruction.py`: without the
binary, extraction and reconstruction fail at the subprocess boundary, which
is why `RECONSTRUCTION_ENABLED` exists and should be set false on a host that
lacks it. **No production or container environment has been verified to have
FFmpeg** — `backend/Dockerfile` has never been deployed (§9 question 9), and
nothing here says otherwise. Installing it locally only means the
`requires_ffmpeg` tests now run *here*.

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
| `REMEDIATION_ENABLED` | `config.load_remediation_enabled()` | `true` (`false` makes every `/remediations` route 404) |
| `REMEDIATION_IMAGE_PROVIDER` | `config.load_remediation_image_provider()` | `none` (`nova-canvas` is accepted but **unimplemented**; anything else fails at startup) |
| `REMEDIATION_MIN_CONFIDENCE` | `config.load_remediation_min_confidence()` | `0.8` — the first code to read the figure `docs/planning.md` has specified since Phase 1 |
| `MAX_REMEDIATIONS_PER_VIDEO` | `config.load_max_remediations_per_video()` | `200` |
| `RECONSTRUCTION_ENABLED` | `config.load_reconstruction_enabled()` | `true` — **needs the FFmpeg executable**; set false without it |
| `RECONSTRUCTION_TIMEOUT_SECONDS` | `config.load_reconstruction_timeout_seconds()` | `300` |
| `REMEDIATION_WINDOW_SECONDS` | `config.load_remediation_window_seconds()` | `1.0` |
| `MAX_REMEDIATION_ASSET_BYTES` | `config.load_max_asset_bytes()` | `8388608` (8 MiB) |
| `REMEDIATION_LEASE_SECONDS` | `remediation_repo.execution_lease_seconds()` | `600` |
| `VERIFICATION_POLICY` | `config.load_verification_policy()` | `target_only` (`clean_output` also valid; anything else fails at startup) |
| `MONGODB_DB_NAME` | `db.py` | `novaflow` |
| `JOB_LEASE_SECONDS` | `video_repo` | `300` |
| `RECOVER_JOBS_ON_STARTUP` | `app.py` | `true` |
| `JOB_EXECUTION_MODE` | `config.load_job_execution_mode()` | `worker` in production, `inline` in development |
| `WORKER_POLL_INTERVAL_SECONDS` | `worker` | `5` |
| `WORKER_RECOVERY_INTERVAL_SECONDS` | `worker` | `60` (0 disables) |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `worker` | `30` |
| `RETRY_BACKOFF_SECONDS` / `_CAP_SECONDS` | `pipeline.retry_delay_seconds` | `30` / `600` |
| `READINESS_TIMEOUT_SECONDS` | `app.py` health probes | `3` |
| `AWS_CONNECT_TIMEOUT_SECONDS` / `AWS_READ_TIMEOUT_SECONDS` / `AWS_MAX_ATTEMPTS` | `aws_clients.client_config()` | `10` / `60` / `3` |

⚠️ **`AWS_MAX_ATTEMPTS` and `BEDROCK_MAX_ATTEMPTS` multiply.** Their product is the worst-case model invocations for one frame, and every attempt is billed. Raise one, not both.
| `API_URL` | `app.py:82` | `http://localhost:8000` |
| `BEDROCK_GUARDRAIL_ID` | `safety.py:13` | **none** |
| `BEDROCK_GUARDRAIL_VERSION` | `safety.py:14` | `1` |
| `NEXT_PUBLIC_API_URL` | `frontend/app/lib/api.ts` only (Phase 7) | `http://localhost:8000` |
| `NEXT_PUBLIC_MAX_UPLOAD_BYTES` | `frontend/app/lib/api.ts` | `104857600` (100 MiB) |
| `NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS` | `frontend/app/lib/api.ts` | `mp4,mov,avi,webm,mkv` |

⚠️ **The two `NEXT_PUBLIC_*` upload limits only mirror the server's.** The
browser cannot read `MAX_UPLOAD_BYTES` / `ALLOWED_VIDEO_EXTENSIONS`, so
client-side validation is a pre-flight courtesy; the backend validates
independently and its rejection message is surfaced verbatim. **Narrowing the
server limits without setting these makes the UI advertise a limit the server
refuses** — exactly the kind of untrue interface Phase 7 removed.

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
- **A reconstructed output is a first-class video record** (Phase 9), with its own `video_id`, owner and `s3_key` under `remediated/`. That is what lets the existing pipeline analyse it and the existing playback route serve it with no special case. ⚠️ **Never write a reconstruction over its source**: the key is built from a freshly minted output id, `reconstruction.reconstruct` refuses if asked to write over its own input, and a test asserts the source object's bytes are unchanged after a run.
- **Only `verification.py` may set `verified` or `verification_failed`** (Phase 9), and only from the OUTPUT video's **persisted** analysis with at least one audited frame. A completed job that audited nothing, a failed job, a job still running, and a storage failure reading the results are each explicitly NOT evidence — they produce `pending`/`inconclusive`, never a pass. **Never use the original video's analysis as evidence about the output.**
- **Keep the three verification facts apart** (Phase 9): `target_resolved`, `policy_passed` and `other_findings` answer different questions, and `null` for the first two means "no verdict", not `false`. "The logo is gone" is not "the video is fine".
- **Reconstruction refuses rather than guesses.** No bounding box, a malformed or too-small region, a timestamp past the clip's duration, or a zero-length window each fail terminally with a safe message. Never invent a region, a default window, or a whole-frame fallback.
- **Call it a scaled overlay, never seamless.** It conceals the approved region with hard edges; it does not inpaint, match grain, or track motion. A frontend test asserts "seamless" and "inpaint" never appear.
- **Never collapse the remediation states into one flag** (Phase 8). `proposed` is not approved, `approved` is not generated, `asset_ready` is not a changed video, `applied` is not compliant. **Only `verified` may be called compliant**, and it is reachable only from `verifying` — i.e. only after a completed re-analysis whose persisted results support the claim. `remediation_repo.view` answers this once in `is_compliance_claim`; never re-derive it from the state string. A `verification_failed` record is NOT clean.
- **Do not add a Nova Canvas adapter without verifying the payload schema** against authoritative Bedrock documentation (§9 q4, `docs/REMEDIATION.md` §6). `image_provider.NullImageProvider.generate` raises and never returns an image; a provider that returned its input, a placeholder, or a fabricated success would reintroduce the exact defect Phase 2 removed. A test is named for it.
- **Treat provider output as untrusted.** `image_provider.validate_image_bytes` derives the content type from the bytes and ignores the provider's declaration; a 2xx response is not evidence of an image.
- **Re-read and re-fingerprint a finding before acting on a remediation.** A proposal addresses a finding positionally and a re-analysis overwrites the frame row, so a stale proposal must become `superseded` rather than be approved or executed.
- **Add new job states to `job_state.py`'s transition table**, not as free-text. `video_repo.transition` validates every move and is conditional on the current state, which is what prevents two workers advancing one job.
- **Report named stages, never percentages.** The frame total is unknown until extraction finishes, so any earlier percentage is invented.
- **Frontend: derive every status from `app/lib/jobs.ts`** (Phase 7). `verdictFor` is the single place that decides whether an audit may be described as clean, and it refuses to for any state but `completed`. Four views share it; a view that re-derives a verdict locally is how the unconditional "Audit Passed" card happened. `jobStateOf` reads an unrecognised status as `uploaded` — the state that claims the least — rather than rendering it raw.
- **Frontend: a missing model field stays missing.** `formatConfidence` returns null when the model reported no confidence, and the UI prints "Not reported". A rendered `0%` asserts certainty the model never expressed. Same for `risk_level`: an unrecognised value is shown verbatim, never mapped to a severity it may not mean.
- **Frontend: never log a request URL.** `app/lib/api.ts` carries the same rule as `backend/logging_setup.py` — a playback URL has a media token in its query string, so a logged target leaks a credential to the browser console. Errors carry a status and a message.
- **Frontend: never auto-retry an upload or an analysis start.** A repeated upload stores a second copy; a repeated analysis bills the model again per frame. Retry is always an explicit user action. Polling is scheduled only after the previous request settles, so requests cannot overlap.
- **Treat model output as untrusted.** Parse it through `detections.parse_frame_analysis`. A missing confidence or an invalid box must stay absent rather than being defaulted - a fabricated `0.0` is worse than nothing.
- **A pipeline stage must raise, not return `None`, on failure.** `extractor.ExtractionError` messages are shown to the video's owner, so they must carry no path, bucket, or exception text.
- **Media access uses a scoped token.** A browser `<video>` element cannot send an `Authorization` header, so `GET /uploads/{file}` takes a short-lived token (`MEDIA_TOKEN_TTL_MINUTES`, default 15) bound to one `video_id`. Session tokens and media tokens are not interchangeable.
- **Never log a full URL or request target.** Playback URLs carry a token in the query string. `backend/logging_setup.py` redacts sensitive query values on the request-emitting loggers; if you add a logger that prints a URL, register it there. Log `request.url.path`, never `request.url`.
- **Build every boto3 client through `aws_clients`** (Phase 6). A bare `boto3.client(...)` has no timeout, and an unbounded call keeps a dead-in-the-water worker heartbeating so its job is never recovered — the timeout is what makes the lease mean anything. `test_no_module_builds_an_unconfigured_boto3_client` fails on a direct call.
- **Classify a failure before retrying it.** Only a failure that could succeed later is retryable: a timeout, a throttle, an unreadable frame table. A missing object, an over-size object, or an unreadable video is deterministic and must fail immediately — retrying bills three times for the same answer. `source_store` carries an explicit `retryable` flag set **at each raise site**, never inferred from the message.
- **A retry must set a backoff.** `video_repo.schedule_retry` sets `not_before`; without it the worker reclaims the job on its next poll and spends the whole attempt budget within seconds of a blip. The recovery sweep skips a job whose `not_before` is in the future, so one failure cannot increment `attempt` twice.
- **Never make `/health/live` depend on an external service.** If it did, a brief MongoDB outage would fail every replica's liveness probe and get them all restarted — turning degradation into guaranteed downtime. Readiness is where dependencies belong, each under a finite timeout.
- **Rate-limit new credential-style endpoints** by adding a rule in `rate_limit.build_credential_rules`. Use middleware, not a route decorator: a decorator runs after request validation, so a malformed body bypasses the counter entirely (measured, see `rate_limit.py`). Counters are per-process.
- **Never trust `X-Forwarded-For`** unless `TRUSTED_PROXY_COUNT` is set to the real proxy depth; read the chain from the right, never the client-controlled left.
- **Database initialization must stay non-destructive.** `backend/db_init.py` detects duplicates before creating a unique index, reports counts only (never usernames), and skips rather than failing. Never delete or merge accounts to make a migration succeed.
- **Usernames are matched exactly.** Do not add trimming or case-folding on read: existing `" alice"` / `"Alice"` records would become unreachable. New signups reject surrounding whitespace instead.

### Known remaining gaps

- **Rate limiting is per-process.** `/auth/login` and `/auth/signup` are throttled, but counters live in process memory, so the effective limit is multiplied by the worker count. Accurate for the single-process Dockerfile; **not** distributed protection. No per-account throttle either.
- **No token revocation.** Tokens live in `localStorage` and are valid until expiry; rotating `JWT_SECRET_KEY` is the only break-glass.
- **Media tokens travel in URLs.** Redacted from this process's logs, but still reach browser history, `Referer`, and any proxy/CDN log — which this application cannot redact.
- ~~**Analysis runs in an in-process `BackgroundTasks` worker**~~ **ADDRESSED in Phase 6.** Execution moved to a separate polling worker (`backend/worker.py`) over the MongoDB `videos` collection, which was already a durable queue (conditional claim, lease, fencing token, bounded attempts). Acceptance is a database write made **before** the API responds, so an accepted job survives an API restart, and recovery sweeps run inside every worker on an interval rather than only at API startup. Remaining honest limits: delivery is **polled, not pushed** (default 5s); execution is **not at-most-once**, so an interrupted job re-runs from the start and re-bills Bedrock for frames already analysed; with every worker down nothing sweeps until one starts; and there is **no backpressure** — `/health/worker` exposes queue depth but nothing throttles on it. **No worker has run against real infrastructure and no process has been SIGKILLed** — crash recovery is verified as logic, not observed behaviour.
- ~~**Source videos live on the application host's local disk**~~ **FIXED in Phase 4.1** — they are in S3 under `sources/`, which is what lets any worker on any host run any job.
- **Frame sampling is 1 fps**, so a violation visible for under ~1 second can be missed.
- **There is no brand-rule configuration** - one static prompt in `prompts.py`. No per-client policies, and no legal/regulatory evaluation.
- **Pre-Phase-3 uploads have no ownership record** and are therefore inaccessible. The uploader was never recorded, so it cannot be recovered from the data. Options in `docs/SECURITY_OPERATIONS.md` §6.
- **AWS hardening is a template, not applied.** `infra/iam_policy_least_privilege.json` still has `${PLACEHOLDER}` values an operator must substitute, and `infra/cloudformation/storage.yaml` — which does express the bucket hardening (public-access block, SSE-S3, versioning, TLS-only policy, lifecycle) — **has never been deployed**. The bucket naming conflict was resolved in Phase 5 (§2.5), but nothing in AWS has been verified.
- **Real-MongoDB integration tests are unverified.** Written and safety-guarded in `tests/test_integration_mongodb.py`, but never executed — no database or Docker daemon was available. Run `pytest -m requires_mongodb`.
- Upload validation does **not** prove a file is a safe video; there is no content scanning.
- Frontend lint is clean as of Phase 7: **0 errors, 0 warnings** (was 18 warnings — unused imports and `<img>` usage, all on panels that were deleted).
- **Hiding a frontend control is not authorization.** Role-based navigation is presentation only; the backend remains the sole authority, and Phase 7 added tests that say so explicitly so the nav filter is never mistaken for an access check.

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

Phases 0 through 9.1 are **complete, committed, and pushed**.

| Phase | What landed |
| :-- | :-- |
| 0 | Repository correctly rooted, flattened, private, tracking `origin/main`. |
| 1 | Evidence-based audit and roadmap. |
| 2 | The four critical defects fixed, each with a regression test. |
| 3 / 3.5 | Authentication, authorization, ownership, upload handling; rate limiting, safe DB init, media-token log redaction, least-privilege IAM template. |
| 4 | End-to-end pipeline and job state machine. |
| 4.1 | Durable S3 source storage, worker leases, crash recovery, CloudFormation storage template. |
| 5 | Dead code removed, storage configuration reconciled to one contract, README rewritten. |
| 6 | Durable worker process, AWS client timeouts, retry classification with backoff, health/readiness probes, Scan replaced by Query on the request path. |
| 7 | Frontend connected to real compliance data; every fabricated panel removed; findings rendered for the first time; accessibility, TypeScript and API-layer cleanup. |
| 8 | Reviewed remediation workflow: proposal, owner approval, staleness detection, confidence gating, provider contract with a truthful unavailable state. **No image generated, no video modified.** |
| 9 | End-to-end correction from an owner-supplied image: validated asset storage, FFmpeg overlay reconstruction into a **new** video, output validation, re-analysis through the existing pipeline, and evidence-based verification. **Still no image generation; the original upload is never modified; FFmpeg was unavailable locally so its real-binary tests are skipped.** |
| 9.1 | FFmpeg 9.0.1 installed, so the real-binary tests ran for the first time. Three genuine defects fixed: an undecodable image made FFmpeg **block** for the full 301s timeout and that timeout was classified retryable; every happy-path image fixture in the suite was itself that undecodable payload; and `fake_s3` left three modules pointing at a real boto3 client, so 9 extraction tests called real AWS. Suite: 1169 passed / 26 skipped in 54.51s, down from 969s. |

**Phase 5 detail.** Deleted: the orphaned MongoDB/GridFS pipeline (`audit_manager.py`, `verifier.py`, `check_count.py`), `debug_probe.py`, `aws_config.py`, `frontend_backup/` (72 files), the stray `frontend/Video Compliance Dashboard/`, the orphan `/agents` and `/logs` duplicate routes, `infra/init_s3.py`, and `infra/iam_policy.yaml` — 82 files. `S3_BUCKET_NAME` is now required in production and validated; `MONGO_URI` and `Pillow` are gone with their only consumer. Regression tests assert the deleted modules stay deleted, that nothing imports `gridfs`, and that nothing reimplements the bucket fallback.

**Phase 7 detail.** Six views audited against the real route surface and
rewritten where they claimed more than the backend said. Removed: the invented
agent cluster and its metrics (`AgentsView`), the fabricated physical-security
"incidents" (`LogsView`), the unconditional "Audit Passed" card and its
companion fake telemetry (`ProjectDetailView`), the "VERIFIED" badge keyed to
a status the API never returns plus the LLaVA/Act-Agent filter tabs and the
"Post to Social" action (`ClientPortal`), the "Commander Niru" account screen
and the fake header indicators (`page.tsx`), the MDN placeholder video and the
"Network/Nodes" gauges (`HUDView`), and the false SHA-256/AES-256 footer and
non-functional role selector (`/login`). Added: `app/lib/jobs.ts` as the
single source of status and verdict wording, `app/components/ViolationList.tsx`
so a finding's description, severity, confidence, timestamp and frame are
visible at all (they previously existed only as half-second bounding-box
overlays), `app/components/ui/Feedback.tsx` for consistent loading/empty/error/
unavailable states, client-side upload validation against the configured
limits, abort-on-unmount throughout, and 189 new frontend tests. The API
client is now the only module reading `NEXT_PUBLIC_API_URL`. **No backend file
was modified** — `git diff` touches `frontend/`, the two READMEs, `.env.example`
and this file only.

**Phases 5 through 9 did NOT make the application production-ready.** Phase 5 made the architecture coherent; Phase 6 made job execution durable and removed a real class of data loss. Everything still open in §5 "Known remaining gaps" stands, and **nothing has ever run against real AWS or Bedrock** — no worker has run against real infrastructure and no process has been SIGKILLed, so crash recovery is verified as logic, not as observed behaviour.

**Phase 6 detail.** Execution split into two processes: the API queues durably and returns; `backend/worker.py` polls, claims under a lease, and runs. The durable queue is the existing MongoDB `videos` collection — **no SQS, no new AWS resource, no new IAM permission** (`docs/PIPELINE_ARCHITECTURE.md` §8.6 records why SQS was rejected). Also added: `backend/aws_clients.py` so every boto3 client has a finite timeout and a bounded retry budget (only `source_store` had one before); retryable-vs-permanent failure classification with exponential backoff via `not_before`; `/health/live`, `/health/ready` and `/health/worker`; and the per-video frame read moved from a full-table `Scan` to a key-based `Query`. The API response contract is unchanged in both execution modes, and a test asserts it.

**Phase 8 detail.** Resolved §9 q4 (see above) and built the review workflow
that question was blocking. New modules: `remediation_state.py` (12 states and
a validated transition table), `remediation.py` (proposal construction from a
persisted finding, fingerprinting, the confidence gate), `remediation_repo.py`
(MongoDB `remediations`, conditional owner-scoped writes), `image_provider.py`
(the provider contract plus `NullImageProvider`), `remediation_routes.py`
(7 routes), and `ownership.py` — `app.require_video_owner` moved there so the
new router reuses the ONE ownership check instead of a second copy (`app.py`
now delegates; external behaviour unchanged).

Also: `GET /violations/{id}` gained `detection_index` per detection (additive;
a test asserts the existing keys are unchanged), because `/violations`
flattens detections across frames so a client cannot otherwise address a
finding. `db_init` creates the new indexes non-destructively. A real bug was
found and fixed in the process: the analyzer stores bounding boxes as
`Decimal`, which `detections.BoundingBox.parse` rejects (it accepts only
int/float, because its input is model JSON) — so reading a stored box back
silently dropped it. `remediation._normalised_box` converts first.

**Phase 9 detail.** Resolved §9 q3 (see above). New modules:
`remediation_assets.py` (sniffed, bounded asset storage under a new private
prefix), `reconstruction.py` (one FFmpeg overlay pass; audio copied, duration
and dimensions preserved, output probed before anything trusts it),
`remediation_executor.py` (the claimed, leased service layer), and
`verification.py` (the only module that may set `verified` /
`verification_failed`, and only from the OUTPUT's persisted analysis).

Extended: the state machine gained `reconstructing` and the
`approved → asset_ready` edge; `remediation_repo` gained claim/lease/fencing
mirroring `video_repo`; `worker.py` drains the remediation queue in the same
loop and sweeps three recovery cases; `source_store.verify_key_belongs_to`
accepts two prefixes per video id (the invariant it protects is unchanged —
*the key names **this** video*); `aws_resources` gained
`remediation-assets/` and `remediated/`, with matching IAM and CloudFormation
lifecycle rules; four new routes (`/asset`, `/execute`, `/status`, `/output`).

~~⚠️ **FFmpeg was not installed on the development host**~~ **RESOLVED in
Phase 9.1.** FFmpeg 9.0.1 was installed and
`test_integration_ffmpeg_reconstruction.py` now **runs** — 25 tests, 25
passed. A real video *has* now been built by this code, and the overlay was
verified by sampling actual pixels: inside the window the region centre moved
from the source's `(253,253,0)` to `(219,38,38)`, the painted colour; before
the window, after it, and outside the region, the pixels are unchanged. The
source file is byte-identical afterwards (SHA-256 compared), and the output's
dimensions, duration (delta 0.000s) and audio stream are preserved.

**Phase 9.1 detail.** Installing FFmpeg exposed three real defects, each now
fixed with a regression test proven to fail without its fix:

| Defect | Where | Fix |
| :-- | :-- | :-- |
| An undecodable image made FFmpeg **block**, not fail. `-loop 1` on a padded PNG header consumed the full `RECONSTRUCTION_TIMEOUT_SECONDS` — measured at **301s** — and `_run` classifies a timeout as `retryable`, so all 3 attempts were spent on bytes that can never work. | `reconstruction.reconstruct` built the filter graph without ever probing the asset. | `probe_image` is now called **before** the graph is built. Refusal in ~0.1s, `retryable=False`, no partial output. `remediation_assets.validate_asset_bytes` independently probes at upload. |
| The test suite's `PNG` fixture *was* that payload — `b"\x89PNG\r\n\x1a\n" + b"\x00" * N` — so the whole remediation workflow was exercised with a file nothing could decode. | `test_remediation_execution.py`, `test_remediation_routes.py`, `test_reconstruction.py` | `conftest.real_png()` builds a genuine 16x16 PNG from `zlib`+`struct` (stdlib only, no Pillow, no committed binary); `conftest.real_jpeg()` is a real 224-byte JPEG. |
| **The suite made real AWS S3 calls.** `fake_s3` patched only `source_store._client`, but `analyzer`, `extractor` and `video_stitcher` bind `s3_client` at module scope. 9 extraction tests attempted a real `PutObject`, failing on `InvalidAccessKeyId`. Hidden for as long as those tests always skipped. | `tests/conftest.py` | The fixture now imports those three modules and binds the fake into each. `test_the_fake_s3_fixture_binds_every_module_scope_client` asserts it. **No charge was incurred and no object was written, but the suite had left the machine.** |

Also fixed: `test_a_short_ffmpeg_timeout_is_reported_not_hung` patched
`subprocess.Popen.wait` globally, which broke the ffprobe call that runs
*first* — it asserted a timeout it had never actually induced. The patch is
now scoped to the extraction process.

⚠️ **Still true after Phase 9.1:** no image is generated (the provider still
raises), the original upload is never modified, the overlay is a
**concealment** with hard edges — never call it seamless — and **nothing has
run against real AWS or Bedrock**. The 26 MongoDB integration tests remain
unexecuted.

**Phase 10 has not been started.** Remaining: 10 (tests, CI, docs, release). Do not start it until it is explicitly assigned.

⚠️ **Nothing in Phase 8 generated an asset or modified a video, and no code
path can.** `docs/REMEDIATION.md` §8 lists the four things Phase 9 must build,
in order, before any record may move to `verified`.

Deferred out of Phase 7 and still open: frontend views are not URL-addressable
(the shell switches views in client state), the audit detail view loads once
rather than polling, search and filtering are client-side over the
already-fetched list, there is still no end-to-end test from login to review
and no CI (Phase 10), and `.agent/skills/video-audit/SKILL.md` still documents
the wrong bounding-box schema (§2.5, Phase 10.3).

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
- Tailwind utility classes with a `cn()` helper. ~~Duplicated in four components~~ **RESOLVED in Phase 7** — `app/lib/utils.ts` is the only definition; import it.
- CSS custom properties for theming in `app/globals.css` (`--background`, `--primary`, `--accent`, `--critical`) — well structured; keep it.
- ~~`process.env.NEXT_PUBLIC_API_URL` inline at each call site~~ **RESOLVED in Phase 7.** `app/lib/api.ts` is the only module that reads it, and every request goes through that file's `requestJson` helper. Do not call `fetch` from a component.
- ~~**`any` types**~~ and ~~**`alert()` for errors**~~ were already gone before Phase 7; Phase 7 also removed the last non-null assertions, by making `isRenderableDetection` a type predicate that narrows `bounding_box`. **Do not reintroduce `any`, a browser modal, or `@ts-ignore`.**
- **Use the shared feedback primitives** in `app/components/ui/Feedback.tsx`: `LoadingPanel`, `EmptyState`, `ErrorBanner`, `UnavailablePanel`, `StatusBadge`. They encode the accessibility rules so a view cannot forget them — an error is `role="alert"`, a loading placeholder is labelled and `aria-busy`, and a status badge always carries a word so colour is never the only signal. "Nothing yet", "something went wrong" and "this is not built" are three different components on purpose.
- **Every data-backed view needs four states**: loading, empty, error (with a retry where the action is safe), and loaded. A view that renders nothing on an empty list is a defect — that was the old asset grid.
- **Abort on unmount.** Each view creates an `AbortController`, passes `signal` through the API client, and guards `setState` with a `cancelled` flag. ⚠️ In `HUDView` the abort is deliberately on a separate unmount-only effect, *not* the polling effect's cleanup: that cleanup also runs the moment the job turns terminal, which is exactly when the results fetch starts.

---

## 9. Unresolved questions

These block specific phases. Do not decide them unilaterally.

1. ~~Delete the dead MongoDB/GridFS pipeline?~~ **RESOLVED in Phase 5 — deleted**, per the recommendation recorded here. A regression test keeps it deleted.
2. Keep MongoDB for users, or consolidate onto DynamoDB? *(Still open. MongoDB now also holds ownership and job state, so this is a larger decision than it was.)*
3. ~~Is video **reconstruction** in release scope?~~ **RESOLVED in Phase 9 — in scope, built from an owner-supplied replacement image, as a localized overlay over the approved region.** Decision record: `docs/REMEDIATION.md` §9. The output is a **new** video with its own id under `remediated/`; the original upload is never modified. ⚠️ It is a *concealment*, not a repair — hard edges, no grain matching, no motion tracking — and `reconstruction_method` is reported as `scaled_overlay` so nothing downstream can read more into it. **Never describe it as seamless.** Left open deliberately: chained corrections (a second remediation rebuilds from the original, not the first output) and promoting an output to replace its source.
4. ~~Is **remediation** (Nova Canvas) in release scope?~~ **RESOLVED in Phase 8 — in scope as a reviewed proposal workflow; automated image generation is NOT implemented.** Full decision record and rationale: `docs/REMEDIATION.md`. In short: the Canvas request schema could not be verified from anything in this repository (botocore types `InvokeModel.body` as an opaque blob — verified; the deleted `audit_manager.remediate_frame` admitted in its own comment that the schema was unconfirmed), and the roadmap forbids guessing a provider wire format. So Phase 8 built the proposal/approval lifecycle, the provider interface with its error contract, and a provider that truthfully reports itself unavailable. ⚠️ **Do not add a Canvas adapter without first verifying the payload schema against authoritative Bedrock documentation**, and do not re-add a mock: `test_remediation_provider.py` asserts the shipped provider raises rather than returning an image.
5. Which bounding-box schema is canonical? *(De facto: the array form in `prompts.py`.)*
6. Correct model ID — `amazon.nova-pro-v1:0`, or a "Nova 2 Pro" id? Docs and code disagree.
7. Is Nova Act / browser automation in scope at all? If not, `automation.py`, `orchestrator.py`, `/execute`, and the "Neural Command" UI panel can all go. *(Still open, so Phase 5 retained all of them. `backend/safety.py` is retained for the same reason: it is never imported, but its `intercept_action` is the human-in-the-loop gate for exactly this feature, so deleting it would pre-empt this decision. `backend/s3_trigger.py`, an unreferenced Lambda stub with no deployment definition, was retained as uncertain rather than deleted.)*
8. ~~Are the `brand-guardian-*` buckets in `infra/` intentional, or leftovers?~~ **RESOLVED in Phase 5 — leftovers.** No application code has ever read them. The two files that created and granted them (`infra/init_s3.py`, `infra/iam_policy.yaml`) were deleted. ⚠️ **No AWS bucket was touched.** If such buckets were ever created in a real account, deleting them there is a separate operator action.
9. Backend deployment target — ECS, Lambda, App Runner? Only the frontend has a deployment config. *(Phase 6 made this sharper rather than resolving it: there are now **two** long-running processes to place, the API and the worker. The worker is a plain process over MongoDB specifically so this decision stays open — it needs no managed queue. Lambda would not fit the worker as written, since it polls and holds a lease for the length of a job.)*

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
