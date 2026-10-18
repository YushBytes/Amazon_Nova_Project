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
| 1.4 | ☐ Upload creates a durable job/audit record | 4 | |
| 1.5 | ☐ Frames are extracted and stored, with a verified count | 4 | |
| 1.6 | ☐ Frames are analysed by Amazon Nova Pro and **detections are persisted** *(currently impossible — audit §9.1)* | 2, 4 | |
| 1.7 | ☐ Violations appear in the dashboard with correct bounding boxes, timestamps, and confidence | 4 | |
| 1.8 | ☐ Job status shown in the UI reflects reality, including failures | 4 | |
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
