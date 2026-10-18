# NovaFlow / Brand Guardian — Implementation Roadmap

**Created:** 2026-10-18 (Phase 1 audit)
**Basis:** `docs/PROJECT_AUDIT.md` — every phase below traces to a numbered finding there.
**Status:** Phase 0 is blocked pending maintainer decisions. No phase has started.

---

## How to use this roadmap

Phases are **dependency-ordered**. Do not start a phase until its listed dependencies are accepted. Phase numbering reflects the order the audit's findings must be addressed, not a generic template — Phase 0 exists because the Git state is unsafe, and the defect-fix phases (2–4) precede all feature work because three critical bugs currently make the product's core feature impossible.

**Scope boundary:** Phases 0–10 are **required for release**. Phases 11–13 are **optional enhancements** and must not be started before Phase 10 is accepted.

### Non-negotiable rules for every phase

These apply to every implementation session. They are repeated in `CLAUDE.md`.

1. **Work only on the current assigned phase.** Do not pull work forward from later phases, even when it looks trivial.
2. **Inspect the relevant existing code before modifying it.** Read the file and its callers first.
3. **Preserve working behavior.** Avoid rewrites. The audit found working components; do not replace them because a different technology is available.
4. **Implement real functionality.** Never present a mock, a hardcoded value, or a silent fallback as genuine AI output. If something cannot be implemented, say so and leave it visibly unimplemented. Finding §9.3 (remediation that reports success while doing nothing) is the exact failure mode to eliminate — do not reintroduce it anywhere.
5. **Write or update tests for every meaningful change.**
6. **Run the relevant tests, lint, type checks, and builds.** Record the actual commands and their actual output.
7. **Review the final diff** for regressions, secrets, generated files, and accidental changes before committing.
8. **Report exact verification results and remaining blockers.** Quote real output.
9. **Never mark an acceptance criterion complete without supporting evidence.** "The code exists" is not evidence.
10. **Do not begin the next phase** until the current one has been reviewed and explicitly accepted by the maintainer.
11. **Commit and push at each phase checkpoint**, subject to the Git safety rules below.
12. **Never force-push, amend published history, commit credentials, or silently overwrite unrelated changes.**

### Git safety rules (apply to every checkpoint)

- Verify `git rev-parse --show-toplevel` is the project root **before every commit**. The audit found the repository rooted at the user's Desktop (§12.1); re-confirm it every time.
- Stage files **explicitly by path**. Never `git add -A` or `git add .` until Phase 0 is accepted and a verified `.gitignore` is in place.
- Run `git status` and `git diff --cached` and read them before committing.
- `git fetch` and inspect remote state before pushing. If a push would conflict, **stop and report** — never force.
- If a push fails for auth/network/permission reasons, **preserve the local commit** and report the exact error. Never claim work was pushed when it was not.

---

# PART I — REQUIRED FOR RELEASE

## Phase 0 — Git safety and repository root

**Status: BLOCKED — requires maintainer decisions before any action.**

### Objective
Establish a correctly-rooted Git repository tracking `YushBytes/Amazon_Nova_Project`, so that the project can be committed without exposing unrelated personal files.

### Why this is first
The audit (§12) found a Git repository initialised at `C:\Users\ayush\OneDrive\Desktop`. A commit from that root would capture **37,558 files** — resumes, personal PDFs, and unrelated projects — into a **public** repository. No other work can be safely committed until this is fixed.

### Blocking decisions required from the maintainer
1. **Project root:** keep `Amazon Nova/Amazon_Nova-main/`, or flatten its contents up into `Amazon Nova/`? *(Recommended: flatten — `Amazon_Nova-main` is a zip-extraction artefact.)*
2. **Approval to remove `C:\Users\ayush\OneDrive\Desktop\.git`.** Verified safe: `git show-ref` returns empty and `git stash list` is empty, so that repository contains **zero commits and no recoverable history**. Explicit approval is still required.
3. **Repository visibility:** keep public, or switch to private until the Phase 2 security fixes land? *(Recommended: private until Phase 2 is accepted.)*

### Scope
Git configuration and repository hygiene only. **No application code.**

### Tasks
1. Confirm the three decisions above in writing.
2. Remove the stray Desktop `.git` directory (after approval).
3. `git init` in the chosen project root; set `user.email` locally if no global value exists (`git config --local --list` showed only `user.name`).
4. Add `.gitattributes` with `* text=auto eol=lf` — `core.autocrlf=true` is set, and without this the first commit produces spurious whole-file diffs on `analyzer_nova.py` and `prompts.py` (§12.4).
5. Add remote `origin` → `https://github.com/YushBytes/Amazon_Nova_Project`.
6. `git fetch origin` and base local `main` on the existing remote commit `e059a9c` so that commit is **preserved, not overwritten**. The remote's 5 files are byte-identical to local (§12.4), so a normal merge suffices — **no force-push**.
7. Extend `.gitignore` with the gaps from §10.6: `backend/uploads/`, `temp_frames_*/`, `test_output.jpg`, `final_compliant_video.mp4`, `.vscode/`, `desktop.ini`, `Thumbs.db`, `*.swp`.
8. Untrack `.claude/settings.local.json` and add it to `.gitignore` (§10.6 — a `*.local.json` is developer-local and currently pre-approves `pip install:*`, `npm install:*`, `curl:*`).
9. Add a `LICENSE` (public repo currently has none).
10. Commit the Phase 1 audit deliverables, which are already written to disk and awaiting this phase.

### Expected files
`.gitattributes` (new), `.gitignore` (edit), `LICENSE` (new), `docs/PROJECT_AUDIT.md`, `docs/IMPLEMENTATION_ROADMAP.md`, `docs/RELEASE_CHECKLIST.md`, `CLAUDE.md`.

### Dependencies
None — but blocked on the three decisions.

### Acceptance criteria
- [ ] `git rev-parse --show-toplevel` returns the chosen project root, **not** the Desktop.
- [ ] `git status --porcelain --untracked-files=all | wc -l` returns a count consistent with the project only (expect < 150, not 37,558).
- [ ] `git log --oneline` shows the remote's `e059a9c` in history — it was not discarded.
- [ ] `git remote -v` points at `YushBytes/Amazon_Nova_Project`.
- [ ] `git status` shows no personal or unrelated files as untracked or staged.
- [ ] No secrets in the diff (`git diff --cached` reviewed in full).
- [ ] Push succeeded without `--force`.

### Required tests
None (no application code). Documentation-only phase.

### Verification commands
```bash
git rev-parse --show-toplevel
git status --porcelain --untracked-files=all | wc -l
git log --oneline --graph -5
git remote -v
git diff --cached --stat
git ls-files | head -50
```

### Human intervention
**Required** — all three decisions above, plus approval to delete the Desktop `.git`.

### Checkpoint
`chore: initialize repository at project root and add .gitattributes` + `docs: audit existing project and define implementation roadmap`. Push to `origin main`.

---

## Phase 1 — Audit and roadmap *(COMPLETE, pending commit)*

### Objective
Produce an evidence-based understanding of the existing project and a safe path to release.

### Delivered
- `docs/PROJECT_AUDIT.md` — full audit, 15 sections, every finding file:line referenced.
- `docs/IMPLEMENTATION_ROADMAP.md` — this document.
- `docs/RELEASE_CHECKLIST.md` — release gate.
- `CLAUDE.md` — conventions for future sessions.

### Acceptance criteria
- [x] Workspace and Git state verified; unsafe state identified and **not** modified.
- [x] All 19 Python modules and all frontend source files read.
- [x] 12-stage workflow classified with evidence.
- [x] Architecture mapped from source, not documentation.
- [x] Security and reliability audited; no live secrets found.
- [x] Baseline recorded, including what could not run and why.
- [x] Four deliverables written.
- [ ] **Committed and pushed** — blocked by Phase 0.

### Checkpoint
Folded into Phase 0's commit, since committing requires the Phase 0 fix.

---

## Phase 2 — Critical defect fixes

### Objective
Fix the three critical defects that make the product's core feature impossible, plus the dependency manifest that prevents the backend from starting at all.

### Why this is before everything else
Per the audit, no violation is ever persisted (§9.1), no project endpoint works (§9.2), and remediation reports success while doing nothing (§9.3). Any feature or UI work before these fixes builds on a non-functional base.

### Scope
`backend/requirements.txt`, `backend/analyzer.py`, `backend/projects.py`, `backend/audit_manager.py`, and a new test suite. **No new features. No new dependencies beyond those already imported by existing code.**

### Tasks

**2.1 — Fix the dependency manifest (§3, "Missing from requirements.txt")**
- Add the packages the code already imports but the manifest omits: `boto3`, `ffmpeg-python`, `Pillow`, `python-multipart`.
- **Pin every dependency** to an explicit version — all 8 current entries are bare names, so builds are not reproducible.
- Reconcile the Python version: code was syntax-checked on 3.14.6 but `backend/Dockerfile:2` targets `python:3.11-slim`. Pick one and make both agree.
- Decide on `strands-agents` and `nova-act` — see Phase 2.5 below.

**2.2 — Fix `analyzer.py` `NameError` (§9.1) — CRITICAL**
- Add `import json` and `import re` at module scope; remove the function-local imports at lines 57-58 and 87.
- Replace the bare `except Exception` at line 162 with handling that marks the frame `audit_failed` in DynamoDB rather than leaving it `pending_audit` forever.
- Remove the dead `body` dict at lines 28-51 (abandoned Anthropic-format scaffolding, never used).
- Harden the JSON extraction at line 88: the greedy `r'\{.*\}'` spans from the first `{` to the last `}` and breaks on multi-object responses. Validate the parsed result against a Pydantic model rather than trusting it.

**2.3 — Fix `projects.py` missing `await` (§9.2) — CRITICAL**
- Make `get_current_user` `async def` and `await decode_token(token)`.
- Fix `boto3.dynamodb.conditions.Attr` at line 76 — use `from boto3.dynamodb.conditions import Attr` as the other modules correctly do. The audit flagged this as a likely `AttributeError` surfacing immediately after the await fix (§7.2).
- Move the hardcoded `TABLE_NAME = "NovaFlowProjects"` (line 14) to an env var, matching `NovaFlowFrames`.
- Replace the broad `except Exception` at lines 103-105 that reports infrastructure errors as "already taken".
- Stop returning raw exception strings to clients (lines 201, and `app.py:155`).

**2.4 — Remove the disguised remediation mock (§9.3) — CRITICAL**
- `audit_manager.remediate_frame()` currently returns its input unchanged (line 119) while the caller sets `remediated: True` (line 178). **Do not "fix" this by leaving it silent.** Either:
  - **(a)** delete the function and its caller branch outright, deferring real remediation to Phase 8; **or**
  - **(b)** raise `NotImplementedError` so no code path can report a false success.
- **(a) is recommended**, pending the Phase 0 decision on whether to delete the whole MongoDB pipeline (audit §13 question 1).
- Note `audit_frame()` in the same file can never succeed either — line 54 passes raw `bytes` into `json.dumps`. If the file survives, fix or remove it; do not leave an always-failing path.

**2.5 — Make AI dependency fallbacks loud (§9.9)**
- `orchestrator.py:10-18` and `automation.py:6-15` silently substitute mock classes when `strands`/`nova_act` are missing, and `/execute` returns `"Mock Response (strands_agents missing)"` as if it were a plan.
- Either remove these modules and the `/execute` route (if browser automation is out of scope — audit §13 question 7), or make the fallback fail loudly and gate the route behind an explicit feature flag the response surfaces.

### Expected files
`backend/requirements.txt`, `backend/analyzer.py`, `backend/projects.py`, `backend/audit_manager.py` (or deleted), `backend/orchestrator.py`, `backend/automation.py`, `backend/app.py`, new `backend/tests/`, new `pytest.ini` or `pyproject.toml`.

### Dependencies
Phase 0 (safe commits).

### Required tests
First tests in the project — this phase establishes the harness.
- `test_analyzer.py`: `check_pending_frames` persists detections given a mocked Bedrock response (this test **must fail** against the current code and pass after the fix); malformed model JSON is handled without raising; a frame that fails analysis is marked `audit_failed`, not left `pending_audit`.
- `test_projects.py`: `get_current_user` returns a dict, not a coroutine; each of the 6 routes returns 200/403 as appropriate rather than 500; role checks reject the wrong role.
- Mock all AWS calls with `botocore.stub.Stubber` or `moto`. **No test may call real AWS.**

### Acceptance criteria
- [ ] `pip install -r backend/requirements.txt` in a clean venv, then `python -c "import app"` succeeds.
- [ ] Every dependency in `requirements.txt` is version-pinned.
- [ ] A test demonstrates `detections` being written to DynamoDB (stubbed) — proving §9.1 is fixed.
- [ ] A test demonstrates all 6 `/projects` routes returning non-500 — proving §9.2 is fixed.
- [ ] `grep -rn "return image_bytes" backend/` returns nothing in a remediation path, and no code path sets `remediated: True` without a real edit.
- [ ] No module silently substitutes a mock for an AI client.
- [ ] `pytest` passes with real output quoted in the report.

### Verification commands
```bash
python -m venv .venv && . .venv/Scripts/activate
pip install -r backend/requirements.txt
python -c "import sys; sys.path.insert(0,'backend'); import app; print('import OK')"
pytest -v
pip list --format=freeze | head -30
```

### Human intervention
Decisions on audit §13 questions 1, 4, and 7 (delete the MongoDB pipeline? is remediation in scope? is Nova Act in scope?).

### Checkpoint
`fix: resolve critical defects in analyzer, projects, and remediation path`

---

## Phase 3 — Security hardening

### Objective
Close the authentication, authorization, and upload-safety holes before the full codebase is pushed to a public repository.

### Scope
`backend/auth.py`, `backend/app.py`, `backend/db.py`, `frontend/app/context/AuthContext.tsx`, new `frontend/middleware.ts`. **No feature work.**

### Tasks

**3.1 — Authentication (§10.2)**
- Move the JWT signing key out of source (`auth.py:15`) to a required env var. **Fail fast at startup if unset** — never fall back to a default.
- Replace unsalted `hashlib.sha256` password hashing (`auth.py:23`) with a password KDF (`bcrypt` or `argon2`). Add the dependency, pinned. Include a migration path for any existing user records.
- **Remove client-controlled role selection at signup** (`auth.py:56,97,102`) — currently any anonymous user can self-provision an `agent` account. Default all signups to `client`; agent promotion must be a separate, authorized action.
- Add a unique index on `users.username` (§7.3) — the current read-then-write check races.
- Add rate limiting to `/auth/login` and `/auth/signup`.
- Re-mount auth as an `APIRouter` via `include_router` instead of `app.mount` (§6.2) — fixes the duplicate conflicting CORS policy, the wrong `tokenUrl="/login"`, and the routes' absence from `/openapi.json`.

**3.2 — Authorization (§10.2)**
- Add auth dependencies to `/upload-reel`, `/analyze/{id}`, `/violations/{id}`, `/submissions`, and `/execute` — currently 10 of 16 routes have none.
- Enforce ownership: a user may only read violations for a video they own.
- Add `frontend/middleware.ts` for route protection, and call `GET /auth/me` on app load to validate the stored token (`AuthContext.tsx:28-42` currently trusts `localStorage` unconditionally).

**3.3 — Upload safety (§10.1)**
- Validate the file extension against an allowlist; **never** derive the stored path from the client filename (`app.py:75` enables path traversal via `a.mp4/../../evil`). Confirm the resolved path stays inside `UPLOAD_DIR`.
- Enforce a server-side size limit while streaming.
- Validate content by magic bytes and an FFmpeg probe before acceptance, not just the client-side `accept="video/*"`.
- Enforce the 60-second duration limit **at upload**, with a clear error — rather than silently dropping it later (§9.10).
- Require auth for `/uploads/*` static serving, or replace it with short-lived presigned URLs (`app.py:38` currently serves every upload publicly).

**3.4 — Configuration and logging (§10.4, §10.6)**
- Restrict CORS from `allow_origins=["*"]` (`app.py:30`) to the known frontend origin.
- Replace `print()` with the `logging` module throughout; ensure FFmpeg stderr (which includes full file paths) and model analysis text (which describes user video content) are not logged at info level.
- Stop returning raw exception strings to clients.
- Add `.env.example` listing every variable **by name only** (the 11 in audit §8.5). No values.

### Expected files
`backend/auth.py`, `backend/app.py`, `backend/db.py`, `backend/requirements.txt`, `frontend/middleware.ts` (new), `frontend/app/context/AuthContext.tsx`, `.env.example` (new), `backend/tests/test_auth.py`, `backend/tests/test_upload_security.py`.

### Dependencies
Phase 2.

### Required tests
- Signup cannot create an `agent` account.
- A token signed with a different secret is rejected.
- Passwords are not stored as plain SHA-256 (verify the hash format).
- **Path-traversal test:** a filename of `a.mp4/../../evil.py` does not write outside `UPLOAD_DIR`.
- Oversized upload is rejected.
- A non-video file with a `.mp4` extension is rejected.
- Each protected route returns 401 without a token and 403 for the wrong role.

### Acceptance criteria
- [ ] `grep -rniE "secret_key *=|password123" backend/ README.md` returns no hardcoded secret or credential.
- [ ] Backend refuses to start without the JWT secret env var.
- [ ] Path-traversal test passes.
- [ ] No route that reads or writes user data is unauthenticated.
- [ ] `.env.example` exists and lists all 11 variables by name, with no values.
- [ ] `pytest` passes, output quoted.

### Verification commands
```bash
pytest -v backend/tests/test_auth.py backend/tests/test_upload_security.py
grep -rniE "secret_key *=|password123|AKIA[0-9A-Z]{16}" backend/ frontend/app/ README.md
# Confirm startup fails without the secret:
python -c "import sys; sys.path.insert(0,'backend'); import app"   # expect a clear config error
```

### Human intervention
Provide the JWT secret for local dev via `.env` (never committed). Decide whether to make the repository private until this phase is accepted.

### Checkpoint
`fix(security): harden auth, authorization, and upload validation`

---

## Phase 4 — Connect the end-to-end pipeline

### Objective
Make a single upload actually produce persisted, reviewable violations — the first time the product's core loop works.

### Scope
`frontend/app/components/HUDView.tsx`, `backend/app.py`, and a new job/status record. **Detection only** — no remediation, no reconstruction.

### Tasks

**4.1 — Trigger analysis (§9.7)**
- `HUDView.tsx`'s "Launch Audit" button uploads but never calls `POST /analyze/{video_id}`. Wire it, and surface real progress.

**4.2 — Introduce a job record (§5 stage 3, §10.4)**
- There is currently **no job or audit record** — upload persists nothing, so a failed pipeline is indistinguishable from a pending one.
- Add a job/status record (DynamoDB, consistent with the existing storage choice — do **not** introduce a new database) tracking: `video_id`, owner, status, stage, counts, error, timestamps.
- Add `GET /jobs/{video_id}` (or extend `/violations`) so the UI can show real status and real errors.
- Make `/analyze` idempotent — guard against the duplicate-job hazard at `app.py:92` where repeated calls re-extract over the same S3 keys.

**4.3 — Fix silent pipeline drop-outs (§9.10)**
- `extractor.extract_and_store` returns early with no record when the probe fails or duration exceeds 60 s. Record a terminal failure status with a user-visible reason in both cases.
- Fix the frame-key collision bug (§10.4): when a frame fails to store, `frame_count` is not incremented, so the **next frame reuses the same S3 key and frame number**, silently overwriting data. This is a data-integrity defect.
- Add a bound to the JPEG marker-scan buffer (`extractor.py:76-103`) to prevent unbounded growth.

**4.4 — Replace mocked status data (§9.6)**
- `/submissions` derives `status` from `hash(video_id) % 2`, assigns a random agent, and returns the literal `"12:32:01 LOCAL"` as every timestamp. Note `hash()` is salted per process, so a video's status **changes on every restart**.
- Rewrite it to read the real job records from 4.2.
- Fix the hardcoded `http://localhost:8000` URL (`app.py:177`) to use the `API_URL` env var.

**4.5 — Unify the bounding-box contract (§4.5)**
- Three conflicting schemas exist: `prompts.py:16` (array), `audit_manager.py:39` (`box_2d`), and `SKILL.md` (object + `timestamp_ms`).
- Choose one — the array form in `prompts.py` is what the frontend already consumes correctly — document it, validate model output against it with Pydantic, and correct `SKILL.md`.

**4.6 — Harden the frontend overlay (§9.12)**
- Guard against missing `confidence` and missing/short `bounding_box` (`HUDView.tsx:125-132`) — a malformed response currently renders `NaN%` or crashes the overlay.
- Replace `alert("Sync Error")` (line 208) with inline error UI; browser modals are to be avoided and it discards the real error.
- Remove the remote MDN sample-video fallback (line 107).
- Enforce the advertised "Max 60MB / 60S" (line 175) client-side — `video.duration` is loaded but never read.
- Fix or remove the `novaflow_submissions` localStorage read (lines 38-45) that nothing ever writes.

### Expected files
`frontend/app/components/HUDView.tsx`, `backend/app.py`, `backend/extractor.py`, `backend/prompts.py`, `.agent/skills/video-audit/SKILL.md`, `backend/tests/test_pipeline.py`.

### Dependencies
Phases 2, 3.

### Required tests
- Upload → analyze → job record transitions through the expected statuses (AWS stubbed).
- An over-length video produces a terminal failure status with a reason, not silence.
- A frame storage failure does **not** cause key reuse.
- `/submissions` returns real job data; no `hash()`-derived field remains.
- Frontend: overlay renders nothing (and does not throw) for a malformed detection.

### Acceptance criteria
- [ ] **One manual end-to-end run**, with credentials, produces a persisted detection visible in the dashboard. Evidence: job record, DynamoDB item, and a screenshot.
- [ ] `grep -n "hash(" backend/app.py` returns nothing in a status path.
- [ ] No pipeline exit path leaves a job without a terminal status.
- [ ] One documented bounding-box schema; `SKILL.md` matches the code.
- [ ] `pytest` passes, output quoted.

### Verification commands
```bash
pytest -v backend/tests/test_pipeline.py
grep -rn "hash(\|12:32:01\|localhost:8000" backend/app.py
cd frontend && npx tsc --noEmit && npm run lint
```

### Human intervention
**Required** — AWS credentials, Bedrock model access for `amazon.nova-pro-v1:0`, the DynamoDB tables, the S3 bucket, a MongoDB instance, and **FFmpeg installed locally** (absent on this host, §8.1). This is the first phase that cannot be fully verified without them.

### Checkpoint
`feat: connect end-to-end detection pipeline with real job tracking`

---

## Phase 5 — Repository cleanup and configuration consistency

### Objective
Remove dead code and reconcile contradictory configuration, so the repository reflects one coherent architecture.

### Why after Phase 4
Deleting code before the pipeline works risks removing something load-bearing. With Phase 4 green, "unreachable" is demonstrable rather than assumed.

### Scope
Deletions and configuration only. **No behavior changes to working code.**

### Tasks
1. **Delete orphaned code (§2.3), each justified in the commit message:** `frontend_backup/` (375 KB, zero references); `frontend/Video Compliance Dashboard/` (contains only a stray 196 KB `package-lock.json`); the duplicate `/agents` and `/logs` routes; `backend/check_count.py`; `backend/debug_probe.py`; root `prompts.py` and root `test_frame.jpg` (duplicates that diverge from the backend copies).
2. **Resolve the MongoDB/GridFS pipeline** (§4.4) per the Phase 0 decision. It is structurally unrunnable — nothing writes `gridfs_id` — so deletion of `audit_manager.py`, `verifier.py`, and `check_count.py` is recommended.
3. **Consolidate env vars (§7.3):** `MONGO_URI` vs `MONGODB_URL` are two names for one thing; `verifier.py:7` hardcodes the connection string. Pick one name.
4. **Fix the S3 bucket mismatch (§10.3):** `infra/` provisions `brand-guardian-raw`/`-final` while all code uses `novaflow-media`. Align them.
5. **Fix the IAM policy (§10.3):** add the **entirely missing DynamoDB permissions**, scope `bedrock:InvokeModel` from `Resource: '*'` to the specific model ARNs, remove unused Step Functions scope, and correct the bucket ARNs.
6. **Add IaC for the DynamoDB tables** (§8.6) — `NovaFlowFrames` and `NovaFlowProjects` currently exist only implicitly in application code.
7. **Harden bucket creation (§10.3):** `infra/init_s3.py` creates buckets with no public-access block, no encryption, no versioning.
8. **Remove the unused `stepfunctions` client** and the never-imported helpers in `aws_config.py`; remove `backend/safety.py` or wire it (it is never imported).
9. **Decide the `/projects` workflow's fate (§9.11):** `CLIENT_REVIEW` is never assigned, so the review step is unreachable by design. Either complete the state machine or remove the routes.
10. **Remove the dead presigned-upload path (§7.4):** `projects.py:132` writes to an S3 key nothing ever reads.

### Expected files
Deletions as listed; `infra/iam_policy.yaml`, `infra/init_s3.py`, new `infra/dynamodb.yaml`, `backend/db.py`, `backend/aws_config.py`.

### Dependencies
Phase 4 accepted (proves what is actually in use).

### Required tests
Full existing suite must still pass — this phase is regression-sensitive by nature. Add no new tests for deletions; **do** add a test that the app imports cleanly after each removal.

### Acceptance criteria
- [ ] `pytest` passes with zero new failures.
- [ ] `cd frontend && npm run build` succeeds after the frontend deletions.
- [ ] One env var name per concept; no hardcoded connection strings (`grep -rn "mongodb://" backend/` → only `.env.example`).
- [ ] IAM policy covers DynamoDB and references the buckets the code actually uses.
- [ ] Every deletion is justified in the commit message with its audit §reference.

### Verification commands
```bash
pytest -v
cd frontend && npm run build && npx tsc --noEmit
grep -rn "mongodb://\|brand-guardian\|MONGO_URI" backend/ infra/
git diff --stat HEAD~1
```

### Human intervention
Decisions on audit §13 questions 1, 2, 7, 8, 9.

### Checkpoint
`refactor: remove dead code and reconcile configuration`

---

## Phase 6 — Backend reliability

### Objective
Make the pipeline survive restarts, failures, and large inputs.

### Scope
Job orchestration, timeouts, retries, cleanup. `backend/app.py`, `backend/extractor.py`, `backend/analyzer.py`.

### Tasks
1. **Move the pipeline off `BackgroundTasks` (§10.4).** It currently runs FFmpeg + Bedrock inside the web worker (`app.py:93,115`), so a restart or deploy loses the job with no record. Also, `run_pipeline` is **sync** (`app.py:108`), so it occupies a threadpool slot for the whole job and will stall the service under concurrency. Use a durable queue or a separate worker process — but **do not introduce a new infrastructure component without approval** (audit §13 question 9).
2. **Add explicit timeouts and retries** to every boto3 client via `botocore.config.Config(connect_timeout, read_timeout, retries)` — there are currently none anywhere, and a hung Bedrock call blocks a worker indefinitely.
3. **Add backoff for Bedrock/DynamoDB throttling** — the per-frame loop currently drops frames on throttle with only a printed message.
4. **Fix the loose video-id match** at `app.py:98` (`startswith` + `files[0]` picks the wrong file on a prefix collision).
5. **Add cleanup**: uploaded files and `temp_frames_*/` (the `shutil.rmtree` at `video_stitcher.py:80` is commented out).
6. **Replace the full-table scans** in request paths (`analyzer.py:109`, `projects.py:75`) with a GSI on `status` — both are acknowledged POC shortcuts whose cost grows with table size.
7. **Add a real `/health`** that checks MongoDB, S3, DynamoDB, and Bedrock reachability. `/status` currently returns a static literal and reports "online" while every dependency is down.
8. **Add structured logging with correlation ids** so a single job can be traced.
9. **Bound resource use for large videos** — the extraction buffer, concurrent frame count, and total frames per job.

### Dependencies
Phase 4.

### Required tests
- A job interrupted mid-pipeline is recoverable or visibly failed — never stuck.
- A throttled Bedrock call retries then fails cleanly with a terminal status.
- `/health` returns unhealthy when a dependency is unreachable.
- Temp directories are removed after a run.
- Re-running `/analyze` for the same `video_id` does not duplicate work.

### Acceptance criteria
- [ ] No AWS client is constructed without an explicit timeout config.
- [ ] No job can be left in a non-terminal state after process death.
- [ ] `/health` reflects real dependency status (demonstrated by stopping a dependency).
- [ ] No `Scan` remains in a request-serving path.
- [ ] `pytest` passes, output quoted.

### Checkpoint
`fix(reliability): add durable job handling, timeouts, retries, and cleanup`

---

## Phase 7 — Frontend completion

### Objective
Replace every hardcoded view with real data, and make role separation genuine.

### Scope
`frontend/app/components/*`, `frontend/app/page.tsx`. UI only.

### Tasks
1. **Replace hardcoded data (§9.12)** in `AgentsView.tsx:20,25` and `LogsView.tsx:19` with real API data — or remove the views if no real data source exists. **Do not leave fictional data in a release.**
2. **Build a real compliance-log view** from the job records and audit history (Phase 4/9).
3. **Wire `ProjectDetailView`** to the `/projects` API, or remove it per the Phase 5 decision.
4. **Make role separation real** — it is currently cosmetic nav filtering (`page.tsx:86`) over a `localStorage` role; pair it with the Phase 3 server-side enforcement.
5. **Remove the decorative fake telemetry** (`HUDView.tsx:46-51`, "4096 clusters synced").
6. **Add real loading, empty, and error states** throughout.
7. **De-duplicate `cn()`** (`HUDView.tsx:20` duplicates `app/lib/utils.ts:4`).
8. **Fix the lint script** — `"lint": "eslint"` with no target lints nothing useful. Add a `typecheck` script (`tsc --noEmit`); none exists despite `strict: true`.
9. **Replace `any` types** in `HUDView.tsx` with real interfaces matching the Phase 4.5 schema.
10. **Accessibility pass** — keyboard navigation, focus states, labels, contrast.

### Dependencies
Phases 3, 4.

### Required tests
Component tests (Vitest + React Testing Library — the project has **no frontend test framework at all** today, so this phase establishes it): auth redirect, role-based nav, overlay rendering for valid and malformed detections, error and empty states.

### Acceptance criteria
- [ ] `grep -rn "const stats = \[\|const agents = \[\|const incidents = \[" frontend/app/` returns nothing.
- [ ] No fictional or placeholder data in any released view.
- [ ] `npm run lint`, `npm run typecheck`, and `npm run build` all pass with zero errors.
- [ ] `npm test` passes.
- [ ] Screenshots of each view with real data.

### Verification commands
```bash
cd frontend && npm run lint && npm run typecheck && npm run build && npm test
grep -rn "const stats = \[\|const incidents = \[" app/
```

### Checkpoint
`feat(frontend): replace mock views with real data and real role enforcement`

---

## Phase 8 — Frame remediation *(scope decision required)*

### Objective
Implement **genuine** AI frame remediation, or formally remove the feature.

### Blocking decision
Audit §13 question 4: is Nova Canvas remediation in release scope? Phase 2 removed the disguised mock; this phase either builds the real thing or confirms the removal is final. **Do not re-add a mock.**

### Scope (if in scope)
Real Nova Canvas inpainting with validation, confidence gating, and human review.

### Tasks
1. Implement a correct Nova Canvas `INPAINTING` call — the previous attempt used `.decode('latin1')` (`audit_manager.py:107`), which is not valid Bedrock image input. Verify the payload schema against current Bedrock documentation; do not guess.
2. **Validate model-supplied coordinates before use (§10.5)** — length, type, range, and ordering. The prior code destructured `box_2d` with no checks, and the prompt itself instructs the model to return `null`, which would raise.
3. **Gate on confidence (§10.5)** — `prompts.py:17` requests a confidence value and `docs/planning.md:24` specifies a 0.8 threshold, but **no code reads it**. Implement the threshold.
4. **Add a human-in-the-loop queue** for low-confidence or failed remediations (planned, entirely absent).
5. **Preserve originals** with a provenance record of what changed, and a rollback path.
6. Add a per-violation remediation API route and the corresponding UI control (audit §5 stage 8: no trigger exists today).
7. Store remediated frames alongside originals with a distinct key, so Phase 9 can consume them.

### Dependencies
Phases 4, 6, and the scope decision.

### Required tests
- Malformed, inverted, out-of-range, and `null` bounding boxes are rejected without raising.
- A below-threshold confidence routes to human review instead of auto-remediating.
- A remediated frame differs byte-wise from the original (**the direct regression test for §9.3**).
- The original is always retrievable after remediation.

### Acceptance criteria
- [ ] A test asserts the remediated output **differs** from the input.
- [ ] No code path reports remediation success without a verified edit.
- [ ] Coordinate validation rejects every malformed case in the test matrix.
- [ ] One manual end-to-end remediation with before/after images as evidence.
- [ ] If descoped: the feature is absent from code, UI, and docs — with no stubs left behind.

### Human intervention
Bedrock model access for `amazon.nova-canvas-v1:0`; the scope decision.

### Checkpoint
`feat: implement verified frame remediation` *or* `docs: descope remediation for v1`

---

## Phase 9 — Reconstruction, verification, and audit history *(scope decision required)*

### Objective
Produce a faithful corrected video, verify it, and persist a real audit trail.

### Blocking decision
Audit §13 question 3: is reconstruction in release scope, or is detection + reporting sufficient for v1?

### Tasks (if in scope)
1. **Fix reconstruction to use remediated frames (§9.4)** — `video_stitcher.py:50` currently stitches the **original** `s3_key`, so the output is the uncorrected video.
2. **Preserve the source framerate and audio.** The output is hardcoded to 1 fps (line 63) and audio is silently discarded — so the current output is not a faithful rendition of the input. This likely requires revisiting the 1 fps extraction strategy: analysing at 1 fps while reconstructing at source rate needs a frame-mapping design.
3. **Abort on missing frames.** Download failures are logged but do not stop the stitch (lines 56-57), so FFmpeg's `frame_%04d` glob silently truncates at the first gap.
4. Use the per-frame `s3_bucket` attribute rather than the module-level env default (line 55).
5. Expose reconstruction via an API route — it is CLI-only today.
6. **Implement the "Critic" verification (§9.5)** — re-audit the remediated output with Nova Pro and confirm violations are gone without new artefacts. This does not exist in any form; `verifier.py` is a GridFS dev utility, not a verifier.
7. **Build a real audit history (§9.6)** — immutable records of detections, remediations, verifications, and reviewer decisions, with evidence frames and timestamps.
8. Add an export/report capability if required.

### Dependencies
Phases 4, 6, 8.

### Required tests
- Reconstruction consumes remediated frames when present (**the regression test for §9.4**).
- A missing frame aborts with a clear error rather than silently truncating.
- Output duration and framerate match the source within tolerance.
- The Critic correctly flags a still-violating frame.
- Audit records are append-only.

### Acceptance criteria
- [ ] Output video verifiably contains the remediated frames (frame-hash comparison).
- [ ] Output duration/framerate match the input.
- [ ] The Critic rejects a deliberately unremediated frame in a test.
- [ ] Audit history is queryable and reflects real events.
- [ ] One manual end-to-end run with the output video as evidence.

### Checkpoint
`feat: add verified reconstruction and audit history`

---

## Phase 10 — Testing, CI, documentation, and release

### Objective
Reach a professional, releasable state with honest documentation and automated verification.

### Tasks

**10.1 — Test coverage**
- Raise coverage across backend and frontend; agree a target with the maintainer. The project started this roadmap with **zero tests** (§8.2).
- Add integration tests for the full pipeline with AWS stubbed.
- Add at least one end-to-end test (Playwright) covering login → upload → analyze → review.
- Ensure **no test calls real AWS or incurs cost**. Mark any test requiring credentials as explicitly skipped by default, and say so in the report.

**10.2 — CI**
- Add `.github/workflows/` — none exists (§8.2). Run: backend lint + `pytest`, frontend lint + typecheck + build + tests, a secret scan, and dependency audit (`pip-audit`, `npm audit`).
- CI must fail the build on any of these.

**10.3 — Documentation**
- **Rewrite `README.md` from verified facts.** The current one is materially inaccurate (§11.1): it claims LLaVA/Ollama (absent), Nova Act (mocked), MongoDB GridFS (orphaned), Next.js 14 (actually 16), and publishes test credentials for users that do not exist.
- Add real setup instructions: prerequisites, dependency install, env vars, AWS setup, FFmpeg, how to run both services.
- **Remove the test-credentials table** (§10.2 finding 9).
- Retitle `docs/planning.md` as an explicit design *target*, noting which parts are unimplemented.
- Correct `SKILL.md`'s bounding-box schema (§11.2) to match the Phase 4.5 decision.
- Add an architecture diagram reflecting the **verified** architecture.
- Add `CONTRIBUTING.md` and confirm `LICENSE`.

**10.4 — Release**
- Complete `docs/RELEASE_CHECKLIST.md` with evidence for every item.
- Confirm no secrets in code or history; confirm `.gitignore` coverage.
- Verify the public S3 bucket question from audit §13 item 4.
- Tag a release.

### Dependencies
All prior phases.

### Acceptance criteria
- [ ] CI green on a clean checkout.
- [ ] Every README claim is verifiable against the code.
- [ ] `docs/RELEASE_CHECKLIST.md` fully checked with evidence.
- [ ] No fictional data, no mocks presented as real output, no disabled code paths claiming success.
- [ ] A fresh clone can be set up by following the README alone (**verify by doing it**).

### Verification commands
```bash
pytest --cov=backend -v
cd frontend && npm run lint && npm run typecheck && npm run build && npm test
gh workflow list && gh run list --limit 5
git log --oneline | head -20
```

### Checkpoint
`docs: rewrite documentation from verified architecture` + `ci: add test and build pipeline` + release tag.

---

# PART II — OPTIONAL ENHANCEMENTS

**Do not start any of these before Phase 10 is accepted.** Each is a genuine improvement, none is required for a reliable release.

## Phase 11 — Orchestration at scale
Step Functions state machine per `docs/planning.md` (currently only an unused boto3 client exists); wire `s3_trigger.py` to actually trigger analysis (its downstream call is a comment); parallel frame processing; SQS/DLQ.

## Phase 12 — Observability and cost control
CloudWatch metrics and alarms; per-job Bedrock cost tracking; tracing; a Bedrock usage budget guard.

## Phase 13 — Product features
Configurable per-client brand policies (currently one hardcoded prompt); wire `safety.py`'s Bedrock Guardrails integration; the Nova Act browser automation behind `/execute`, if ever in scope; richer reporting and client sharing.

---

## Dependency graph

```
Phase 0 (Git safety) ──► Phase 1 (audit, commit) ──► Phase 2 (critical defects)
                                                          │
                                                          ▼
                                                   Phase 3 (security)
                                                          │
                                                          ▼
                                                   Phase 4 (e2e pipeline)  ◄── needs AWS + FFmpeg
                                                          │
                                        ┌─────────────────┼─────────────────┐
                                        ▼                 ▼                 ▼
                                 Phase 5 (cleanup)  Phase 6 (reliability)  Phase 7 (frontend)
                                        └─────────────────┼─────────────────┘
                                                          ▼
                                                   Phase 8 (remediation)*
                                                          │
                                                          ▼
                                                   Phase 9 (reconstruction)*
                                                          │
                                                          ▼
                                                   Phase 10 (tests, CI, docs, release)
                                                          │
                                                          ▼
                                              Phases 11-13 (optional)

* scope decision required before starting
```

## Phase status

| Phase | Title | Status |
| :-- | :-- | :-- |
| 0 | Git safety and repository root | **BLOCKED** — 3 decisions needed |
| 1 | Audit and roadmap | Complete, awaiting commit |
| 2 | Critical defect fixes | Not started |
| 3 | Security hardening | Not started |
| 4 | Connect the end-to-end pipeline | Not started |
| 5 | Repository cleanup and config consistency | Not started |
| 6 | Backend reliability | Not started |
| 7 | Frontend completion | Not started |
| 8 | Frame remediation | Not started — scope TBD |
| 9 | Reconstruction, verification, audit history | Not started — scope TBD |
| 10 | Testing, CI, documentation, release | Not started |
| 11-13 | Optional enhancements | Not started |
