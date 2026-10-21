# NovaFlow / Brand Guardian

A video brand-compliance application. You upload a video; the backend extracts
frames with FFmpeg, sends each frame to Amazon Bedrock (Nova Pro) for brand-safety
analysis, and stores per-frame detections that the dashboard renders as bounding
box overlays.

**Status: under active development, not production-ready.** See
[Limitations](#limitations) before relying on anything here. Statements in this
file are kept to what is verifiable in the code; `docs/PROJECT_AUDIT.md` is the
evidence base, and `CLAUDE.md` records the current phase.

---

## Architecture

```
Browser (Next.js, :3000)
        │  JWT in Authorization header
        ▼
FastAPI API (:8000)  ─── accepts uploads, queues jobs, serves results
        │                  (never runs analysis in worker mode)
        ├── MongoDB ......... accounts, ownership, job state AND the job queue
        ├── S3 .............. source videos and extracted frames (one bucket)
        └── DynamoDB ........ per-frame metadata and detections

Worker process  ─── python backend/worker.py
        │             polls the queue, claims a job under a lease, runs it
        ├── S3 .............. downloads the source, uploads frames
        ├── DynamoDB ........ writes frame metadata and detections
        └── Bedrock ......... amazon.nova-pro-v1:0, one call per frame
```

The API and the worker are separate processes over shared storage. Acceptance
is a database write that happens **before** the API responds, so an accepted
job survives an API restart — there is no in-memory handoff to lose. The queue
is the `videos` collection itself, not a separate queue service; see
`docs/PIPELINE_ARCHITECTURE.md` §8 for why, including why SQS was rejected.

**Frontend** — Next.js 16.1.6, React 19.2.3, TypeScript 5.9.3 (`strict`),
Tailwind CSS v4, Framer Motion, lucide-react. Tests run on Vitest. Three
routes (`/`, `/login`, `/_not-found`); the shell at `/` switches views in
client state, so individual views are not deep-linkable. Every view is backed
by a real endpoint or says on screen that the capability does not exist —
`frontend/README.md` maps each one to its route.

**Backend** — FastAPI + Uvicorn, `python-jose` JWT (HS256), Argon2id password
hashing, `motor` (async MongoDB), `boto3`, `ffmpeg-python`. Tests run on pytest.

### Where data actually lives

| Data | Store | Notes |
| :-- | :-- | :-- |
| User accounts | MongoDB `novaflow.users` | Argon2id hashes. Roles: `client`, `agent`. |
| Video ownership, job state, worker leases | MongoDB `novaflow.videos` | The job state machine lives here. |
| Remediation proposals and decisions | MongoDB `novaflow.remediations` | Phase 8. Owner-scoped; conditional writes keyed on the record's state. Phase 9 added the worker claim, lease and verification evidence. |
| Replacement images | S3 `remediation-assets/{remediation_id}/asset.{ext}` | Phase 9. Owner-supplied, accepted only after sniffing the bytes. |
| Reconstructed output videos | S3 `remediated/{output_video_id}/output.mp4` | Phase 9. A **new** object with its own video id. Nothing overwrites `sources/`. |
| Source videos | S3 `sources/{video_id}/source.{ext}` | Server-generated keys; no client filename is used. |
| Extracted frames | S3 `frames/{video_id}/frame_NNNN.jpg` | 1 frame per second by default. |
| Agent reel uploads | S3 `raw/{project_id}/{agent}/reel.mp4` | Presigned PUT. |
| Frame metadata and detections | DynamoDB `NovaFlowFrames` | PK `video_id`, SK `frame_number`. |
| Projects and submissions | DynamoDB `NovaFlowProjects` | PK `project_id`, SK `sk`. |

MongoDB holds **no** video or frame bytes. An earlier parallel MongoDB/GridFS
pipeline existed; it was never reachable from the API and was removed.

### Pipeline

`POST /upload-reel` stores the video and records ownership — it does **not**
start analysis. `POST /analyze/{video_id}` queues a job, which then moves
`queued → extracting → analyzing → completed | failed`. A worker takes a lease
on the job and every subsequent write is conditional on that lease, so a
restarted or superseded worker cannot corrupt a newer attempt. A job is
`completed` only once results are persisted; an empty detections list means
nothing unless the state is `completed`.

A dead worker's lease lapses and the next recovery sweep requeues the job.
Retryable failures (a timeout, a throttle, an unreadable frame table) back off
and retry within a bounded attempt budget; deterministic failures fail
immediately rather than billing three times for the same answer.

Details: `docs/PIPELINE_ARCHITECTURE.md`. Operations: `docs/PIPELINE_OPERATIONS.md`.

### API surface

```
POST /auth/signup                  POST /upload-reel
POST /auth/login                   POST /analyze/{video_id}
GET  /auth/me                      POST /analyze/{video_id}/retry
                                   GET  /jobs/{video_id}
GET  /status                       GET  /violations/{video_id}
GET  /submissions                  GET  /uploads/{file}?access_token=...
POST /execute                      (6 routes under /projects/)
                                   (10 routes under /remediations/)

GET  /health/live                  liveness; touches no dependency
GET  /health/ready                 config + MongoDB; 503 when not serveable
GET  /health/worker                queue depth and abandoned-job count
```

`/health/live` deliberately checks nothing external - if it depended on
MongoDB, a brief outage would fail every replica's liveness probe and get them
all restarted, turning degradation into guaranteed downtime. `/health/worker`
is inferred from the queue and does not contact the worker process.

Every route that touches user data requires a JWT and checks ownership; a
missing ownership record is a denial, and unauthorized access returns 404 rather
than 403 so ids cannot be enumerated. Video playback uses a separate
short-lived media token bound to one video id, because a browser `<video>`
element cannot send an `Authorization` header.

`POST /execute` returns placeholder text — the browser-automation feature behind
it is not implemented and is awaiting a scope decision (`CLAUDE.md` §9).

`/remediations/*` is the remediation workflow: propose a change for a
persisted finding, read what it would change and on what basis, approve or
reject it, **supply the replacement image yourself**, have a new output video
built from it, and see what a fresh analysis **of that output** found.

**It still generates no image** — no provider is implemented, so
`POST /remediations/{id}/execute` on a proposal with no attached image answers
503 and leaves the record `approved`. **And it never modifies your original
upload**: an output is a separate object with its own video id under
`remediated/`. A remediation is reported as verified only after the output's
own analysis completed and the configured policy passed, which under the
default policy means *the targeted finding is gone* — not *the video is
compliant*. The decision record is `docs/REMEDIATION.md`.

---

## Setup

### Prerequisites

- **Python 3.11–3.14** (the Dockerfile targets 3.11)
- **Node.js 20+** and npm
- **FFmpeg** on `PATH` — a separate native binary. `pip install ffmpeg-python`
  provides only the Python wrapper; frame extraction shells out to the real
  `ffmpeg` executable. Verify with `ffmpeg -version`.
- **MongoDB** reachable at `MONGODB_URL`
- **AWS credentials** via the standard boto3 chain (IAM role, `aws configure`,
  or the conventional `AWS_*` variables) with access to S3, DynamoDB and
  Bedrock. Credentials are never read from the application's own variables.

### Configuration

```bash
cp .env.example .env     # then edit; .env is git-ignored, never commit it
```

`.env.example` is the authoritative list, documenting every variable, its
default, and whether it is required. The ones you cannot skip in production:

| Variable | Requirement |
| :-- | :-- |
| `JWT_SECRET_KEY` | **Required in production** (≥ 32 chars). Development generates an ephemeral per-process key, so sessions do not survive a restart. |
| `S3_BUCKET_NAME` | **Required in production.** Validated against the S3 naming rules at startup. |
| `NOVAFLOW_ENV` | Set to `production` to enforce the above. Defaults to `development`. |
| `CORS_ALLOWED_ORIGINS` | Set in production; `*` is rejected. |
| `MONGODB_URL` | The only MongoDB connection-string variable. It holds the job queue, so the worker needs it too. |
| `JOB_EXECUTION_MODE` | `worker` or `inline`. Defaults to `worker` in production. |

With `NOVAFLOW_ENV=production`, a missing `JWT_SECRET_KEY` or `S3_BUCKET_NAME`
raises `ConfigurationError` and the application refuses to start. That is
deliberate: the development bucket default (`novaflow-media`) is a placeholder
nobody owns, so accepting it in production would mean every upload failing later
with an opaque S3 error instead of a clear one at startup. **There is no
local-disk fallback for new uploads in any environment.**

### Provisioning AWS resources

`infra/cloudformation/storage.yaml` is the **only supported provisioning path**.
It creates the S3 bucket (public-access block, SSE-S3 encryption, versioning,
TLS-only bucket policy, lifecycle rules, optional access logging) and both
DynamoDB tables.

```bash
aws cloudformation deploy \
  --template-file infra/cloudformation/storage.yaml \
  --stack-name novaflow-storage \
  --parameter-overrides MediaBucketName=<globally-unique-name> Environment=staging
```

Each stack output names the environment variable to set it to
(`MediaBucketNameOut` → `S3_BUCKET_NAME`, and so on). Apply the identity policy
in `infra/iam_policy_least_privilege.json` alongside it — it is a **template**
whose `${PLACEHOLDER}` values must be substituted first.

> This template has never been deployed. It is validated structurally only (see
> below), so it is unverified against CloudFormation itself.

### Running

```bash
# API
cd backend
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app:app --host 0.0.0.0 --port 8000

# Worker - a second process. Required when JOB_EXECUTION_MODE=worker (the
# production default). In development the default is `inline`, where the API
# runs jobs itself, so you can skip this while getting started.
cd backend && python worker.py

# Frontend
cd frontend
npm install
npm run dev            # http://localhost:3000
```

| `JOB_EXECUTION_MODE` | Processes needed | Durable? |
| :-- | :-- | :-- |
| `inline` (dev default) | API only | No - killing the API strands the running job until a sweep requeues it |
| `worker` (prod default) | API **and** at least one worker | Yes - acceptance is a database write |

With `worker` mode and no worker running, uploads and submissions still
succeed and jobs accumulate in `queued`. `GET /health/worker` is how you see
that: `queued` climbing with `active: 0`.

Worker CLI: `python worker.py --once` drains what is claimable and exits
(cron-style); `--status` prints queue depth and the abandoned-job count.
SIGTERM/SIGINT finish the current job, then exit.

There is **no seed script**, so no users exist on a fresh database. Create one
through `POST /auth/signup`. Public signup always assigns the `client` role; the
`agent` role is granted only by an operator updating MongoDB directly, by design.

---

## Tests

```bash
# Backend
cd backend && pytest                      # 1111 passed, 55 skipped by default
pytest -m requires_mongodb                # needs MONGODB_TEST_URL
pytest -m requires_ffmpeg                 # needs ffmpeg on PATH

# Frontend
cd frontend && npm test                    # 387 tests (Vitest)
npm run lint && npm run typecheck && npm run build
```

Frontend: 387 tests pass, 0 type errors, **0 lint errors and 0 lint warnings**,
and the build emits `/`, `/_not-found` and `/login`. `fetch` is stubbed at the
network boundary in every frontend test, so they prove the UI renders the right
thing for a given API response — and nothing about real backend or AWS
behaviour. Run `npm run build` before `npm run typecheck` after deleting a
route: the typecheck reads generated validators under `.next/types/`.

There is no end-to-end test covering login → upload → analyze → review against
a live backend, and no browser-automation stack (Phase 10).

55 backend tests are **skipped by default** because they need real
dependencies: 26 require a MongoDB instance (`MONGODB_TEST_URL`, which must
name a disposable database — setup drops collections), 16 require the FFmpeg
binary for frame extraction, and 13 require it for video reconstruction.
**Skipped means unverified, not passing** — in particular, no video has ever
been reconstructed by real FFmpeg on the machine this was written on.

No test calls real AWS or incurs cost. Every external service is stubbed.

---

## Limitations

These are current and deliberate to state plainly.

- **Nothing has ever been run against real AWS or Bedrock.** Every external
  service is mocked in tests. **Detection accuracy is unmeasured** and no claim
  is made about it. S3, DynamoDB, Bedrock and the CloudFormation template are
  unverified against the live services.
- **Execution is a polling worker over MongoDB, not a queue service.** It is
  durable (acceptance is a database write) and recovery runs continuously while
  a worker is up, but it is **not at-most-once**: a job interrupted mid-analysis
  re-runs from the start, so Bedrock is billed again for frames already
  analysed. Delivery is polled, not pushed, so a job waits up to one poll
  interval. With every worker down, nothing sweeps until one starts or an
  operator runs `python backend/recover_jobs.py --apply`.
- **No backpressure.** Submissions are accepted regardless of queue depth;
  `/health/worker` exposes the depth but nothing throttles on it.
- **No worker has ever run against real infrastructure**, and no process has
  been SIGKILLed. Crash recovery is verified as logic - a lapsed lease and its
  conditional reclaim - not as observed behaviour under a real crash.
- **Rate limiting is per-process**, so the effective limit multiplies by worker
  count. There is no distributed limiter and no per-account throttle.
- **No token revocation.** Tokens are valid until expiry; rotating
  `JWT_SECRET_KEY` is the only break-glass.
- **Media tokens travel in URLs**, so they can reach browser history, `Referer`
  headers and proxy/CDN logs that this application cannot redact.
- **Frame sampling is 1 fps**, so a violation visible for under ~1 second can be
  missed.
- **There is no brand-rule configuration** — one static prompt in
  `backend/prompts.py`. No per-client policies, and no legal or regulatory
  evaluation.
- **Upload validation does not prove a file is a safe video.** There is no
  content scanning.
- **The dashboard no longer presents hardcoded data as live state.** Every
  fabricated panel was removed in Phase 7 — invented agents and cluster
  metrics, fabricated security "incidents", and an unconditional "Audit
  Passed" card that appeared whether or not analysis had run. Capabilities the
  backend lacks now say so on screen. Frame remediation and corrected-video
  reconstruction remain **not implemented**, and `POST /execute` still returns
  placeholder text, so no UI control is wired to it.
- **Remediation can now build a corrected video, from an image you supply.**
  Phase 9 resolved whether reconstruction is in scope
  (`docs/REMEDIATION.md` §9): an owner approves a proposal, uploads the
  replacement image, and the backend builds a **new** video with it and
  analyses that output in its own right. **No image has ever been generated by
  this system** and no code path can generate one — the Nova Canvas request
  schema could not be verified from the pinned SDK or anything else in the
  repository, and guessing a provider wire format is forbidden.
- **The original upload is never modified.** An output is a separate S3 object
  under its own video id; three independent mechanisms prevent a write to
  `sources/`, and a test asserts the source bytes are unchanged after a run.
- **A reconstruction is a concealment, not a repair.** The image is scaled to
  the approved region and drawn over it with hard edges — no inpainting, no
  grain matching, no motion tracking. The whole clip is re-encoded once by the
  video pass; audio is copied and therefore unaffected.
- **`verified` means the targeted finding is gone, not that the video is
  compliant.** It requires a completed analysis of the output with at least one
  audited frame, and under the default policy other findings in the output do
  not block it — they are recorded and shown separately. A failed, pending or
  zero-frame analysis is never a pass.
- **Reconstruction has never run against the real FFmpeg binary** on the
  machine this was written on: 13 integration tests exist and are **skipped**.
  Install FFmpeg and run `pytest -m requires_ffmpeg` before trusting the
  output.
- **Frontend views are not URL-addressable**, the audit detail view loads once
  rather than polling, and search and filtering run client-side over the list
  already fetched (`GET /submissions` caps at 100 records).
- The AWS IAM hardening is a reviewed template, not an applied configuration.

Building a corrected video and checking it — the most recent work — closes the
loop from a finding to an auditable, evidence-based verdict. It does **not**
make the application production-ready: nothing here has run against real AWS,
the infrastructure template has never been deployed, detection accuracy remains
unmeasured, no image can be generated automatically, and the reconstruction
code has never executed against the real FFmpeg binary.

---

## Repository layout

```
backend/            FastAPI service and the pipeline
backend/worker.py   the durable analysis worker (run as its own process)
backend/tests/      pytest suite
frontend/           Next.js dashboard (the live one)
infra/              CloudFormation storage template + IAM policy template
docs/               audit, roadmap, architecture and operations documents
analyzer_nova.py    standalone single-frame Nova Pro CLI
prompts.py          prompt used by analyzer_nova.py (backend/prompts.py is the
                    pipeline's own, and the two differ — see CLAUDE.md)
```

## Documentation

| Document | What it is |
| :-- | :-- |
| `CLAUDE.md` | Operational guide and current phase status. Start here. |
| `docs/PROJECT_AUDIT.md` | Evidence-based audit with file:line references. |
| `docs/IMPLEMENTATION_ROADMAP.md` | Phased plan to a releasable state. |
| `docs/PIPELINE_ARCHITECTURE.md` | Job state machine and pipeline stages. |
| `docs/PIPELINE_OPERATIONS.md` | Running, recovering and monitoring jobs. |
| `docs/SECURITY_AUDIT.md` | Security findings and what was fixed. |
| `docs/SECURITY_OPERATIONS.md` | Operator security procedures. |
| `docs/REMEDIATION.md` | The remediation scope decision, domain model, API and provider contract. |

`docs/planning.md` is an original design **target**, not a description of the
system; much of it is unimplemented.
