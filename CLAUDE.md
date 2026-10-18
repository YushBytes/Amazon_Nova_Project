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
| Remediation returned its input unchanged | `backend/audit_manager.py` | The disguised mock was removed. Real Nova Canvas remediation is a later phase and is **not** implemented — do not re-add a stub that reports success. |

**Still true:** the pipeline has never been run end to end against live AWS, so
detection accuracy is unverified. See §3 for what has and has not been executed.

### 1.4 The README is materially inaccurate **[VERIFIED]**

`README.md` claims LLaVA/Ollama analysis (**no such code exists** — the only `llava` matches are a cosmetic display string), Nova Act automation (mocked), MongoDB GridFS as the primary store (orphaned), and Next.js 14 (it is 16.1.6). It also publishes test credentials for users no seed script creates.

**Do not treat `README.md` or `docs/planning.md` as a description of the system.** `docs/planning.md` is an unimplemented design target (no Critic, no Step Functions, no HITL). Use `docs/PROJECT_AUDIT.md`.

---

## 2. Verified architecture

### 2.1 Layout

```
Amazon Nova/                         <- project root (flattened in Phase 0; holds .gitignore)
├── backend/          FastAPI service, 17 Python modules
├── frontend/         Next.js 16 App Router dashboard (the live one)
├── frontend_backup/  375 KB ORPHANED Vite dashboard — zero references
├── infra/            CloudFormation IAM + S3 bootstrap (not deployed)
├── docs/             planning.md (aspirational) + the audit deliverables
├── analyzer_nova.py  Standalone Nova Pro single-frame CLI (the one working AI path)
└── prompts.py        DIVERGES from backend/prompts.py — only the backend one is imported
```

### 2.2 Stack **[VERIFIED from source]**

- **Frontend:** Next.js 16.1.6, React 19.2.3, TypeScript 5.9.3 (`strict: true`), Tailwind CSS v4, Framer Motion, lucide-react.
- **Backend:** FastAPI + Uvicorn, `python-jose` JWT (HS256), `motor` (async MongoDB), `boto3`.
- **AI:** Amazon Bedrock, model `amazon.nova-pro-v1:0`. *(Docs say "Nova 2 Pro"; code says `nova-pro-v1:0`. Unresolved.)*
- **Storage:** S3 (`novaflow-media`) for frame JPEGs; DynamoDB (`NovaFlowFrames`, `NovaFlowProjects`) for metadata; MongoDB (`novaflow.users`) for accounts; local `backend/uploads/` for raw videos.
- **Video:** `ffmpeg-python` + the FFmpeg binary, 1 fps extraction.
- **Deploy:** AWS Amplify (frontend only); `backend/Dockerfile` exists with no deployment definition.

### 2.3 Storage reality vs. documentation **[VERIFIED]**

The live pipeline uses **S3 + DynamoDB**, not MongoDB/GridFS. GridFS appears only in `audit_manager.py` and `verifier.py`, which are **unreachable from the API** — and structurally unrunnable, because they read `meta["gridfs_id"]` and **nothing in the repository ever writes a GridFS frame or a `frame_metadata` document**. MongoDB is used *only* for user accounts.

**Two parallel incompatible pipelines exist.** Pipeline A (live): `extractor.py` → `analyzer.py` → S3 + DynamoDB. Pipeline B (dead): `audit_manager.py` → MongoDB + GridFS. Audit §4.4 recommends deleting B. Do not revive it without a decision.

### 2.4 Request flow **[VERIFIED]**

```
Browser (:3000)
  ├─ POST /auth/login ──► auth.py sub-app ──► MongoDB novaflow.users
  ├─ POST /upload-reel ─► app.py:69 ──► local disk ./uploads/   (NO AUTH)
  ├─ GET  /submissions ─► app.py:157 ──► MOCKED (status from hash()!)
  └─ GET  /violations/{id} ─► app.py:123 ──► DynamoDB (always empty, §1.3)

  ✗ POST /analyze/{id}  — NEVER CALLED BY THE FRONTEND
  ✗ /projects/* (6)     — never called; all 500
  ✗ POST /execute       — never called; returns mock text
```

**Only 4 of 16 routes are used by the UI. 10 of 16 have no authentication.**

### 2.5 Known architectural inconsistencies **[VERIFIED]**

- **Three conflicting bounding-box schemas:** `backend/prompts.py:16` uses `bounding_box: [ymin,xmin,ymax,xmax]`; `audit_manager.py:39` uses `box_2d`; `.agent/skills/video-audit/SKILL.md` uses `{x,y,width,height}` + `timestamp_ms`. **The frontend consumes the array form** (`HUDView.tsx:125`), so `prompts.py` is the de facto contract and `SKILL.md` is wrong.
- **Bucket mismatch:** `infra/` provisions `brand-guardian-raw`/`-final`; all code uses `novaflow-media`. The IAM role grants access to unused buckets and **none to the bucket in use**, and has **no DynamoDB permissions at all**.
- **Env var collision:** `MONGODB_URL` (`db.py:5`) vs `MONGO_URI` (`audit_manager.py:13`).
- **`requirements.txt` omits `boto3`**, `ffmpeg-python`, `Pillow`, and `python-multipart` — all imported by the code. **The backend cannot start from a clean install of the manifest.** Nothing is version-pinned.

---

## 3. Commands

### Verified working **[VERIFIED]**

```bash
python -m py_compile backend/*.py analyzer_nova.py prompts.py   # passes — all 19 files
git rev-parse --show-toplevel                                   # ALWAYS run before committing
gh repo view YushBytes/Amazon_Nova_Project
```

### Declared but unverified — prerequisites are missing **[UNVERIFIED]**

```bash
# Backend — WILL FAIL: boto3/motor/jose not installed, and boto3 is absent from requirements.txt
cd backend && uvicorn app:app --host 0.0.0.0 --port 8000

# Frontend — node_modules has never been installed
cd frontend && npm install && npm run dev      # :3000
cd frontend && npm run build
cd frontend && npm run lint                    # NOTE: "eslint" with no target — lints nothing useful

# CLI utilities — require ffmpeg-python + the FFmpeg binary (NOT installed on this host)
cd backend && python extractor.py <video.mp4> [video_id]
cd backend && python analyzer.py <image.jpg | video_id>
cd backend && python video_stitcher.py <video_id> [out.mp4]
```

### Does not exist **[VERIFIED]**

- **No tests anywhere.** No `tests/`, no `test_*.py`, no `*.spec.ts`, no `pytest.ini`, no `pyproject.toml`, no Jest/Vitest/Playwright, no `test` script in `package.json`.
- **No CI.** No `.github/` directory.
- **No `typecheck` script**, despite `strict: true`. Use `npx tsc --noEmit`.
- **No `.env.example`.**

When a phase adds tests, run them and **quote the real output**. Never write "tests pass" without it.

### Environment on this machine **[VERIFIED 2026-10-18]**

Python 3.14.6 (Dockerfile targets 3.11 — mismatch) · Node v24.18.1 · npm 11.16.0 · Docker 29.8.0 · git 2.51.0 (`core.autocrlf=false` set locally for this repo) · gh 2.97.0 authenticated as `YushBytes` (scopes `gist`, `read:org`, `repo`, `workflow`) · **FFmpeg NOT INSTALLED** · `boto3`, `motor`, `pymongo`, `jose`, `ffmpeg-python`, `strands`, `nova_act` **all missing** · `frontend/node_modules` absent.

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

Pre-existing application configuration:

| Name | Read at | Default in code |
| :-- | :-- | :-- |
| `AWS_REGION` | 8 modules | `us-east-1` |
| `S3_BUCKET_NAME` | `extractor`, `analyzer`, `projects`, `video_stitcher` | `novaflow-media` |
| `DYNAMODB_TABLE_NAME` | `extractor`, `analyzer`, `app`, `video_stitcher` | `NovaFlowFrames` |
| `MONGODB_URL` | `db.py:5` | `mongodb://localhost:27017` |
| `MONGODB_DB_NAME` | `db.py:6` | `novaflow` |
| `MONGO_URI` | `audit_manager.py:13` | duplicate of `MONGODB_URL` — consolidate |
| `API_URL` | `app.py:82` | `http://localhost:8000` |
| `BEDROCK_GUARDRAIL_ID` | `safety.py:13` | **none** |
| `BEDROCK_GUARDRAIL_VERSION` | `safety.py:14` | `1` |
| `NEXT_PUBLIC_API_URL` | 4 frontend files | `http://localhost:8000` |

AWS credentials come from the boto3 default chain, not from these variables. The JWT signing key is read from `JWT_SECRET_KEY`; it is no longer in the source.

`.gitignore` correctly covers `.env*`. **Never commit a `.env`.** When reporting, name variables and redact values.

---

## 5. Security posture

**Phase 3 (2026-10-19) hardened this area. Full detail: `docs/SECURITY_AUDIT.md`.**

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
- **Media access uses a scoped token.** A browser `<video>` element cannot send an `Authorization` header, so `GET /uploads/{file}` takes a 15-minute token bound to one `video_id`. Session tokens and media tokens are not interchangeable.

### Known remaining gaps

- **No rate limiting** on `/auth/login` or `/auth/signup` — the most significant outstanding gap.
- **No token revocation.** Tokens live in `localStorage` and are valid until expiry; rotating `JWT_SECRET_KEY` is the only break-glass.
- **`users.username` has no unique index** — concurrent signups can duplicate a username (low impact; operator step documented).
- **Pre-Phase-3 uploads have no ownership record** and are therefore inaccessible.
- **S3 bucket hardening and the IAM policy gaps** from `docs/PROJECT_AUDIT.md` §10.3 are untouched.
- Upload validation does **not** prove a file is a safe video; there is no content scanning.

When working in this repo: never print a secret value, never commit one, and if you find one, report its **file and variable name** with the value redacted and recommend rotation — do not delete user files or change cloud permissions yourself.

---

## 6. Phase workflow — mandatory rules

Work proceeds phase by phase per `docs/IMPLEMENTATION_ROADMAP.md`. Every session must follow these:

1. **Work only on the current assigned phase.** Do not pull work forward, even when it looks trivial.
2. **Inspect the relevant existing code before modifying it** — the file and its callers.
3. **Preserve working behavior.** Avoid rewrites. Do not replace a working component because another technology is available.
4. **Implement real functionality.** Never present a mock, hardcoded value, or silent fallback as genuine AI output. `audit_manager.py:119` (returns the input, reports success) and `orchestrator.py:17` (returns `"Mock Response"` as a plan) are the exact patterns to eliminate — **do not reintroduce them anywhere.** If something cannot be implemented, leave it visibly unimplemented and say so.
5. **Write or update tests for every meaningful change.**
6. **Run relevant tests, lint, type checks, and builds** when applicable.
7. **Review the final diff** for regressions, secrets, generated files, and accidental changes.
8. **Report exact verification results and remaining blockers**, quoting real command output.
9. **Never mark an acceptance criterion complete without evidence.** "The code exists" is not evidence.
10. **Do not begin the next phase** until the current one is reviewed and explicitly accepted.
11. **Commit and push at each phase checkpoint**, subject to §7.
12. **Never force-push, amend published history, commit credentials, or silently overwrite unrelated changes.**

### Current phase status

Phases 0, 1, 2 and 3 are **complete, committed, and pushed**: the repository is correctly rooted, flattened, private and tracking `origin/main`; the four critical Phase 1 defects are fixed with regression tests; and authentication, authorization, resource ownership and upload handling are hardened (`docs/SECURITY_AUDIT.md`). **Phase 4 (connect the end-to-end pipeline) is next and has not been started.** It still needs the maintainer decisions recorded in §9 — do not start it until it is explicitly assigned.

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
- `logging` is used in `app.py`, `auth.py`, `config.py`, `projects.py`, `video_repo.py` and `analyzer.py`. ⚠️ Several older modules (`extractor.py`, `video_stitcher.py`, `audit_manager.py`, `safety.py`) still use `print()`. **Use `logging` in new code.**
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

1. Delete the dead MongoDB/GridFS pipeline (`audit_manager.py`, `verifier.py`, `check_count.py`)? *(Recommended: yes — unreachable and unrunnable.)*
2. Keep MongoDB for users, or consolidate onto DynamoDB?
3. Is video **reconstruction** in release scope, or is detection + reporting enough for v1?
4. Is **remediation** (Nova Canvas) in release scope? It is currently a disguised mock and must be built properly or removed.
5. Which bounding-box schema is canonical? *(De facto: the array form in `prompts.py`.)*
6. Correct model ID — `amazon.nova-pro-v1:0`, or a "Nova 2 Pro" id? Docs and code disagree.
7. Is Nova Act / browser automation in scope at all? If not, `automation.py`, `orchestrator.py`, `/execute`, and the "Neural Command" UI panel can all go.
8. Are the `brand-guardian-*` buckets in `infra/` intentional, or leftovers?
9. Backend deployment target — ECS, Lambda, App Runner? Only the frontend has a deployment config.

---

## 10. Things to never do in this repository

- Never `git add -A` / `git add .` / `git commit -a` — stage by explicit path; the stray Desktop repository still exists (§1.1).
- Never force-push, rewrite history, or create another GitHub repository.
- Never commit a `.env`, a credential, `node_modules/`, `backend/uploads/`, `temp_frames_*/`, or build artifacts.
- Never present a mock, hardcoded value, or silent fallback as real AI output.
- Never claim a test passed, a build succeeded, or a push completed without the actual output.
- Never mark an audit or roadmap item verified because the code exists.
- Never introduce a new database, framework, AI provider, or cloud service without an explicit decision.
- Never run the Bedrock or video pipeline against real AWS without confirming cost is acceptable — frame-by-frame Nova Pro calls bill per frame.
- Never print a secret value into a report, a commit message, or the terminal.
