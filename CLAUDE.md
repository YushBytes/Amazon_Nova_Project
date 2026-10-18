# CLAUDE.md — NovaFlow / Brand Guardian

Guidance for Claude Code sessions in this repository.

**Read `docs/PROJECT_AUDIT.md` before changing anything.** It is an evidence-based audit completed 2026-10-18 with file:line references for every finding. This file summarises what matters operationally; the audit is the authority.

Facts below are marked **[VERIFIED]** (proven during the audit) or **[UNVERIFIED]** (read from source but never executed — most of the backend falls here, because required dependencies and AWS credentials were unavailable). Do not upgrade an [UNVERIFIED] claim without running something.

---

## 1. Critical warnings — read first

### 1.1 The Git repository is rooted at the user's Desktop **[VERIFIED]**

```
git rev-parse --show-toplevel  →  C:/Users/ayush/OneDrive/Desktop
```

A `git init` was run in the Desktop folder, not the project folder. `git status --porcelain -uall | wc -l` at that root reports **37,558 files**, including the user's resumes, personal PDFs, and unrelated projects. The remote repository is **PUBLIC**.

**Therefore:**
- **Never run `git add -A`, `git add .`, or `git commit -a`** until Roadmap Phase 0 is accepted.
- **Verify `git rev-parse --show-toplevel` before every commit.**
- Stage files explicitly by path.
- The project's `.gitignore` is at `Amazon_Nova-main/.gitignore` and governs only that subtree — it does not protect the Desktop root.

Phase 0 of `docs/IMPLEMENTATION_ROADMAP.md` fixes this. It needs maintainer decisions and has not been done.

### 1.2 The remote has almost nothing **[VERIFIED]**

`YushBytes/Amazon_Nova_Project` (PUBLIC) `main` = commit `e059a9c` ("Add files via upload") containing exactly **5 blobs**: `.gitignore`, `README.md`, `analyzer_nova.py`, `prompts.py`, `test_frame.jpg`.

The `backend/`, `frontend/`, `infra/`, `docs/`, and `frontend_backup/` trees have **never been pushed**. Those 5 remote files are byte-identical to their local counterparts (SHA differences are purely `core.autocrlf=true` CRLF normalization), so **nothing on the remote needs preserving beyond the commit itself** — and a normal merge suffices. **Never force-push.**

### 1.3 Three critical defects — do not build on top of them **[VERIFIED by AST]**

| Defect | Location | Effect |
| :-- | :-- | :-- |
| `json` used but never imported at module scope | `backend/analyzer.py:145,155` | Every frame analysis raises `NameError`, swallowed by `except` on line 162. **No detection is ever written to DynamoDB.** The violations API always returns `[]`. |
| async `decode_token` called without `await` | `backend/projects.py:38` | `user` is a coroutine; `user["role"]` raises `TypeError`. **All 6 `/projects` routes return 500.** |
| Remediation returns its input unchanged | `backend/audit_manager.py:116,119` | The Bedrock call is commented out; the caller sets `remediated: True` (line 178). **The system reports success while editing nothing.** |

Roadmap Phase 2 fixes these. Until then, assume the detection pipeline does not work.

### 1.4 The README is materially inaccurate **[VERIFIED]**

`README.md` claims LLaVA/Ollama analysis (**no such code exists** — the only `llava` matches are a cosmetic display string), Nova Act automation (mocked), MongoDB GridFS as the primary store (orphaned), and Next.js 14 (it is 16.1.6). It also publishes test credentials for users no seed script creates.

**Do not treat `README.md` or `docs/planning.md` as a description of the system.** `docs/planning.md` is an unimplemented design target (no Critic, no Step Functions, no HITL). Use `docs/PROJECT_AUDIT.md`.

---

## 2. Verified architecture

### 2.1 Layout

```
Amazon Nova/Amazon_Nova-main/        <- de facto project root (holds .gitignore)
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

Python 3.14.6 (Dockerfile targets 3.11 — mismatch) · Node v24.18.1 · npm 11.16.0 · Docker 29.8.0 · git 2.51.0 (`core.autocrlf=true`) · gh 2.97.0 authenticated as `YushBytes` (scopes `gist`, `read:org`, `repo`, `workflow`) · **FFmpeg NOT INSTALLED** · `boto3`, `motor`, `pymongo`, `jose`, `ffmpeg-python`, `strands`, `nova_act` **all missing** · `frontend/node_modules` absent.

---

## 4. Environment variables

**Names only — never write values into any file, commit, or report.** No `.env` exists on disk; no `.env.example` exists yet (Roadmap Phase 3 adds one).

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

AWS credentials come from the boto3 default chain. The JWT signing key is **hardcoded** at `backend/auth.py:15` rather than env-sourced — Roadmap Phase 3 fixes this.

`.gitignore` correctly covers `.env*`. **Never commit a `.env`.** When reporting, name variables and redact values.

---

## 5. Security posture

**No live secrets exist in the repository or its history** [VERIFIED] — a pattern scan for AWS key IDs, `mongodb+srv://` URIs, API keys, and bearer tokens found none, no `.env` is on disk, and there is no local Git history at all.

Known issues, all in `docs/PROJECT_AUDIT.md` §10:

- **Hardcoded JWT secret** (`auth.py:15`) in a **public** repo — anyone can forge a token for any role.
- **Privilege escalation at signup** — `role` is client-supplied (`auth.py:56,97`); anyone can self-register as `agent`.
- **Unsalted SHA-256 password hashing** (`auth.py:24`).
- **Path traversal on upload** — the extension is taken from the client filename (`app.py:75`) and `split('.')[-1]` does not strip separators.
- **No auth on 10 of 16 routes**; `/uploads/*` is served publicly with no auth or expiry.
- **No file size, MIME, or duration validation** server-side.
- **CORS `allow_origins=["*"]`** (`app.py:30`), conflicting with the auth sub-app's restrictive policy.
- README publishes test credentials (`password123`) for users that do not exist.

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

Phase 1 (audit) is **complete but uncommitted** — committing requires Phase 0. **Phase 0 is blocked** on three maintainer decisions (project root, approval to remove the Desktop `.git`, repository visibility). Do not start Phase 2 until Phase 0 is accepted.

---

## 7. Git safety rules

- **Verify `git rev-parse --show-toplevel`** is the project root before every commit. See §1.1.
- **Stage explicitly by path.** No `git add -A` / `git add .` until Phase 0 is accepted and a verified `.gitignore` is in place.
- Read `git status` and `git diff --cached` before committing.
- `git fetch` and inspect remote state before pushing. On conflict, **stop and report** — never force.
- **Never force-push.** The remote's single commit must be preserved, and its content is byte-identical to local, so a normal merge works.
- If a push fails for auth/network/permission reasons, **preserve the local commit** and report the exact error. Never claim work was pushed when it was not.
- Do not create another GitHub repository. Do not delete branches or rewrite history.
- `core.autocrlf=true` is set and there is no `.gitattributes` — expect spurious line-ending diffs until Phase 0 adds one.

---

## 8. Conventions

Observed in the existing code, for consistency. Several are **bad practices the roadmap replaces** — flagged as such; do not propagate them into new code.

**Backend (Python)**
- 4-space indent; module-level constants in `SCREAMING_SNAKE_CASE`; `load_dotenv()` at module top.
- boto3 clients are built at module scope — convenient for CLI use, but it makes testing harder and means no shared timeout config. New code should accept an injected client.
- Each pipeline module has an `if __name__ == "__main__":` CLI block with `sys.argv` parsing. Keep this; it is genuinely useful.
- ⚠️ **`print()` is used for all logging** (only `s3_trigger.py` configures a logger). **Use `logging` in new code.**
- ⚠️ **Bare `except Exception` that logs and continues** is pervasive and is the direct cause of the §1.3 defects being silent. **Catch specific exceptions and record a terminal failure state.**
- ⚠️ **Raw exception strings are returned to clients** (`app.py:155`, `projects.py:201`). Do not do this.
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

- Never `git add -A` / `git add .` / `git commit -a` while the repo is rooted at the Desktop (§1.1).
- Never force-push, rewrite history, or create another GitHub repository.
- Never commit a `.env`, a credential, `node_modules/`, `backend/uploads/`, `temp_frames_*/`, or build artifacts.
- Never present a mock, hardcoded value, or silent fallback as real AI output.
- Never claim a test passed, a build succeeded, or a push completed without the actual output.
- Never mark an audit or roadmap item verified because the code exists.
- Never introduce a new database, framework, AI provider, or cloud service without an explicit decision.
- Never run the Bedrock or video pipeline against real AWS without confirming cost is acceptable — frame-by-frame Nova Pro calls bill per frame.
- Never print a secret value into a report, a commit message, or the terminal.
