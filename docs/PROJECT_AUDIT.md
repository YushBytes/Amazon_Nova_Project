# NovaFlow / Brand Guardian — Codebase Audit

**Audit date:** 2026-10-18
**Audit scope:** Full repository, read-only. No application code was modified.
**Auditor:** Claude Code (Phase 1)
**Audited tree:** `Amazon Nova/Amazon_Nova-main/` (~3,755 lines of first-party source)

### How to read this document

Every finding is tagged with its verification level:

| Tag | Meaning |
| :-- | :-- |
| **[VERIFIED]** | Proven by command output, AST analysis, or byte comparison during this audit. |
| **[INFERRED]** | Strongly implied by reading the source, but not executed (deps/credentials unavailable). |
| **[OPEN]** | Unresolved question requiring a decision or information from the maintainer. |

No code was executed that calls AWS, MongoDB, or any paid service. No dependency was installed. No Git state was modified.

---

## 1. Project purpose and current capabilities

### Intended purpose

Per `README.md` and `docs/planning.md`, NovaFlow / Brand Guardian is intended to be an AI-powered video brand-compliance platform:

1. A video reel is uploaded.
2. Amazon Nova Pro (the "Planner") analyses frames for brand-safety violations and returns bounding boxes.
3. Amazon Nova Canvas (the "Remediator") inpaints the offending regions.
4. Amazon Nova Pro (the "Critic") verifies the remediation.
5. The video is reconstructed and delivered, with a human-in-the-loop step when confidence is low.
6. Clients and agents review results through a role-separated Next.js dashboard.

### Actual current capabilities **[VERIFIED]**

What genuinely works today is considerably narrower:

| Capability | State |
| :-- | :-- |
| Next.js dashboard renders, with role-based nav filtering | Works (client-side only) |
| Login / signup against MongoDB with JWT issuance | Implemented, unverified (needs MongoDB) |
| Video file upload to local disk via `POST /upload-reel` | Implemented, unverified |
| Frame extraction to S3 + DynamoDB via FFmpeg | Implemented, unverified; **not reachable from the UI** |
| Single-frame Nova Pro analysis as a CLI script | Implemented, unverified (`analyzer_nova.py`) |
| Frame analysis writing results to DynamoDB | **Broken** — guaranteed runtime `NameError` (§9.1) |
| Violation review in the dashboard | Wired, but always receives an empty list (§3) |
| AI remediation of a frame | **Mock** — returns the input unchanged (§9.3) |
| Post-remediation verification ("Critic") | **Missing entirely** |
| Corrected-video reconstruction | Partially implemented, uses the *wrong* frames (§9.4) |
| Project marketplace / client review workflow | **Broken** — every endpoint 500s (§9.2) |
| Automated tests | **None exist** |
| CI | **None exists** |

**Bottom line:** the repository is a hackathon-stage prototype. The individual building blocks of the pipeline exist as separate scripts, but they are not connected into a working end-to-end flow, and three separate defects guarantee that no violation is ever persisted, surfaced, or remediated.

---

## 2. Repository structure and entry points

### 2.1 Directory layout **[VERIFIED]**

```
Amazon Nova/                        <- current shell working directory
└── Amazon_Nova-main/               <- de facto project root (holds .gitignore)
    ├── README.md                   Inaccurate; see §11.1
    ├── analyzer_nova.py            Standalone Nova Pro single-frame CLI
    ├── prompts.py                  Prose-format prompt (diverges from backend/prompts.py)
    ├── test_frame.jpg              Test fixture (differs from backend/test_frame.jpg)
    ├── .gitignore
    ├── .claude/settings.local.json Permissive local tool allowlist
    ├── .agent/skills/video-audit/SKILL.md   Coordinate-handling notes; schema conflicts (§11.2)
    ├── docs/planning.md            Target architecture (largely unimplemented)
    ├── backend/                    FastAPI service — 17 Python modules
    ├── frontend/                   Next.js 16 App Router dashboard (live)
    ├── frontend_backup/            375 KB orphaned Vite/React dashboard (§9.8)
    └── infra/                      CloudFormation IAM role + S3 bootstrap script
```

### 2.2 Entry points **[VERIFIED]**

| Entry point | File | Notes |
| :-- | :-- | :-- |
| Backend HTTP server | `backend/app.py:184` | `uvicorn app:app --host 0.0.0.0 --port 8000` |
| Frontend dev server | `frontend/package.json:6` | `next dev`, port 3000 |
| Frontend production build | `frontend/package.json:7` | `next build` |
| Container image | `backend/Dockerfile:26` | Installs FFmpeg; backend only, no compose file |
| Frame extraction CLI | `backend/extractor.py:137` | `python extractor.py <video> [video_id]` |
| Frame analysis CLI | `backend/analyzer.py:165` | `python analyzer.py <image.jpg \| video_id>` |
| Single-frame Nova CLI | `analyzer_nova.py:44` | Hardcoded to `test_frame.jpg` |
| Video reconstruction CLI | `backend/video_stitcher.py:83` | `python video_stitcher.py <video_id> [out.mp4]` |
| MongoDB audit pipeline CLI | `backend/audit_manager.py:187` | Parallel, unreferenced pipeline (§4.4) |
| GridFS frame dump CLI | `backend/verifier.py:40` | Dev utility, not a verifier (§9.5) |
| S3 event Lambda | `backend/s3_trigger.py:7` | Logs only; no downstream trigger |
| IAM provisioning | `infra/iam_policy.yaml` | Not deployed; incomplete (§10.4) |
| S3 bucket bootstrap | `infra/init_s3.py:28` | Creates buckets the app never reads (§10.3) |

### 2.3 Dead, orphaned, and duplicated files **[VERIFIED]**

| Path | Finding |
| :-- | :-- |
| `frontend_backup/` (375 KB, 70+ files) | Entire alternate Vite dashboard. Zero references from `frontend/`. Contains `src/app/data/mockData.ts`. |
| `frontend/Video Compliance Dashboard/` | Contains **only** an orphaned 196 KB `package-lock.json` — no `package.json`, no source. Pure junk. |
| `prompts.py` vs `backend/prompts.py` | Two divergent system prompts. Root = prose format; backend = JSON schema. Only the backend one is imported by `analyzer.py`. |
| `test_frame.jpg` vs `backend/test_frame.jpg` | Different files (MD5 `12af2d9d…` vs `c0e2ffe0…`). |
| `frontend/app/agents/page.tsx` | Duplicates `components/AgentsView.tsx` as a separate route, with its own copy of the hardcoded data. |
| `frontend/app/logs/page.tsx` | Duplicates `components/LogsView.tsx` the same way. |
| `backend/check_count.py` | 8-line throwaway debug script, hardcoded to `video_id="test_8k_v2"`. |
| `backend/debug_probe.py` | FFmpeg probe debug script. |
| `backend/analyzer.py:28-51` | Dead `body` dict built in Anthropic Claude message format, constructed then never used; superseded by `nova_payload` on line 63. |
| `backend/safety.py` | `NovaFlowSafety` class is never imported or instantiated anywhere. |
| `backend/aws_config.py` | `get_nova_video_input`, `parse_s3_uri`, and the module-level `stepfunctions` client are never imported anywhere. |

---

## 3. Actual technology stack **[VERIFIED]**

### Confirmed in use

| Layer | Technology | Evidence |
| :-- | :-- | :-- |
| Frontend framework | Next.js 16.1.6, App Router | `frontend/package.json:15` |
| UI runtime | React 19.2.3 | `frontend/package.json:16` |
| Styling | Tailwind CSS v4 (`@tailwindcss/postcss`) | `frontend/package.json:21,27` |
| Animation | Framer Motion 12 | `frontend/package.json:13` |
| Icons | lucide-react | `frontend/package.json:14` |
| Language | TypeScript 5.9.3, `strict: true` | `frontend/tsconfig.json:6` |
| Backend framework | FastAPI + Uvicorn | `backend/requirements.txt:1-2` |
| Auth tokens | JWT via `python-jose` HS256 | `backend/auth.py:8,16` |
| User store | MongoDB via `motor` (async) | `backend/db.py:2` |
| Frame metadata | **DynamoDB** via `boto3` | `backend/extractor.py:22` |
| Frame blobs | **Amazon S3** via `boto3` | `backend/extractor.py:21` |
| AI inference | Amazon Bedrock `bedrock-runtime` | `backend/analyzer.py:18` |
| AI model | `amazon.nova-pro-v1:0` | `backend/analyzer.py:25` |
| Video processing | `ffmpeg-python` + FFmpeg binary | `backend/extractor.py:3` |
| Image masking | Pillow (`PIL`) | `backend/audit_manager.py:7` |
| Frontend hosting | AWS Amplify | `frontend/amplify.yml` |
| Container | Docker, `python:3.11-slim` | `backend/Dockerfile:2` |

### Declared in docs but **NOT implemented** **[VERIFIED]**

| Claim | Source | Reality |
| :-- | :-- | :-- |
| **Ollama / LLaVA** multi-modal analysis | `README.md:3,29` | Zero matches for `ollama` anywhere in the codebase. The only `llava` matches are the cosmetic display string `"LLAVA-01"` in an agent-name array (`backend/app.py:165`) and a similar label in `ClientPortal.tsx`. **No LLaVA integration exists.** |
| **MongoDB (GridFS)** as the primary store | `README.md:29` | GridFS appears only in `backend/audit_manager.py` and `backend/verifier.py`, neither of which is reachable from the running API. The live pipeline uses S3 + DynamoDB. |
| **Nova Act** browser automation | `README.md:3` | `backend/automation.py` imports `nova_act` with a silent mock fallback; the package is not in `requirements.txt` and is not installed. |
| **AWS Step Functions** state machine | `docs/planning.md:28` | A `stepfunctions` boto3 client is created in `aws_config.py:30` and never used. No state machine definition exists. |
| **Nova Canvas** remediation | `docs/planning.md:20` | The `invoke_model` call is **commented out** (`audit_manager.py:116`). See §9.3. |
| **Planner-Critic** verification loop | `docs/planning.md:12` | No Critic implementation exists anywhere. |
| **Next.js 14** | `README.md:28` | Actually Next.js 16.1.6. |

### Missing from `backend/requirements.txt` **[VERIFIED]**

`requirements.txt` declares only 8 packages. The following are imported by the code but **not declared**, so a clean `pip install -r requirements.txt` followed by `uvicorn app:app` will fail on import:

| Missing package | Imported at | Consequence |
| :-- | :-- | :-- |
| `boto3` | `aws_config.py:2`, `extractor.py:4`, `analyzer.py:2`, `projects.py:3`, `audit_manager.py:3`, `video_stitcher.py:3`, `safety.py:2`, `automation.py:2` | **Backend cannot start** — `projects.py` is imported by `app.py:19`. |
| `ffmpeg-python` | `extractor.py:3`, `debug_probe.py:1` | Frame extraction impossible. |
| `Pillow` | `audit_manager.py:7` | Mask generation impossible. |
| `python-multipart` | required by FastAPI for `UploadFile` | `POST /upload-reel` cannot parse uploads. |
| `gridfs` / `bson` | `audit_manager.py:5`, `verifier.py:3-4` | (Provided transitively by `pymongo`.) |
| `nova-act` | `automation.py:4` | Silently degrades to a mock. |

Additionally, **no dependency is version-pinned** in `requirements.txt` — all 8 entries are bare package names, so builds are not reproducible.

---

## 4. Current architecture and data flow

### 4.1 Verified request topology **[VERIFIED]**

```
Browser (Next.js :3000)
  │
  ├─ POST /auth/login ──────────► FastAPI sub-app (auth.py)  ──► MongoDB "novaflow".users
  │                                                               (JWT HS256, hardcoded secret)
  ├─ POST /upload-reel ─────────► app.py:69  ──► local disk ./uploads/<video_id>.<ext>
  │                                              (no auth, no validation — §10.1)
  ├─ GET  /submissions ─────────► app.py:157 ──► os.listdir() + MOCKED metadata
  │                                              (status derived from hash() — §9.6)
  └─ GET  /violations/{id} ─────► app.py:123 ──► DynamoDB "NovaFlowFrames"
       (polled every 3 s)                        (always empty in practice — §9.1)

  ✗ POST /analyze/{video_id} ───  NEVER CALLED BY THE FRONTEND  (§5, §9.7)
  ✗ /projects/*  (6 routes) ────  NEVER CALLED BY THE FRONTEND; all 500 (§9.2)
  ✗ POST /execute ──────────────  NEVER CALLED BY THE FRONTEND; returns mock text (§9.9)
```

### 4.2 Intended background pipeline (code exists, never triggered from UI) **[INFERRED]**

```
POST /analyze/{id}  (app.py:92)
  └─ BackgroundTasks → run_pipeline()          app.py:108
       ├─ extractor.extract_and_store()        extractor.py:43
       │    ├─ ffmpeg.probe → reject if >60 s  extractor.py:51  (silent drop — §9.10)
       │    ├─ FFmpeg → MJPEG @ 1 fps, split on SOI/EOI markers
       │    ├─ S3 PUT   frames/{video_id}/frame_NNNN.jpg
       │    └─ DynamoDB PUT {video_id, frame_number, status:"pending_audit"}
       └─ analyzer.check_pending_frames()      analyzer.py:99
            ├─ DynamoDB Scan status=="pending_audit"
            ├─ S3 GET frame bytes
            ├─ Bedrock invoke_model amazon.nova-pro-v1:0
            └─ DynamoDB UPDATE status="audited", detections=[...]
                 ▲ UNREACHABLE: NameError on json.dumps kills every iteration (§9.1)
```

### 4.3 Storage topology **[VERIFIED]**

| Store | Purpose | Keys / collections | Used by live API? |
| :-- | :-- | :-- | :-- |
| MongoDB `novaflow` | User accounts | `users` collection | Yes — `auth.py` |
| MongoDB `novaflow_video` | Frame metadata + GridFS blobs | `frame_metadata`, GridFS | **No** — orphaned pipeline |
| DynamoDB `NovaFlowFrames` | Frame metadata + detections | PK `video_id`, SK `frame_number` | Yes — `extractor`, `analyzer`, `app.py`, `video_stitcher` |
| DynamoDB `NovaFlowProjects` | Project records | PK `project_id`, SK `sk` | Yes — `projects.py` (but broken) |
| S3 `novaflow-media` | Frame JPEGs + raw reels | `frames/{vid}/frame_NNNN.jpg`, `raw/{pid}/{user}/reel.mp4` | Yes |
| S3 `brand-guardian-raw` / `-final` | Created by `infra/init_s3.py` | — | **No** — never read (§10.3) |
| Local disk `backend/uploads/` | Uploaded videos, served statically | `<video_id>.<ext>` | Yes — `app.py:38` |

**Three storage layers coexist for the same conceptual data** (local disk, S3+DynamoDB, MongoDB+GridFS). This is the single largest architectural inconsistency in the project.

### 4.4 The two parallel, incompatible pipelines **[VERIFIED]**

This is the most important architectural finding.

| | **Pipeline A (live)** | **Pipeline B (orphaned)** |
| :-- | :-- | :-- |
| Files | `extractor.py`, `analyzer.py`, `app.py`, `video_stitcher.py` | `audit_manager.py`, `verifier.py`, `check_count.py` |
| Blob store | S3 | MongoDB GridFS |
| Metadata store | DynamoDB `NovaFlowFrames` | MongoDB `novaflow_video.frame_metadata` |
| Prompt source | `backend/prompts.py` (`SYSTEM_PROMPT`) | Inline prompt, `audit_manager.py:33` |
| Detection schema | `violations: [{category, confidence, bounding_box:[y1,x1,y2,x2], risk_level}]` | `{violation_detected, violation, box_2d:[y1,x1,y2,x2], label}` |
| Remediation | None | `remediate_frame()` — **mocked** (§9.3) |
| Reachable from API? | Yes | **No** — `audit_manager` is imported by nothing |
| Writes GridFS frames? | No | Yes — but **nothing ever populates** `gridfs_id`, because no extractor writes to GridFS |

**Pipeline B is structurally unrunnable even if invoked:** `audit_manager.run_audit_pipeline()` reads `meta["gridfs_id"]` (line 140), but no code in the repository ever writes a `frame_metadata` document or a GridFS blob. The only writer, `extractor.py`, targets S3 + DynamoDB. **[VERIFIED]** — no `fs.put` of raw frames and no `metadata_col.insert` exists anywhere.

**[OPEN]** Pipeline B appears to be an earlier iteration superseded by A. Confirm it can be deleted rather than revived.

### 4.5 Three conflicting bounding-box schemas **[VERIFIED]**

| Source | Schema | Coordinate space |
| :-- | :-- | :-- |
| `backend/prompts.py:16` | `"bounding_box": [ymin, xmin, ymax, xmax]` | normalized 0–1000 |
| `backend/audit_manager.py:39` | `"box_2d": [ymin, xmin, ymax, xmax]` | normalized 0–1000 |
| `.agent/skills/video-audit/SKILL.md` | `"bounding_box": {x, y, width, height}` + `timestamp_ms` | normalized 0–1000 |

The frontend (`HUDView.tsx:125-128`) consumes the **array** form and divides by 10 to get CSS percentages — consistent with `prompts.py` only. The SKILL.md object form would break the overlay (`d.bounding_box[0]` would be `undefined`). The documented contract must be unified before remediation work begins.

---

## 5. Feature-by-feature implementation status

Classification per the Phase 1 brief. "Implemented but not verified" means the code path is plausible and complete on inspection, but could not be executed in this environment (missing deps / AWS credentials / MongoDB).

| # | Workflow stage | Status | Evidence |
| :-- | :-- | :-- | :-- |
| 1 | User uploads a supported video | **Partially implemented** | `app.py:69` accepts the upload, but `accept="video/*"` (`HUDView.tsx:163`) is the *only* type check and it is client-side and trivially bypassable. No server-side MIME/magic-byte check, no size cap, no duration check. UI advertises "Max 60MB / 60S" (`HUDView.tsx:175`) which is **never enforced** on either side. |
| 2 | App validates the file and stores it safely | **Partially implemented** | Stores to `./uploads/`. **Unsafe:** extension is taken from the user-supplied filename (`app.py:75`) and interpolated into the path — path-traversal risk (§10.1). No auth on the endpoint. |
| 3 | Backend creates a processing job / audit record | **Missing** | No job or audit record is created on upload. `app.py:69-90` returns a response and persists nothing. There is no jobs table, no status row, no queue. The `/projects` DynamoDB table is a separate, broken concern. |
| 4 | Video is decoded, frames extracted | **Implemented but not verified** | `extractor.py:43-135`. FFmpeg-based, 1 fps, writes S3 + DynamoDB. Cannot be executed here: `ffmpeg-python` not installed and the `ffmpeg` binary is absent from this host **[VERIFIED]**. |
| 5 | AI analyzes frames against brand policies | **Blocked by defect** | `analyzer.py:99-163` is structurally complete but **every iteration raises `NameError: name 'json' is not defined`** at line 145, swallowed by the `except` on line 162. See §9.1 — AST-proven. The lone working AI path is the standalone CLI `analyzer_nova.py`. |
| 6 | System records violations, evidence frames, timestamps, boxes, confidence | **Blocked by defect** | The `table.update_item` that would persist `detections` is at `analyzer.py:149` — *after* the line that throws. **No detection is ever written to DynamoDB.** Timestamps and S3 frame keys *are* written correctly by the extractor (`extractor.py:116-124`). |
| 7 | Users review detected violations in the frontend | **Implemented but non-functional** | `HUDView.tsx:54-71` polls `/violations/{id}` every 3 s and renders overlays at `HUDView.tsx:117-135`. The rendering logic is correct for the array schema. It always receives `detections: []` as a consequence of §9.1, so the UI shows nothing. |
| 8 | User can request a correction for a selected violation | **Missing** | No UI control, no API route, no handler. Searched the full tree — no remediation trigger exists. |
| 9 | Correction edits the frame via the intended AI service | **Mocked (disguised)** | `audit_manager.remediate_frame()` builds a mask correctly, then the Bedrock call is **commented out** (`audit_manager.py:116`) and the function `return image_bytes` — the *unmodified input* — on line 119. The caller then stores it and sets `remediated: True` (line 178). **The system reports successful remediation while changing nothing.** |
| 10 | App reconstructs / exports the corrected video | **Partially implemented, incorrect** | `video_stitcher.reconstruct_video()` works mechanically, but queries `f["s3_key"]` (line 50) — the **original** frames. It never references remediated output. Output would be the uncorrected video. Also: no API route exposes it, audio is discarded, and source FPS is hardcoded to 1. |
| 11 | System verifies the corrected output | **Missing** | `verifier.py` is a GridFS frame-dump dev utility (writes `test_output.jpg`), not a verifier. The "Critic" from `docs/planning.md:22` does not exist in any form. |
| 12 | App presents accurate status, results, and audit history | **Mocked** | `/submissions` (`app.py:157`) derives `status` from `hash(video_id) % 2`, assigns a random `agent` from a hardcoded pool, and returns the literal string `"12:32:01 LOCAL"` as the timestamp for every row. `AgentsView`, `LogsView`, `/agents`, and `/logs` are entirely hardcoded arrays. **No real audit history exists.** |

### Supporting features

| Feature | Status | Evidence |
| :-- | :-- | :-- |
| Signup / login / JWT issuance | **Implemented but not verified** | `auth.py:88-122`. Needs a running MongoDB. Security defects in §10.2. |
| `GET /auth/me` token introspection | **Implemented but not verified** | `auth.py:125`. Never called by the frontend. |
| Role-based nav filtering (agent/client) | **Implemented and verified by inspection** | `page.tsx:80-86`. Cosmetic only — no server-side enforcement (§10.2). |
| Session persistence | **Implemented** | `AuthContext.tsx:28-42`, `localStorage`. Token is never re-validated against the server. |
| Project marketplace / accept / review / chat | **Blocked by defect** | All 6 `/projects` routes 500 — §9.2, AST-proven. |
| AI chat about a project | **Mocked (disguised)** | `projects.py:206-221`. Returns a hardcoded f-string claiming "the AI detected minor lighting issues". No Bedrock call. Comment on line 216 admits it is a placeholder. |
| Goal decomposition (`/execute`) | **Mocked (disguised)** | §9.9. |
| Bedrock Guardrails safety checks | **Implemented, unwired** | `safety.py`. Never imported. `BEDROCK_GUARDRAIL_ID` would be `None` if called. |
| Nova Act browser automation | **Mocked** | `automation.py:6-15`. Instantiated at `app.py:45` but no route uses it. |
| S3 event → analysis trigger | **Partially implemented** | `s3_trigger.py` logs the S3 URI; the downstream trigger is a comment (line 22). Not deployed. |
| Human-in-the-loop queue | **Missing** | Planned in `docs/planning.md:24`. Only a `HITLQueue.tsx` in the orphaned `frontend_backup/`. |

---

## 6. API and integration inventory **[VERIFIED]**

### 6.1 HTTP routes

| Method | Path | Handler | Auth | Called by UI? | State |
| :-- | :-- | :-- | :-- | :-- | :-- |
| GET | `/status` | `app.py:47` | None | No | Works |
| POST | `/execute` | `app.py:55` | **None** | No | Mock response |
| POST | `/upload-reel` | `app.py:69` | **None** | Yes | Works; unsafe (§10.1) |
| POST | `/analyze/{video_id}` | `app.py:92` | **None** | **No** | Orphaned trigger (§9.7) |
| GET | `/violations/{video_id}` | `app.py:123` | **None** | Yes | Returns empty (§9.1) |
| GET | `/submissions` | `app.py:157` | **None** | Yes | Mocked data |
| GET | `/uploads/*` | `app.py:38` (static mount) | **None** | Yes | Public file serving |
| POST | `/auth/signup` | `auth.py:88` | n/a | Yes | Role self-election (§10.2) |
| POST | `/auth/login` | `auth.py:112` | n/a | Yes | Works (needs MongoDB) |
| GET | `/auth/me` | `auth.py:125` | Bearer | No | Unverified |
| POST | `/projects/` | `projects.py:43` | Bearer | No | **500** (§9.2) |
| GET | `/projects/marketplace` | `projects.py:70` | Bearer | No | **500** (§9.2) |
| PATCH | `/projects/{id}/accept` | `projects.py:83` | Bearer | No | **500** (§9.2) |
| POST | `/projects/{id}/upload-init` | `projects.py:113` | Bearer | No | **500** (§9.2) |
| POST | `/projects/{id}/review` | `projects.py:165` | Bearer | No | **500** (§9.2) |
| POST | `/projects/{id}/chat` | `projects.py:206` | Bearer | No | **500** (§9.2); mock |

**Only 4 of the 16 routes are exercised by the frontend.** 10 of 16 have no authentication at all.

### 6.2 Auth mounting defect **[INFERRED]**

`app.py:42` mounts the auth **FastAPI sub-app** via `app.mount("/auth", auth_app)`. Consequences:

- `auth_app` carries its own `CORSMiddleware` with `allow_origins=["http://localhost:3000"]` (`auth.py:76`), while the parent uses `allow_origins=["*"]` (`app.py:30`) — two conflicting CORS policies on one service.
- `OAuth2PasswordBearer(tokenUrl="/login")` (`auth.py:82`) declares an absolute `/login`, but the mounted route lives at `/auth/login`. OpenAPI docs and any OAuth2 client flow will point at the wrong URL.
- A mounted sub-app does not appear in the parent's `/openapi.json`, so `/auth/*` routes are invisible in `/docs`.

Mounting should be `app.include_router(...)` on an `APIRouter`, consistent with `projects.py`.

### 6.3 External integrations

| Integration | Client created at | Model / resource | Verified? |
| :-- | :-- | :-- | :-- |
| Bedrock Runtime (Nova Pro) | `analyzer.py:18`, `audit_manager.py:18`, `analyzer_nova.py:9` | `amazon.nova-pro-v1:0` | No — no AWS creds used in this audit |
| Bedrock Runtime (Nova Canvas) | `audit_manager.py:77` | `amazon.nova-canvas-v1:0` | **Call commented out** |
| Bedrock Guardrails | `safety.py:9` | `apply_guardrail` | Unwired |
| S3 | `extractor.py:21`, `projects.py:130`, `video_stitcher.py:15` | `novaflow-media` | No |
| DynamoDB | `extractor.py:22`, `projects.py:17`, `app.py:132` | `NovaFlowFrames`, `NovaFlowProjects` | No |
| Step Functions | `aws_config.py:30` | — | Client created, never used |
| MongoDB | `db.py:16`, `audit_manager.py:19`, `verifier.py:11` | `novaflow`, `novaflow_video` | No |

### 6.4 Bedrock payload concerns **[INFERRED]**

These need live validation against the Bedrock API in a later phase; they are flagged, not confirmed, defects:

- `analyzer.py:68` sends `{"image": {"format": "jpeg", "source": {"bytes": <base64 str>}}}` through `invoke_model` with a JSON body. The `source.bytes` field is the **Converse API** convention (which takes raw bytes via the SDK); `invoke_model` with `schemaVersion: messages-v1` is the native Nova format. `analyzer.py` omits `schemaVersion` entirely, while `audit_manager.py:49` includes it. The two call sites disagree. **[OPEN]** Which is correct for the target model version must be confirmed against current Bedrock docs.
- `audit_manager.py:54` passes **raw `bytes`** into `json.dumps(...)` → guaranteed `TypeError: Object of type bytes is not JSON serializable`, caught at line 69. So `audit_frame()` *always* returns `{"error": ...}`. **[VERIFIED by inspection]**
- `audit_manager.py:107-108` uses `.decode('latin1')` to stringify image bytes. This is not base64 and would be rejected by Bedrock.
- `analyzer.py:29` sets `"anthropic_version": "bedrock-2023-05-31"` in the dead `body` dict — a Claude-specific field in a Nova payload, further evidence lines 28-51 are abandoned scaffolding.
- `docs/planning.md` and `audit_manager.py:29` reference "Nova 2 Pro", but every model ID in code is `amazon.nova-pro-v1:0`. **[OPEN]** Confirm the intended model ID.

---

## 7. Database and storage inventory **[VERIFIED]**

### 7.1 DynamoDB `NovaFlowFrames`

Inferred from access patterns (no IaC definition exists for this table):

| Attribute | Type | Written at |
| :-- | :-- | :-- |
| `video_id` | S — **partition key** | `extractor.py:117` |
| `frame_number` | N — **sort key** | `extractor.py:118` |
| `timestamp` | S (`str(frame/FPS)`) | `extractor.py:119` |
| `s3_key`, `s3_bucket` | S | `extractor.py:120-121` |
| `status` | S — `pending_audit` → `audited` | `extractor.py:122`, `analyzer.py:154` |
| `created_at`, `updated_at` | S (ISO) | `extractor.py:123`, `analyzer.py:157` |
| `analysis_report` | S (JSON string) | `analyzer.py:155` — **never reached** |
| `detections` | L | `analyzer.py:156` — **never reached** |

Access patterns: `Query` by `video_id` (`app.py:136`, `video_stitcher.py:28`); `Scan` with `FilterExpression` on `status` (`analyzer.py:109`). The scan is acknowledged as a POC shortcut (`analyzer.py:108`) and will not scale.

**Type inconsistency:** `timestamp` is stored as a string (`extractor.py:119`) and coerced with `float()` on read (`app.py:146`). Workable but fragile; should be a Number.

### 7.2 DynamoDB `NovaFlowProjects`

| Attribute | Type | Notes |
| :-- | :-- | :-- |
| `project_id` | S — **partition key** | `projects.py:50` |
| `sk` | S — **sort key**, always the literal `"metadata"` | `projects.py:51` |
| `title`, `requirements`, `client_id`, `status`, `created_at` | S | |
| `agent_id` | S or **NULL** | Set to Python `None` on create (`projects.py:55`) |
| `feedback`, `post_time`, `reel_s3_key` | S | Conditionally added |

Defects:
- **Table name is hardcoded** (`projects.py:14`), unlike `NovaFlowFrames` which is env-configurable.
- `GET /marketplace` uses a full table `Scan` (line 75), acknowledged on line 72.
- `agent_id: None` writes a DynamoDB NULL. The `ProjectResponse` model types it `Optional[str]`, so round-trips work, but `attribute_exists`-style conditions on it will behave unexpectedly.
- `projects.py:76` references `boto3.dynamodb.conditions.Attr` with only `import boto3` at module scope. `boto3.dynamodb.conditions` is not guaranteed to be bound as an attribute of the `boto3` package by a bare `import boto3`. **[OPEN]** This may raise `AttributeError` even after the §9.2 fix; needs runtime confirmation. Other call sites correctly use `from boto3.dynamodb.conditions import Attr/Key`.
- The status machine is `SUBMITTED → IN_PROGRESS → AI_AUDITING → CLIENT_REVIEW → COMPLETED`, but **nothing ever sets `CLIENT_REVIEW`**. `upload-init` jumps straight to `AI_AUDITING` (line 149), and `POST /review` rejects anything not already in `CLIENT_REVIEW` (line 175). **The review step is unreachable by design.** **[VERIFIED]**

### 7.3 MongoDB

| DB | Collection | Used by | Live? |
| :-- | :-- | :-- | :-- |
| `novaflow` | `users` | `auth.py` | Yes |
| `novaflow_video` | `frame_metadata`, GridFS (`fs.files`/`fs.chunks`) | `audit_manager.py`, `verifier.py`, `check_count.py` | No |

**Env var collision:** the connection string is read as `MONGODB_URL` in `db.py:5` but as `MONGO_URI` in `audit_manager.py:13`. Two names, two databases, two drivers (`motor` async vs `pymongo` sync). `verifier.py:7` hardcodes `mongodb://localhost:27017/` with no env override at all.

No indexes are declared anywhere — notably none on `users.username`, which is queried on every login (`auth.py:117`) and checked for uniqueness on signup (`auth.py:93`) without a unique constraint. **Concurrent signups can create duplicate usernames.** **[INFERRED]**

### 7.4 S3

| Bucket | Key patterns | Written by | Read by |
| :-- | :-- | :-- | :-- |
| `novaflow-media` (env `S3_BUCKET_NAME`) | `frames/{video_id}/frame_NNNN.jpg` | `extractor.py:106` | `analyzer.py:138`, `video_stitcher.py:55` |
| `novaflow-media` | `raw/{project_id}/{username}/reel.mp4` | presigned PUT, `projects.py:132` | **Nothing** |
| `brand-guardian-raw`, `brand-guardian-final` | — | `infra/init_s3.py:29` | **Nothing** |

Two defects:
1. The presigned upload at `projects.py:132` writes to a key that **no code ever reads**. The `/projects` upload path is a dead end — it stores a reel and never analyses it.
2. `infra/init_s3.py` and `infra/iam_policy.yaml` provision `brand-guardian-*` buckets while all application code defaults to `novaflow-media`. The IAM role therefore grants access to buckets the app does not use, and **no access to the bucket it does use**. **[VERIFIED]**

### 7.5 Local filesystem

`backend/uploads/` is created at import time (`app.py:37`, redundantly again at line 67) and mounted at `/uploads` via `StaticFiles` (line 38). Uploaded videos are served publicly with no authentication and no expiry. `UPLOAD_DIR` is a relative path, so its location depends on the process CWD. Not in `.gitignore` (§10.6). No cleanup of any kind.

`video_stitcher.py:43` creates `temp_frames_{video_id}/` in the CWD and the cleanup `shutil.rmtree` is **commented out** (line 80) — unbounded disk growth across runs.

---

## 8. Test and build baseline **[VERIFIED]**

### 8.1 Environment

| Tool | Version | Note |
| :-- | :-- | :-- |
| Python | 3.14.6 | Dockerfile targets 3.11 — **mismatch** |
| Node.js | v24.18.1 | |
| npm | 11.16.0 | |
| Git | 2.51.0.windows.1 | `core.autocrlf=true` |
| Docker | 29.8.0 | |
| GitHub CLI | 2.97.0, authenticated as `YushBytes` (scopes: `gist`, `read:org`, `repo`, `workflow`) | |
| **FFmpeg** | **NOT INSTALLED** | `ffmpeg: command not found` — blocks all video work locally |

### 8.2 Test inventory

**There are zero tests in this repository.** **[VERIFIED]**

- No `tests/` directory, no `test_*.py`, no `*_test.py`, no `*.spec.ts`, no `*.test.tsx`.
- No `pytest.ini`, `conftest.py`, `tox.ini`, `setup.cfg`, or `pyproject.toml`.
- No test runner in `frontend/package.json` — no Jest, Vitest, or Playwright; no `test` script.
- The only paths matching `*test*` are the `test_frame.jpg` image fixtures and an unrelated `aspect-ratio.tsx` in `frontend_backup/`.
- No `.github/` directory — **no CI of any kind**.

### 8.3 Commands actually run during this audit

| Command | Result |
| :-- | :-- |
| `python -m py_compile backend/*.py analyzer_nova.py prompts.py infra/init_s3.py` | **PASS** — all 19 Python files are syntactically valid |
| AST analysis of `backend/analyzer.py` | **Confirmed defect** — `json` unresolvable in `check_pending_frames` (§9.1) |
| AST analysis of `backend/auth.py` + `backend/projects.py` | **Confirmed defect** — async `decode_token` called without `await` from a sync function (§9.2) |
| Dependency presence probe (`importlib.util.find_spec`) | `jose`, `motor`, `pymongo`, `gridfs`, `boto3`, `ffmpeg`, `strands`, `nova_act` **all MISSING** |
| `gh repo view` / `gh api .../git/trees/main` | Remote verified (§12) |
| Byte comparison of remote vs local blobs | Identical (§12) |

### 8.4 Commands that could NOT be run, and why

Each is an **environmental blocker**, not an application defect — except where noted:

| Command | Blocker | Class |
| :-- | :-- | :-- |
| `uvicorn app:app` | `boto3`, `motor`, `jose` not installed. **Also an application defect:** `boto3` is absent from `requirements.txt`, so installing from the manifest would not fix it. | Env + **defect** |
| `npm run build` | `frontend/node_modules` absent; `npm install` was never run. Installing deps is out of scope for a read-only audit. | Env |
| `npm run lint` | Same. | Env |
| `npx tsc --noEmit` | Same. No `typecheck` script is defined either. | Env + gap |
| `pytest` | `pytest` is installed, but there are no tests to collect. | **Gap** |
| `python extractor.py <video>` | `ffmpeg-python` not installed; FFmpeg binary absent. | Env |
| `python analyzer_nova.py` | `boto3` absent; no AWS credentials; would incur Bedrock charges. Deliberately not run. | Env + policy |
| `docker build backend/` | Deliberately not run (large image pull). Would likely **fail at runtime** regardless: `requirements.txt` omits `boto3`, so `COPY . .` + `uvicorn app:app` → `ModuleNotFoundError` on `app.py:19 → projects.py:3`. | Policy + **defect** |

### 8.5 Required environment variables (names only, no values)

| Name | Read at | Default in code | Required? |
| :-- | :-- | :-- | :-- |
| `AWS_REGION` | `extractor.py:16`, `analyzer.py:13`, `projects.py:13`, `audit_manager.py:15`, `safety.py:11`, `video_stitcher.py:12`, `orchestrator.py:28`, `automation.py:24` | `us-east-1` | Effectively yes |
| `S3_BUCKET_NAME` | `extractor.py:14`, `analyzer.py:11`, `projects.py:131`, `video_stitcher.py:10` | `novaflow-media` | Yes |
| `DYNAMODB_TABLE_NAME` | `extractor.py:15`, `analyzer.py:12`, `app.py:131`, `video_stitcher.py:11` | `NovaFlowFrames` | Yes |
| `MONGODB_URL` | `db.py:5` | `mongodb://localhost:27017` | Yes (auth) |
| `MONGODB_DB_NAME` | `db.py:6` | `novaflow` | No |
| `MONGO_URI` | `audit_manager.py:13` | `mongodb://localhost:27017/` | Duplicate of `MONGODB_URL` — consolidate |
| `API_URL` | `app.py:82` | `http://localhost:8000` | Yes in prod |
| `BEDROCK_GUARDRAIL_ID` | `safety.py:13` | **none — `None`** | Only if `safety.py` is wired |
| `BEDROCK_GUARDRAIL_VERSION` | `safety.py:14` | `1` | No |
| `NEXT_PUBLIC_API_URL` | `AuthContext.tsx:26`, `HUDView.tsx:61,191`, `ClientPortal.tsx:41`, `login/page.tsx:24` | `http://localhost:8000` | Yes in prod |

Also required but **never read from the environment** — supplied implicitly via the boto3 default credential chain: AWS credentials/role. And the JWT signing key, which is **hardcoded** rather than env-sourced (§10.2).

**No `.env.example` exists.** A new contributor has no way to discover this list.

### 8.6 Missing cloud prerequisites **[INFERRED]**

For the pipeline to run end to end, none of which could be verified in this audit:

- AWS credentials with Bedrock `InvokeModel` access.
- **Bedrock model access explicitly granted** for `amazon.nova-pro-v1:0` (and `amazon.nova-canvas-v1:0` for remediation) in the target region — Bedrock requires per-model opt-in.
- DynamoDB tables `NovaFlowFrames` and `NovaFlowProjects` created with the key schemas in §7.1–7.2. **No IaC creates these** — `infra/` only covers IAM and S3.
- S3 bucket `novaflow-media`.
- A reachable MongoDB instance.
- FFmpeg on the host or in the container (the Dockerfile does install it).

---

## 9. Bugs and incomplete functionality

Ordered by severity. Each entry names the file, line, mechanism, and impact.

### 9.1 CRITICAL — `analyzer.py`: `NameError` destroys every frame analysis **[VERIFIED by AST]**

`backend/analyzer.py` imports only `sys`, `boto3`, `os` at module scope. `import json` appears at **line 57, inside the body of `analyze_frame_with_nova`**, creating a function-local binding.

`check_pending_frames` then calls `json.dumps` at **line 145** and **line 155**. Within that function `json` is unbound, so line 145 raises `NameError: name 'json' is not defined`. The call sits inside the `try` opened at line 136, and the bare `except Exception as e` at line 162 swallows it, printing only `-> Error auditing frame {fnum}: name 'json' is not defined`.

**Impact:** the `table.update_item` on line 149 — the only code that writes `detections` and `analysis_report` to DynamoDB — is **never reached for any frame**. Every frame remains `status: "pending_audit"` forever. `GET /violations/{id}` consequently always returns `detections: []`, and the dashboard overlay is always empty. **The product's core feature does not work, and the failure is silent.**

Proof: AST inspection confirmed module-level imports `['boto3', 'os', 'sys']`, zero local imports inside `check_pending_frames`, and two `json.dumps` usages at lines 145 and 155.

**Fix shape:** add `import json` at module scope; remove the function-local `import json`/`import re` in favour of top-level imports; replace the bare `except` with typed handling that marks the frame `audit_failed` rather than leaving it `pending_audit`.

### 9.2 CRITICAL — `projects.py`: every project endpoint returns 500 **[VERIFIED by AST]**

`auth.decode_token` is declared `async def` (`auth.py:39`). `projects.get_current_user` is a **plain `def`** (`projects.py:37`) and calls `decode_token(token)` at line 38 **without `await`**, returning it directly.

The call therefore yields an un-awaited **coroutine object**, which FastAPI injects as `user`. Every handler then does `user["role"]` (e.g. `projects.py:45`) → `TypeError: 'coroutine' object is not subscriptable` → HTTP 500. A `RuntimeWarning: coroutine 'decode_token' was never awaited` accompanies it.

**Impact:** all six `/projects/*` routes are completely non-functional — create, marketplace, accept, upload-init, review, and chat. The entire client/agent project workflow is dead. (It is also unreferenced by the frontend, so the breakage is currently invisible.)

Proof: AST confirmed `decode_token` is an `AsyncFunctionDef` at `auth.py:39`; `get_current_user` is a sync `FunctionDef` at `projects.py:37` containing a `decode_token()` call at line 38 with no `Await` node anywhere in the function.

**Fix shape:** make `get_current_user` `async def` and `await decode_token(token)`. Add a regression test asserting 200/403 rather than 500. Note the `boto3.dynamodb.conditions` concern in §7.2 may surface immediately afterwards.

### 9.3 CRITICAL — Remediation is a disguised mock **[VERIFIED]**

In `backend/audit_manager.py:73-123`, `remediate_frame()`:
- correctly computes pixel coordinates and draws a Pillow mask (lines 82-100);
- builds a Nova Canvas `INPAINTING` payload (lines 104-111) using `.decode('latin1')`, which is not a valid encoding for Bedrock image input;
- has the actual `bedrock.invoke_model(...)` call **commented out** on line 116;
- `return image_bytes` on line 119 — **the unmodified input image**.

The caller (`run_audit_pipeline`, lines 166-181) stores that unchanged image to GridFS and sets `status: "remediated"`, `remediated: True`.

**Impact:** the system asserts that a violation was remediated while having performed no edit whatsoever. This is precisely the "mock disguised as real AI output" failure mode the project must eliminate. The comment on line 118 ("For the hackathon POC, return the original image bytes as fallback or success simulation") confirms it is deliberate.

Compounding this, `audit_frame()` in the same file can never succeed: line 54 passes raw `bytes` into `json.dumps`, which raises `TypeError: Object of type bytes is not JSON serializable`, caught on line 69. So `audit_result` is always `{"error": ...}`, `violation_detected` is always falsy, and the remediation branch on line 161 is in fact **never entered**. The mock is unreachable — but the code must not ship in either state.

### 9.4 HIGH — Video reconstruction stitches the *original* frames **[VERIFIED]**

`video_stitcher.reconstruct_video()` queries DynamoDB for all frames of a video and downloads `f["s3_key"]` (line 50) — the key written by the extractor for the **unmodified** frame. No remediated-frame key is ever consulted (none exists in the DynamoDB schema; the only remediated artefact in the codebase is a GridFS id in the orphaned MongoDB pipeline).

**Impact:** even with remediation working, the exported video would be the uncorrected original. Additional defects in the same function:
- `S3_BUCKET` is taken from the module-level env default (line 55) instead of the per-frame `s3_bucket` attribute the extractor records — breaks if the bucket ever differs.
- Output framerate is hardcoded to `1` (line 63), matching the 1 fps extraction. The reconstructed video therefore runs at 1 fps regardless of source — **the output is not a faithful rendition of the input**.
- **Audio is silently discarded** — no audio stream is carried through.
- Download failures are caught per-frame and logged (lines 56-57) but **do not abort the stitch**, so FFmpeg's `frame_%04d` glob will silently truncate at the first gap, producing a short video with no error.
- Temp-directory cleanup is commented out (line 80).
- No API route exposes this function; it is CLI-only.

### 9.5 HIGH — No post-remediation verification exists **[VERIFIED]**

`docs/planning.md:22` specifies a "Critic" (Nova Pro) that re-checks remediated output and routes to a human when confidence < 0.8. **No such code exists.** `backend/verifier.py`, despite its name, is a 47-line dev utility that fetches one frame from GridFS and writes `test_output.jpg` to the CWD. It hardcodes `mongodb://localhost:27017/` (line 7) with no env override and reads `metadata["gridfs_id"]`, a field nothing populates (§4.4).

### 9.6 HIGH — `/submissions` fabricates audit metadata **[VERIFIED]**

`app.py:157-181` lists files in `./uploads/` and synthesises the response:

- `status` = `"VERIFIED"` if `hash(video_id) % 2 == 0` else `"AUDIT_PENDING"` (line 176) — a **coin flip**, unrelated to any analysis.
- `agent` = picked from the hardcoded pool `["LLAVA-01", "BEDROCK-ALPHA", "NOVA-PROBE", "AGENT-K"]` by `hash(video_id)` (lines 165, 174).
- `time` = the literal string `"12:32:01 LOCAL"` for every row (line 173).
- `url` = **hardcoded** `http://localhost:8000/...` (line 177), ignoring the `API_URL` env var used on line 82 — breaks in any deployed environment.

The docstring admits it ("mocked for multi-agent demo", line 160). `ClientPortal.tsx:41` renders this as genuine data, so the client-facing portal displays fabricated verification statuses. Note also that Python's `hash()` of a `str` is **salted per process** (PYTHONHASHSEED), so the status of a given video **changes on every backend restart**.

### 9.7 HIGH — The analysis trigger is never invoked **[VERIFIED]**

`POST /analyze/{video_id}` (`app.py:92`) is the sole entry point to the AI pipeline. A full grep of `frontend/app/` for `fetch(` returns exactly five call sites: `/submissions`, `/violations/{id}`, `/upload-reel`, and the login endpoint. **`/analyze` is never called.**

The "Launch Audit" button (`HUDView.tsx:178-220`) posts to `/upload-reel`, stores the returned `video_id`, sets the video source, and starts polling `/violations/{id}` — but never triggers analysis. The user presses "Launch Audit" and nothing is ever audited.

**Impact:** combined with §9.1, there are two independent reasons the dashboard can never display a violation. Both must be fixed.

### 9.8 MEDIUM — Dead and duplicated code at scale **[VERIFIED]**

See §2.3 for the full table. Highlights: a 375 KB orphaned `frontend_backup/` dashboard; a directory containing only a 196 KB stray `package-lock.json`; two divergent `prompts.py`; two different `test_frame.jpg`; duplicate `/agents` and `/logs` routes each carrying their own copy of the hardcoded data; an entire unreachable MongoDB pipeline (`audit_manager.py`, `verifier.py`); dead scaffolding at `analyzer.py:28-51`; and three unused modules (`safety.py`, `aws_config.py` helpers, `automation.py`).

### 9.9 MEDIUM — `/execute` returns mock text as if it were AI output **[VERIFIED]**

`orchestrator.py:10-18` wraps the `strands`/`strands_agents` import in a nested `try/except ImportError` that falls back to **mock classes** whose `run()` returns the literal string `"Mock Response (strands_agents missing)"`. `strands-agents` is listed in `requirements.txt` but is **not installed** in this environment.

`POST /execute` (`app.py:55`) returns that string as `steps` with `"status": "plan_generated"`. The silent fallback means a misconfigured deployment looks healthy while producing nothing. `automation.py:6-15` has the identical anti-pattern for `nova_act` (and `nova-act` is not even in `requirements.txt`).

**Fix shape:** fail loudly on missing AI dependencies, or gate the route behind an explicit feature flag that the response surfaces.

### 9.10 MEDIUM — Silent pipeline drop-outs leave jobs permanently "pending" **[VERIFIED]**

`extractor.extract_and_store()` returns early, with no status recorded anywhere, when:
- `get_video_duration()` returns `None` — i.e. the probe failed or there is no video stream (lines 48-49);
- duration exceeds `MAX_DURATION = 60` s (lines 51-53).

In both cases the function just `print`s and returns. The caller `run_pipeline` (`app.py:108`) proceeds to `check_pending_frames` regardless, which finds nothing. No error propagates to the API, no DynamoDB record is written, no job status is updated.

**Impact:** a user uploading a 90-second video sees "Neural analysis initiated" and then an eternally empty violation list, with no error anywhere in the UI. The 60-second cap is also undocumented in the API response and contradicts nothing enforced at upload time.

### 9.11 MEDIUM — The `/projects` review state is unreachable **[VERIFIED]**

As detailed in §7.2: `POST /projects/{id}/review` requires `status == "CLIENT_REVIEW"` (`projects.py:175`), but no code path ever assigns that status. `upload-init` sets `AI_AUDITING` (line 149) and nothing advances it further. The client review workflow cannot be completed even after §9.2 is fixed.

Also in the same file: `projects.py:91-96` defines `ExpressionAttributeValues` including `":submitted"`, used only by the `ConditionExpression` — correct, but `projects.py:103-105` catches **all** exceptions and reports "It might be already taken", masking genuine infrastructure errors (missing table, no credentials) as a business-logic conflict.

### 9.12 LOW — Frontend data integrity and UX defects **[VERIFIED]**

- `AgentsView.tsx:20,25`, `LogsView.tsx:19`, `agents/page.tsx:26,31,37`, `logs/page.tsx:25` are **hardcoded arrays**. The Agents, Compliance Logs, and telemetry views show fixed fictional data.
- `HUDView.tsx:107` falls back to a **remote MDN sample video** (`https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4`) when no upload has occurred — an external dependency in the product UI.
- `HUDView.tsx:175` advertises "Max 60MB / 60S" but no size or duration check is performed. `HUDView.tsx:183-186` creates a `video` element and uses `onloadedmetadata` purely to sequence the upload; `video.duration` is **never read**.
- `HUDView.tsx:208` surfaces failures with `alert("Sync Error")`. Per project convention, browser modal dialogs should be avoided; it also discards the actual error.
- `HUDView.tsx:38-45` reads submissions from `localStorage` key `novaflow_submissions`, which **nothing ever writes** — `setSubmissions` only mutates React state (line 199). The submission list silently resets on reload.
- `HUDView.tsx:46-51` defines a `steps` array ("4096 clusters synced") that is decorative, fixed status text.
- `HUDView.tsx:132` renders `Math.round(d.confidence * 100)` with no guard for a missing `confidence`, and `d.bounding_box[...]` with no guard for a missing/short array — a malformed model response would render `NaN%` or crash the overlay.
- `HUDView.tsx:20` redefines `cn()` locally, duplicating `app/lib/utils.ts:4`.
- `page.tsx:28` branches on `user?.role === "client"`, but role is read from `localStorage` and never re-validated server-side (§10.2).
- `frontend/package.json:9` sets `"lint": "eslint"` with no target path; `eslint` with no arguments lints nothing useful in a Next.js project. There is **no `typecheck` script** despite `strict: true`.

---

## 10. Security and reliability findings

### 10.1 Unsafe file upload — HIGH **[VERIFIED]**

`POST /upload-reel` (`app.py:69-90`) has, in combination:

1. **No authentication.** Anyone who can reach the port can upload. Same for `/analyze`, `/violations`, `/submissions`, `/execute`.
2. **Path traversal via the extension.** Line 75 computes `extension = file.filename.split('.')[-1]` from the **client-supplied filename** and interpolates it into `file_path` on line 77. `split('.')[-1]` does not strip path separators: a filename such as `a.mp4/../../evil.py` yields the extension `mp4/../../evil.py`, and `os.path.join(UPLOAD_DIR, f"{video_id}.{extension}")` escapes the upload directory. **Arbitrary file write.** The extension must be validated against an allowlist and the resolved path confirmed to stay within `UPLOAD_DIR`.
3. **No size limit.** `shutil.copyfileobj` (line 80) streams the whole body to disk. An unauthenticated client can exhaust the disk.
4. **No content validation.** No MIME check, no magic-byte sniff, no FFmpeg probe before acceptance. The only check is the client-side `accept="video/*"` attribute.
5. **No duration check at upload**, despite the pipeline rejecting >60 s later and silently (§9.10).
6. **Public static serving.** `app.mount("/uploads", StaticFiles(...))` (line 38) serves every uploaded file to anyone, with no auth and no expiry. Combined with (2) and (4), a crafted upload can be stored and then served back from the application origin.

### 10.2 Authentication and authorization — HIGH **[VERIFIED]**

| # | Finding | Location |
| :-- | :-- | :-- |
| 1 | **Hardcoded JWT signing key.** The HS256 secret is a literal in source (`auth.py:15`) with no env override. Anyone with repository read access — and the repository is **PUBLIC** (§12) — can forge a token for any user and any role. | `auth.py:15,36,42` |
| 2 | **Privilege escalation at signup.** `AuthRequest.role` is client-supplied (`auth.py:56`) and `POST /signup` accepts `"agent"` with no authorization check (lines 97-98, 102). Any anonymous user can self-provision an agent account. | `auth.py:88-109` |
| 3 | **Unsalted SHA-256 password hashing.** `hash_password` is a bare `hashlib.sha256` (line 24) — fast, unsalted, trivially rainbow-tabled. A password KDF (bcrypt/argon2/scrypt) is required. `hmac.compare_digest` on line 28 correctly avoids timing leaks, but cannot compensate. Note `passlib`/`bcrypt` is not in `requirements.txt`. | `auth.py:23-28` |
| 4 | **No server-side route protection.** Authorization is purely cosmetic nav filtering (`page.tsx:86`). The backend endpoints the UI actually uses (`/upload-reel`, `/analyze`, `/violations`, `/submissions`) require **no token at all**. | `app.py`, `page.tsx:80-86` |
| 5 | **Token never validated after login.** `AuthContext.tsx:28-42` trusts `localStorage` and never calls `GET /auth/me`. An expired or tampered token leaves the user apparently logged in until a request fails. There is no Next.js `middleware.ts`, so routes are not guarded at the edge. | `AuthContext.tsx:28-42` |
| 6 | **JWT in `localStorage`** — readable by any XSS payload. No refresh/rotation; a 24-hour token (`auth.py:17`) cannot be revoked. | `AuthContext.tsx:48-49` |
| 7 | **No rate limiting** on `/auth/login` or `/auth/signup` — unbounded credential stuffing. | `auth.py:112` |
| 8 | **No username uniqueness constraint.** `auth.py:93` does a read-then-write check with no unique index on `users.username`, so concurrent signups can create duplicates. | `auth.py:93-106` |
| 9 | **README publishes test credentials** for `agent_test` / `client_test` with `password123` (`README.md:22-25`) in a public repository. No seed script creates these users (grep for `agent_test` returns nothing outside the README), so the credentials are also **false documentation** — but if anyone creates them to match the docs, they become a live weak-credential backdoor. | `README.md:22-25` |

### 10.3 Cloud configuration — MEDIUM **[VERIFIED]**

- **IAM/bucket mismatch.** `infra/iam_policy.yaml:36-40` scopes S3 access to `brand-guardian-raw` and `brand-guardian-final`; all application code defaults to `novaflow-media`. The role grants access to unused buckets and **no access to the one in use**.
- **No DynamoDB permissions in the IAM role at all**, despite DynamoDB being central to the live pipeline. The role as written cannot run the application.
- **No Step Functions permissions**, despite a `stepfunctions` client being constructed (`aws_config.py:30`).
- **`bedrock:InvokeModel` on `Resource: '*'`** (`iam_policy.yaml:28`) — grants every Bedrock model, not just the two required. Should be scoped to the specific model ARNs.
- **No bucket hardening anywhere.** `infra/init_s3.py:17,21` calls `create_bucket` with no `PublicAccessBlockConfiguration`, no default encryption, no versioning, and no bucket policy. **[OPEN]** Whether the live `novaflow-media` bucket is public could not be checked without credentials — **this must be verified manually before any public demo.**
- **No IaC for the DynamoDB tables.** `NovaFlowFrames` and `NovaFlowProjects` must be created by hand; their key schemas exist only implicitly in application code.
- **Presigned PUT accepts an unvalidated object.** `projects.py:135-143` issues a 1-hour presigned `put_object` URL with `ContentType: video/mp4` in `Params`. A presigned URL constrains only the signed parameters; the client controls the body, so arbitrary content can be written to that key. Nothing downstream validates it (and nothing reads it at all — §7.4).

### 10.4 Reliability and resource handling — MEDIUM/HIGH **[VERIFIED]**

| Finding | Location | Impact |
| :-- | :-- | :-- |
| **In-process background tasks, no queue.** `BackgroundTasks` (`app.py:93,115`) runs the whole FFmpeg + Bedrock pipeline inside the web worker. A restart, crash, or deploy loses the job with no record and no retry. There is no job table, no idempotency key, and no way to resume. | `app.py:92-121` | Data loss |
| **Blocking work on the event loop.** `run_pipeline` is a **sync** function (`app.py:108`) doing FFmpeg I/O, S3 round-trips, and Bedrock calls. FastAPI runs sync background tasks in a threadpool, so this will saturate the default threadpool under concurrency and stall the service. | `app.py:108-115` | Availability |
| **Duplicate-job hazard.** `/analyze/{video_id}` can be called repeatedly for the same id with no guard; each run re-extracts and re-uploads frames over the same S3 keys and re-scans DynamoDB. | `app.py:92` | Cost, corruption |
| **Loose video-id matching.** `files = [f for f in os.listdir(UPLOAD_DIR) if f.startswith(video_id)]` (`app.py:98`) then takes `files[0]`. A prefix collision selects the wrong file. | `app.py:98-102` | Wrong input |
| **No timeouts on any AWS call.** No `botocore.config.Config(connect_timeout=…, read_timeout=…, retries=…)` anywhere. Default Bedrock read timeouts are long; a hung call blocks a worker thread indefinitely. | all boto3 clients | Availability |
| **No retry/backoff** around Bedrock, S3, or DynamoDB beyond boto3's defaults. Throttling on a per-frame loop will drop frames with only a printed message. | `analyzer.py:128-163` | Data loss |
| **Per-frame failures are swallowed and leave inconsistent state.** `except Exception` on `analyzer.py:162` and `extractor.py:128` logs and continues; a frame that fails to store leaves `frame_count` un-incremented, so **subsequent frames reuse the same S3 key and frame number** — silently overwriting data and corrupting the frame sequence. | `extractor.py:126-129` | **Data integrity** |
| **Unbounded memory in extraction.** `extractor.py:76-103` accumulates FFmpeg output in a `bytearray` and scans for JPEG SOI/EOI markers. If an EOI is never found the buffer grows without limit. Byte-stuffing within JPEG entropy data is not handled, so marker-scanning is fragile. | `extractor.py:76-103` | OOM, corruption |
| **No cleanup of uploads or temp dirs.** `uploads/` grows forever; `temp_frames_*/` cleanup is commented out (`video_stitcher.py:80`). | `app.py:36`, `video_stitcher.py:80` | Disk exhaustion |
| **Full-table scans in request paths.** `analyzer.py:109` and `projects.py:75` both `Scan`. Cost and latency grow linearly with total table size; both are acknowledged as POC shortcuts. | `analyzer.py:108`, `projects.py:72` | Cost, latency |
| **CORS `allow_origins=["*"]`** with all methods and headers on the main app (`app.py:28-33`), conflicting with the sub-app's restrictive policy (`auth.py:76`). | `app.py:28-33` | Exposure |
| **No `/health` readiness probe.** `/status` returns a static literal (`app.py:47-49`) and checks no dependency — it reports "online" while MongoDB, S3, and Bedrock are all unreachable. | `app.py:47` | Observability |
| **`print()` as logging.** Every backend module uses `print` rather than the `logging` module (only `s3_trigger.py` configures a logger). No levels, no structure, no correlation ids. | throughout | Observability |
| **Potentially sensitive data in logs.** `analyzer.py:145` would print the full model analysis (scene descriptions of user video content) to stdout; `extractor.py:69` pipes all FFmpeg stderr to stdout, which includes full input file paths. Raw exception strings are returned to clients at `app.py:155` and `projects.py:201`, leaking internal detail. | `analyzer.py:145`, `extractor.py:69`, `app.py:155` | Information disclosure |

### 10.5 Unsafe assumptions in AI-generated corrections — HIGH **[VERIFIED]**

Beyond the remediation being mocked (§9.3), the design lacks every safeguard a remediation feature needs:

- **No validation of model-supplied coordinates.** `audit_manager.py:86` destructures `box_2d` into four values with no length, type, range, or ordering check. A malformed or hallucinated box (negative, inverted, >1000, or `null` — note line 42 explicitly instructs the model to return `null`) propagates straight into `draw.rectangle` (line 95). A `None` box would raise `TypeError`; an inverted box silently produces an empty or wrong mask.
- **No confidence gating.** `prompts.py:17` asks the model for a `confidence` value and `docs/planning.md:24` specifies a 0.8 human-in-the-loop threshold, but **no code reads `confidence` for any decision**. Low-confidence detections would be remediated automatically.
- **No human-in-the-loop.** Planned (`docs/planning.md:24`) but entirely absent.
- **No before/after comparison, no artefact detection, no re-audit** of remediated frames.
- **No originals preservation guarantee.** The remediated frame is written as a new GridFS object (`audit_manager.py:170`), which is sound, but there is no provenance chain, no audit record of what was changed, and no rollback path.
- **Unbounded trust in model JSON.** `analyzer.py:88` extracts a box with the greedy regex `r'\{.*\}'` across the whole response — on a response containing multiple JSON objects it captures from the first `{` to the **last** `}`, which is not valid JSON and falls into the bare `except` on line 92. `audit_manager.py:67` calls `json.loads(output_text)` with no extraction or schema validation at all. No Pydantic model validates any model output anywhere.

### 10.6 Repository hygiene — MEDIUM/HIGH **[VERIFIED]**

- **No secrets found in tracked files.** A scan for `AKIA[0-9A-Z]{16}`, `aws_secret`, `api_key`, `mongodb+srv://`, and bearer-token patterns across all `.py`, `.ts`, `.tsx`, `.json`, `.yaml`, `.md` files returned **no live credentials**. No `.env` file exists on disk. The only match of concern is the hardcoded JWT dev secret at `auth.py:15` (§10.2).
- **No secrets in Git history** — because there is **no Git history**. The local repository has zero commits, and the remote contains a single commit of 5 non-sensitive files (§12).
- **`.gitignore` is in the wrong place and incomplete.** It sits at `Amazon_Nova-main/.gitignore`, but the active Git repository is rooted at the **Desktop** (§12), so it governs only that subtree. Even applied correctly it **omits**:
  - `backend/uploads/` — user-uploaded videos would be committed;
  - `temp_frames_*/` — `video_stitcher.py` scratch dirs;
  - `test_output.jpg` — written by `verifier.py:32`;
  - `final_compliant_video.mp4` — default stitcher output (`video_stitcher.py:19`);
  - `.vscode/`, `*.swp`, `Thumbs.db`, `desktop.ini` (the last is relevant on this OneDrive-backed Windows path).
  It does correctly ignore `.env*`, `__pycache__/`, `node_modules/`, and `.next/`.
- **No `.gitattributes`.** With `core.autocrlf=true` (§12), the CRLF files in this tree will be LF-normalized on commit, producing spurious whole-file diffs. A `.gitattributes` pinning `* text=auto eol=lf` should land with the first commit.
- **`.claude/settings.local.json` is committed and permissive** — pre-approves `pip install:*`, `npm install:*`, `curl:*`, `python3:*`. A `*.local.json` file is conventionally developer-local and should be git-ignored, not shared.
- **No `.dockerignore` at the repo root**; `backend/.dockerignore` exists but the build context is `backend/` only, so the frontend is excluded by path rather than by rule.
- **No `LICENSE`** in a public repository.
- **No `.env.example`**, no `CONTRIBUTING.md`, no architecture diagram.

---

## 11. Documentation accuracy

### 11.1 `README.md` — materially inaccurate **[VERIFIED]**

| Claim | Line | Reality |
| :-- | :-- | :-- |
| "multi-modal AI analysis (LLaVA)" | 3 | No LLaVA/Ollama code exists. The analyser uses Amazon Nova Pro via Bedrock. |
| "robust browser automation (Nova Act)" | 3 | `automation.py` is a mock; `nova-act` is not even a declared dependency; no route uses it. |
| "Real-time bounding box visualization" | 9 | The overlay component exists, but it never receives data (§9.1, §9.7). |
| "Comprehensive logging of AI-detected anomalies" | 10 | The logs view is a hardcoded array (`LogsView.tsx:19`). |
| Testing credentials `agent_test`/`client_test`/`password123` | 22-25 | No seed script creates these users. The table is both false and a security liability in a public repo. |
| "Next.js 14" | 28 | Next.js 16.1.6. |
| "MongoDB (GridFS)" | 29 | GridFS is used only by the orphaned pipeline. The live store is S3 + DynamoDB. |
| "Ollama (LLaVA)" | 29 | Not present. |
| "Once the services are running" | 17 | No instructions exist for *how* to run them — no setup steps, no dependency install, no env var list, no MongoDB/AWS prerequisites. |

The README describes a different system from the one in the repository. It must be rewritten from verified facts.

### 11.2 `docs/planning.md` and `SKILL.md` **[VERIFIED]**

`docs/planning.md` is a **target** design (Planner–Critic–Remediator over Step Functions) and is largely unimplemented: no Critic, no Step Functions, no HITL, no working Nova Canvas call. It is not labelled as aspirational, which invites exactly the mistake of assuming the architecture exists. It should be retitled as a design target.

`.agent/skills/video-audit/SKILL.md` documents a bounding-box schema (`{x, y, width, height}` + `timestamp_ms`) that **matches no code in the repository** and conflicts with both `prompts.py` and `audit_manager.py` (§4.5). Its coordinate-scaling formula is correct and useful; the JSON contract is wrong. It is actively misleading for future sessions and must be corrected.

---

## 12. Git and repository state — **BLOCKER** **[VERIFIED]**

This is the most urgent finding in the audit and the reason the Phase 1 commit was **not** executed.

### 12.1 The local repository is rooted at the user's Desktop

```
git rev-parse --show-toplevel  →  C:/Users/ayush/OneDrive/Desktop
git rev-parse --absolute-git-dir →  C:/Users/ayush/OneDrive/Desktop/.git
```

A `git init` was run in `C:\Users\ayush\OneDrive\Desktop`, **not** in the project folder. Consequences:

- `git status --porcelain --untracked-files=all` at that root reports **37,558 files**. A `git add -A` + commit from the repository root would capture the user's entire Desktop — resumes (`Ayush_Bidwai_Master_Resume*.docx/pdf`), personal PDFs, unrelated projects (`DSA/`, `GAMES/`, `Website/`, `campusfeed/`, `collage/`, `SLM Project/`), `.lnk` shortcuts, and `__pycache__` — into a **public** GitHub repository. This is a serious data-exposure hazard.
- The project's `.gitignore` lives at `Amazon_Nova-main/.gitignore` and therefore governs only that subtree; the Desktop root has no `.gitignore` at all.

### 12.2 No local history, no remote

```
git log            →  fatal: your current branch 'main' does not have any commits yet
git show-ref       →  (empty — no refs exist)
git remote -v      →  (empty — no remote configured)
git stash list     →  (empty)
git diff --cached  →  (empty — nothing staged)
```

The branch is `main` (unborn). `git config --local --list` shows only `core.*` defaults plus `user.name=YushBytes`; **`user.email` is not set locally**, which will block a commit unless a global value exists. `core.autocrlf=true` is set.

### 12.3 The remote exists but contains almost nothing

```
gh repo view YushBytes/Amazon_Nova_Project
  → visibility: PUBLIC, isEmpty: false, defaultBranch: main
  → pushedAt: 2026-10-18T16:45:44Z

git ls-remote --heads  →  e059a9cf1e2a6972c0ba49365aa749de590c6cd1  refs/heads/main

HEAD commit e059a9c — "Add files via upload" — Ayush Bidwai, 2026-10-18T16:45:43Z
```

The remote's `main` holds **exactly 5 blobs**:

```
.gitignore
README.md
analyzer_nova.py
prompts.py
test_frame.jpg
```

**The premise that the extracted project files were pushed to this repository is incorrect.** Only the five root-level files were uploaded, via the GitHub web UI. The entire `backend/` (17 modules), `frontend/` (Next.js app), `infra/`, `docs/`, and `frontend_backup/` trees — i.e. essentially the whole project — have **never been pushed**.

### 12.4 Local vs remote content comparison

| File | Remote blob SHA vs local `git hash-object` | Byte comparison |
| :-- | :-- | :-- |
| `.gitignore` | same | identical |
| `README.md` | same | identical |
| `test_frame.jpg` | same | identical |
| `analyzer_nova.py` | **differs** | **byte-identical** (1391 bytes, 58 CRLF both sides) |
| `prompts.py` | **differs** | **byte-identical** (695 bytes, 25 CRLF both sides) |

The two SHA mismatches are **not** content differences. `core.autocrlf=true` causes `git hash-object` to normalize CRLF→LF before hashing, while the web-uploaded remote blobs preserve CRLF verbatim. Verified with `cmp`: both files are byte-identical.

**Conclusion: the remote contains no work that is absent locally.** Nothing would be lost by building on it. The spurious line-ending delta is a reason to add `.gitattributes` with the first commit, not a conflict.

### 12.5 Authentication

`gh auth status`: logged in to github.com as **YushBytes**, token in keyring, scopes `gist`, `read:org`, `repo`, `workflow`; HTTPS git protocol. **Push permission is available.** Authentication is not the blocker.

### 12.6 Why no commit was made in Phase 1

Step 1 of the Phase 1 brief directs: *"If the remote is incorrect, the repository has unexpected history, or the Git state is unsafe, stop before changing Git configuration or pushing anything."*

All three conditions hold: the repository is rooted at the wrong directory (unsafe), no remote is configured (incorrect), and the remote's contents do not match the stated premise (unexpected). Any `git add`/`commit` from the current root risks committing 37,558 personal files to a public repository.

The audit deliverables have therefore been **written to disk but left uncommitted**. §13 specifies the minimum intervention required. **No Git configuration was changed, nothing was staged, and nothing was pushed.**

---

## 13. Required human intervention (blockers)

Ordered by urgency. Items 1–2 block the Phase 1 commit; items 3–6 block later phases.

### 1. Decide the project root, then re-initialise Git correctly — **BLOCKING**

A decision is required before any commit:

- **(a)** Treat `Amazon Nova/Amazon_Nova-main/` as the project root (minimal change), **or**
- **(b)** Move the contents of `Amazon_Nova-main/` up into `Amazon Nova/` and drop the zip-extraction folder name (cleaner, matches the shell's working directory).

**(b) is recommended** — `Amazon_Nova-main` is a zip-extraction artefact, not a meaningful directory name.

The stray Desktop repository must then be addressed. The safe sequence, to be run **only with explicit approval**, is: remove `C:\Users\ayush\OneDrive\Desktop\.git` (it holds **zero commits**, so **no history can be lost** — verified via `git show-ref` returning empty and `git stash list` empty), then `git init` inside the chosen project root, add the remote, and `git fetch` + base the first commit on `e059a9c` so the remote's existing commit is preserved rather than overwritten.

**No force-push is needed or permitted.** The remote's 5 files are byte-identical to local, so a normal merge-and-push is sufficient.

### 2. Confirm whether to keep the repository public — **BLOCKING**

`YushBytes/Amazon_Nova_Project` is **PUBLIC**. Before pushing the full codebase, note that a public push makes the hardcoded JWT secret (`auth.py:15`) and the documented test credentials (`README.md:22-25`) world-readable. Neither is a live cloud credential, but both should be removed in Phase 2 regardless. Consider making the repository private until the Phase 2 security fixes land.

### 3. Rotate nothing — but confirm **[VERIFIED]**

No live credential was found in the repository or in Git history. **No rotation appears necessary.** However, two items warrant a manual check that the audit could not perform:

- Confirm no `.env` file was ever shared or committed elsewhere (none exists on disk now).
- If the `brand-guardian-dev-secret-change-in-prod` JWT key at `auth.py:15` was ever used to sign tokens against a deployed instance, **all issued tokens must be treated as compromised** once the repository is public. Replace it with an env-sourced secret and invalidate existing sessions in Phase 2.

### 4. Provide the AWS prerequisites

Needed before the AI pipeline can be verified end to end (§8.6): credentials/role with Bedrock + S3 + DynamoDB access; **explicit Bedrock model access for `amazon.nova-pro-v1:0`** (and `amazon.nova-canvas-v1:0` if remediation is in scope) in the chosen region; the two DynamoDB tables; and the S3 bucket. **Also confirm whether `novaflow-media` currently has public access blocked** — this could not be checked without credentials.

### 5. Install the missing local toolchain

**FFmpeg is not installed on this host** and is required for all video work. Python 3.14.6 is also well ahead of the Dockerfile's 3.11; some pinned dependencies may lack wheels for 3.14.

### 6. Answer the open architecture questions

| # | Question | Why it blocks work |
| :-- | :-- | :-- |
| 1 | Delete the MongoDB/GridFS pipeline (`audit_manager.py`, `verifier.py`, `check_count.py`) in favour of S3 + DynamoDB? | Determines whether §4.4 is a cleanup or a migration. Recommended: **delete**; it is unreachable and structurally unrunnable. |
| 2 | Keep MongoDB for user accounts, or consolidate onto DynamoDB? | Two databases for one small app is avoidable operational cost. Phase 1 does **not** recommend changing this unilaterally. |
| 3 | Is video **reconstruction** in release scope, or is detection + reporting sufficient for v1? | Determines whether §9.4 is a blocker or deferred. |
| 4 | Is **remediation** (Nova Canvas inpainting) in release scope? | It is currently a disguised mock (§9.3) and must either be implemented properly or removed outright. |
| 5 | Which bounding-box schema is canonical? | Three conflicting definitions (§4.5) must collapse to one before any remediation work. |
| 6 | Correct model ID — `amazon.nova-pro-v1:0`, or a "Nova 2 Pro" id? | Docs and code disagree (§6.4). |
| 7 | Is Nova Act / browser automation in scope at all? | `automation.py`, `orchestrator.py`, `/execute`, and the "Neural Command" UI panel all hang off this. If out of scope, three modules and a UI section can be deleted. |
| 8 | Are the `brand-guardian-*` buckets in `infra/` intentional, or leftovers to delete? | Determines whether to fix the IAM policy or the code defaults (§10.3). |
| 9 | Deployment target: Amplify (frontend) + what for the backend — ECS, Lambda, App Runner? | `amplify.yml` covers only the frontend; the backend has a Dockerfile but no deployment definition. |

---

## 14. Known limitations of this audit

Stated plainly, so nothing here is over-trusted:

1. **No code was executed beyond syntax compilation and AST analysis.** Required dependencies (`boto3`, `motor`, `jose`, `pymongo`, `ffmpeg-python`) are not installed and FFmpeg is absent. Every "Implemented but not verified" classification means exactly that.
2. **No AWS or Bedrock call was made.** No credentials were used. Nothing about live model behaviour, response schemas, IAM effectiveness, or bucket permissions has been confirmed. The Bedrock payload concerns in §6.4 are **inferred from source**, not observed.
3. **No MongoDB instance was available**, so the auth flow (signup → login → `/auth/me`) is unverified end to end.
4. **No frontend build or lint run.** `node_modules` is absent, and installing it was out of scope. TypeScript and ESLint may surface additional errors — `strict: true` with liberal `any` usage in `HUDView.tsx` is a likely source.
5. **`frontend_backup/` was inventoried but not read in depth**, as it is orphaned and recommended for deletion. If it is to be revived, it needs its own audit.
6. **`package-lock.json` files were excluded** from the secret scan to avoid noise. They contain no first-party code.
7. **Dependency vulnerability scanning was not performed** — no `npm audit` (no lockfile install) and no `pip-audit`. Given that `requirements.txt` pins **nothing**, a supply-chain review is a genuine Phase 2 task.
8. **Behavioural claims about the UI are from source reading**, not from running the app. The "always empty violations list" conclusion follows from §9.1 and §9.7 by static reasoning; it is strongly supported but was not observed in a browser.

---

## 15. Summary of evidence for the headline findings

| Finding | Severity | Verification |
| :-- | :-- | :-- |
| Git repo rooted at Desktop; 37,558 files exposed to a public push | **BLOCKER** | `git rev-parse --show-toplevel`, `git status --porcelain -uall \| wc -l` |
| Project was never actually pushed — remote holds 5 of ~130 files | **BLOCKER** | `gh api .../git/trees/main?recursive=1` → 5 blobs |
| `analyzer.py` `NameError` — no detection is ever persisted | **CRITICAL** | AST: module imports `['boto3','os','sys']`; `json.dumps` at lines 145, 155 |
| All 6 `/projects` routes 500 — missing `await` | **CRITICAL** | AST: `decode_token` is `AsyncFunctionDef`; `get_current_user` sync, no `Await` |
| Remediation returns the input unchanged while reporting success | **CRITICAL** | `audit_manager.py:116` commented out, `:119` `return image_bytes`, `:178` `remediated: True` |
| Hardcoded JWT secret in a public repo + signup role self-election | **HIGH** | `auth.py:15`, `auth.py:56,97,102` |
| Path traversal via user-controlled upload extension | **HIGH** | `app.py:75,77` |
| `/analyze` never called by the UI | **HIGH** | grep: 5 `fetch(` sites, none to `/analyze` |
| `requirements.txt` omits `boto3` — backend cannot start from the manifest | **HIGH** | `find_spec` probe + `app.py:19 → projects.py:3` |
| `/submissions` fabricates statuses from `hash()` | **HIGH** | `app.py:174-177` |
| Reconstruction stitches original, not remediated, frames | **HIGH** | `video_stitcher.py:50` |
| Zero tests, zero CI | **HIGH** | `find` for test/spec files; no `.github/` |
| Two parallel incompatible pipelines; one structurally unrunnable | **MEDIUM** | §4.4 — no writer for `gridfs_id` exists |
| Three conflicting bounding-box schemas | **MEDIUM** | `prompts.py:16`, `audit_manager.py:39`, `SKILL.md` |
| README describes a different system (LLaVA, GridFS, Next 14) | **MEDIUM** | §11.1 |
| No live secrets in code or Git history | **(good)** | pattern scan; zero commits locally, 5 benign blobs remotely |

---

*End of audit. See `docs/IMPLEMENTATION_ROADMAP.md` for the prioritized remediation plan and `CLAUDE.md` for working conventions.*
