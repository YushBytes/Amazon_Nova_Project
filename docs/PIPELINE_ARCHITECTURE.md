# Pipeline Architecture

**NovaFlow / Brand Guardian — Phase 4**
**Last updated:** 2026-10-19

How a video gets from an upload to persisted, reviewable compliance results.

Everything below describes code that exists in this repository. Where a claim
is unverified against live infrastructure, it says so.

---

## 1. What changed in Phase 4

Before this phase, `POST /analyze/{video_id}` scheduled a FastAPI background
task, set a free-text `status` to `"ANALYZING"`, and returned. Nothing ever
recorded a terminal outcome, so three different situations were
indistinguishable to a client: still running, finished, and died.

Phase 4 adds a validated job state machine, persists every real milestone, and
makes the frontend read that state instead of inferring progress.

| Concern | Before | Now |
| :-- | :-- | :-- |
| Job state | free-text `status`, 2 values, never terminal | 6-state validated machine, always terminal |
| Extraction failure | silent `return None` | `ExtractionError` with a safe message |
| Frame cap | duration only | explicit `MAX_FRAMES_PER_VIDEO` |
| Model output | greedy regex, stored verbatim | Pydantic-validated, hallucinations dropped |
| Bedrock errors | single attempt | bounded retry with backoff, permanent errors fail fast |
| Results pagination | one query, silently truncated | cursor paginated |
| Progress | interim counts, no completion signal | real stages and counts from the job record |
| Retry | none | explicit, bounded by `MAX_ATTEMPTS` |

---

## 2. Stages

```
  CLIENT                    BACKEND                         STORES
  ──────                    ───────                         ──────

  POST /upload-reel ──────► validate extension/size/type
                            stream to disk (chunked)
                            record ownership + job  ───────► MongoDB  videos
                            state = uploaded                 (owner, job state)
         ◄──────────────── video_id + media token

  POST /analyze/{id} ─────► require_video_owner
                            pipeline.submit()
                            state = queued (durable)  ─────► MongoDB  videos
         ◄──────────────── job record
                            │
                            └─► BackgroundTasks: pipeline.run_job()
                                 │
                                 ├─ claim: queued -> extracting
                                 │  (conditional; a second worker is refused)
                                 │
                                 ├─ STAGE 1  extractor.extract_and_store()
                                 │    ffmpeg probe -> duration check
                                 │    ffmpeg fps filter -> MJPEG pipe
                                 │    per frame:  S3 put_object  ────────► S3
                                 │                DynamoDB put_item  ────► DynamoDB
                                 │                status=pending_audit      NovaFlowFrames
                                 │    -> ExtractionResult | ExtractionError
                                 │
                                 ├─ extracting -> analyzing (+ frames_extracted)
                                 │
                                 ├─ STAGE 2  analyzer.check_pending_frames()
                                 │    paginated scan for pending_audit
                                 │    per frame:  S3 get_object  ◄──────── S3
                                 │                Bedrock invoke_model
                                 │                validate -> FrameAnalysis
                                 │                DynamoDB update_item  ──► DynamoDB
                                 │                status=audited|audit_failed
                                 │    -> {pending, audited, failed, detections}
                                 │
                                 └─ STAGE 3  terminal state
                                      audited>0 and failed==0 -> completed
                                      otherwise               -> failed
                                                               ──────────► MongoDB

  GET /jobs/{id}  ────────► job_view(record)        authoritative state
  GET /violations/{id} ───► paginated detections + the job record
  POST /analyze/{id}/retry ► submit(allow_retry=True), attempt+1
```

---

## 3. State model

Defined in `backend/job_state.py`.

```
    UPLOADED ──────► QUEUED ──────► EXTRACTING ──────► ANALYZING
       │                │               │                  │
       │                │               │                  ├──► COMPLETED
       │                ▼               ▼                  ▼
       └──────────────► FAILED ◄────────┴──────────────────┘
                          │
                          └──► QUEUED        (explicit, bounded retry)
```

| State | Meaning |
| :-- | :-- |
| `uploaded` | File stored, ownership recorded, analysis not requested |
| `queued` | Analysis accepted; the worker has not begun |
| `extracting` | Frames are being extracted |
| `analyzing` | Extracted frames are being sent to the model |
| `completed` | Every extracted frame was audited AND the summary was persisted |
| `failed` | A stage failed; `error_summary` says which, in safe terms |

Properties enforced by the machine and covered by tests:

* **`completed` is reachable only from `analyzing`.** Scheduling work cannot
  produce it. `COMPLETED` is also written only after the result summary
  persists — if that write fails, the job becomes `failed`, because the stored
  state is what a client reads.
* **Every non-terminal state can reach `failed`**, so nothing is stranded.
* **Terminal states re-enter only via `queued`**, through an explicit retry
  that increments `attempt` and clears the previous `error_summary`.
* **A partial audit is a failure.** If any frame failed, the job is `failed`
  even though some frames succeeded. Reporting it complete would let the UI
  show "compliant" for footage that was only partly analysed.
* **Transitions are conditional** on the state we validated against, so two
  workers cannot both advance one job.
* **Unknown or absent stored status coerces to `uploaded`**, so records written
  before Phase 4 stay readable.

### Progress is stages, not percentages

`job.stage` is a named milestone. No percentage is reported, because the frame
total is unknown until extraction finishes — any earlier percentage would be
invented. Real counts (`frames_extracted`, `frames_audited`, `frames_failed`,
`detections_recorded`) appear as soon as they are known.

---

## 4. Storage

Three stores, each holding what it is suited to. Unchanged from earlier phases
except that job state now lives beside ownership.

### MongoDB — accounts, ownership, job state

Collection `videos`, one document per video:

| Field | Type | Notes |
| :-- | :-- | :-- |
| `video_id` | string | **unique index**; server-generated `vid_<8 hex>` |
| `owner` | string | **index**; username from the token at upload |
| `stored_filename` | string | server-generated; never client-supplied |
| `original_filename` | string | display metadata only |
| `extension`, `size_bytes` | string, int | |
| `status` | string | the job state |
| `attempt`, `max_attempts` | int | retry budget |
| `frames_extracted`, `frames_audited`, `frames_failed` | int \| null | real counts |
| `detections_recorded` | int \| null | |
| `frames_truncated` | bool | true when the frame cap was hit |
| `duration_seconds`, `sample_fps` | number | |
| `error_summary`, `failed_stage` | string \| null | safe text, shown to the owner |
| `model_id`, `pipeline_version` | string \| null | traceability |
| `created_at`, `updated_at`, `started_at`, `completed_at` | UTC datetime | |

**Why job state is here and not in a second DynamoDB table:** ownership and job
state belong to the same aggregate, so one conditional update covers both and
they cannot disagree; the record already carried a `status` field; and no new
table means no new IaC and no operator provisioning step. Frame data stays in
DynamoDB and S3 — that split is unchanged.

Indexes are created by `db_init.initialize_database()` at startup, which is
duplicate-aware and non-destructive.

### DynamoDB — frame metadata and detections

Table from `DYNAMODB_TABLE_NAME` (default `NovaFlowFrames`):

| Attribute | Type | Role |
| :-- | :-- | :-- |
| `video_id` | S | **partition key** |
| `frame_number` | N | **sort key** |
| `timestamp` | S | seconds into the source video (`frame_number / fps`) |
| `s3_key`, `s3_bucket` | S | where the frame image lives |
| `status` | S | `pending_audit` → `audited` \| `audit_failed` |
| `detections` | L | validated detection maps; **empty for a failed frame** |
| `analysis_report` | S | JSON summary: counts, scene description, recommendation |
| `model_id`, `pipeline_version` | S | traceability |
| `created_at`, `updated_at` | S | ISO-8601 UTC |

A detection map:

```
{ category: S, description: S,
  risk_level: S            (only when the model gave a known level)
  confidence: N            (only when the model gave a number in [0,1])
  bounding_box: [N,N,N,N]  (only when the model gave a valid 0-1000 box) }
```

Optional fields are **omitted entirely** when the model did not supply them, so
a reader can tell "no confidence given" from "confidence zero".

> **No IaC defines this table.** Required schema: partition key `video_id` (S),
> sort key `frame_number` (N). No TTL is required or used; frames persist until
> deleted by an operator. `NovaFlowProjects` needs partition `project_id` (S),
> sort `sk` (S). See `docs/PIPELINE_OPERATIONS.md`.

### S3 — frame images

| Key pattern | Written by | Read by |
| :-- | :-- | :-- |
| `frames/{video_id}/frame_NNNN.jpg` | `extractor._store_frame` | `analyzer.check_pending_frames` |
| `raw/{project_id}/{username}/reel.mp4` | presigned PUT in `projects.py` | nothing (dead path, pre-existing) |

Keys are entirely server-generated. The client filename contributes only a
validated extension, never a path fragment.

Source videos themselves currently live on the **application host's local
disk** under `uploads/`, not in S3 — that is pre-existing behaviour, unchanged
here, and it is a durability limitation (see Operations §limitations).

---

## 5. Frame sampling policy and its limits

Configured in `backend/config.py`, implemented in `backend/extractor.py`.

| Setting | Default | Effect |
| :-- | :-- | :-- |
| `FRAME_SAMPLE_FPS` | 1 | frames sampled per second of video |
| `MAX_VIDEO_DURATION_SECONDS` | 60 | clips longer than this are refused |
| `MAX_FRAMES_PER_VIDEO` | 120 | hard cap; extraction truncates and flags it |
| `FFMPEG_TIMEOUT_SECONDS` | 300 | wall-clock limit for one FFmpeg run |

Stated plainly:

* **A violation visible for less than one sampling interval can be missed.** At
  1 fps that is anything under about a second. This is a sampling pipeline, not
  exhaustive frame-by-frame review.
* **Timestamps are sampling times** (`frame_number / fps`), not
  container-reported presentation timestamps. Accurate for constant-rate
  sampling, and it is what the UI uses to seek.
* **Sampling is uniform.** No scene detection or keyframe selection.
* **The cap bounds cost.** Duration × fps determines how many Bedrock calls one
  upload makes; the cap is the backstop when a malformed container
  under-reports duration.
* **Truncation is recorded**, not hidden: `frames_truncated` is stored and the
  extraction summary says so.

---

## 6. Nova Pro request and response contract

Model: **`amazon.nova-pro-v1:0`**, a named constant in `backend/analyzer.py`.
It is deliberately **not** environment-configurable: silently pointing it at a
different model would invalidate every stored result without the pipeline
version changing.

### Request

`invoke_model` with Nova's native message format:

```
{ "schemaVersion": "messages-v1",
  "messages": [{ "role": "user", "content": [
      { "image": { "format": "jpeg", "source": { "bytes": "<base64>" } } },
      { "text": "Analyze this video frame for brand-compliance violations..." }
  ]}],
  "system": [{ "text": SYSTEM_PROMPT }],
  "inferenceConfig": { "maxNewTokens": 1000 } }
```

### Expected response

Per `backend/prompts.py`:

```
{ "scene_description": "...",
  "violations": [ { "category": "...", "description": "...",
                    "confidence": 0.0, "bounding_box": [ymin,xmin,ymax,xmax],
                    "risk_level": "High|Medium|Low" } ],
  "recommendation": "..." }
```

Coordinates are normalized 0–1000.

### Validation — the response is untrusted input

`backend/detections.py` treats model output as hostile:

| Input | Behaviour |
| :-- | :-- |
| Valid JSON object | parsed into `FrameAnalysis` |
| Multiple JSON objects, or prose around one | balanced, string-aware scan picks the analysis-shaped object. The old greedy `{.*}` regex spanned the first `{` to the last `}` and produced invalid JSON |
| Fenced code block | unwrapped |
| No usable JSON | `AnalysisParseError` → frame marked `audit_failed`, **not** "clean" |
| `violations` not a list | parse error |
| Entry with no category | discarded, counted in `discarded_entries` |
| Inverted, out-of-range, short, or non-numeric box | box dropped, **detection kept** |
| Confidence outside [0,1] | dropped; a value in (1,100] is rescaled; words like "high" rejected |
| Unknown `risk_level` | set to `null` rather than guessed |

Nothing is defaulted into existence. A fabricated `0.0` confidence would be
worse than an absent one.

### Error handling

| Error | Behaviour |
| :-- | :-- |
| `ThrottlingException`, `ServiceUnavailable`, `InternalServerException`, `ModelTimeout`, `ModelNotReady` | retried, exponential backoff, up to `BEDROCK_MAX_ATTEMPTS` (3) |
| Transport (`BotoCoreError`) | retried the same way |
| `ValidationException`, `AccessDenied`, `ResourceNotFound`, `UnrecognizedClient` | **not retried** — permanent, fails on attempt 1 |
| Malformed response | **not retried** — another call will not make the model answer differently |

Only the error **code** is logged. A Bedrock error body can echo the request,
which contains frame data.

### Brand rules — an honest limitation

There is **no persistent brand-rule configuration** in this project. The
compliance contract is the single static prompt in `backend/prompts.py`, which
asks for competitor logos, unsafe items, offensive text, and prohibited brand
activities. Per-client rule sets, configurable policies, and any claim to
evaluate legal or regulatory requirements are **not implemented**, and the
pipeline does not pretend otherwise.

---

## 7. API contract

All routes require a bearer token and enforce ownership, except the three
public ones. Cross-user access returns **404**, never 403, so ids cannot be
enumerated.

| Method | Path | Purpose |
| :-- | :-- | :-- |
| POST | `/upload-reel` | Store a video, record ownership, create the job (`uploaded`) |
| POST | `/analyze/{video_id}` | Queue analysis. **409** if already running, **429** if the attempt budget is spent |
| GET | `/jobs/{video_id}` | **Authoritative** job state |
| POST | `/analyze/{video_id}/retry` | Re-queue a terminal job |
| GET | `/violations/{video_id}` | Paginated detections **plus** the job record |
| GET | `/submissions` | The caller's own videos |
| GET | `/uploads/{file}` | Owner-only playback, scoped media token |

### `GET /jobs/{video_id}`

```
{ video_id, state, stage, is_terminal, is_active,
  attempt, max_attempts,
  frames_extracted, frames_audited, frames_failed, detections_recorded,
  error_summary, model_id, pipeline_version,
  original_filename, size_bytes,
  created_at, updated_at, completed_at }
```

`owner` and `stored_filename` are deliberately **not** projected.

### `GET /violations/{video_id}?cursor=&limit=`

```
{ video_id, detections[], frame_count, audited_frames, failed_frames,
  next_cursor, has_more, job }
```

`frame_count` is frames in **this page**; `job.frames_extracted` is the total.
`cursor` is an opaque `frame_number`. **A client must not treat an empty
`detections` array as "compliant" unless `job.state === "completed"`.**

Status codes: `200` ok · `400` bad cursor · `401` no/invalid token · `404`
unknown **or** not yours · `409` already running / not retryable · `429`
throttled or attempts exhausted · `502` analysis store unavailable · `500`
generic, never an exception string.

---

## 8. Job execution: the durable worker

**Phase 6 replaced in-process execution.** Sections 8.1–8.5 describe what runs
now; 8.6 records the architecture that was rejected and why.

### 8.1 Two processes

```
POST /analyze/{id}
      │
      │  1. validate + ownership check
      │  2. persist status=queued          <-- durable HERE, before the response
      ▼
   202-style response {video_id, status: "queued", job: {...}}

   ... the API's involvement ends ...

python backend/worker.py
      │  poll   find_queued_jobs()         indexed (status, updated_at)
      │  claim  claim() conditional update + claim_id + lease
      │  run    extract -> analyse -> persist
      ▼  sweep  recover_abandoned_jobs() every WORKER_RECOVERY_INTERVAL_SECONDS
```

The API no longer executes analysis. `JOB_EXECUTION_MODE=worker` (the
production default) makes the API queue-and-return; `inline` keeps the Phase 4
in-process behaviour for single-process local development and the test suite.
**The HTTP response shape is identical in both modes** — a test asserts this,
so the frontend cannot tell which is configured.

### 8.2 The queue is the job record

There is no SQS, no Celery, no Redis. The MongoDB `videos` collection was
already a durable work queue before this phase:

| Queue concept | How it already exists |
| :-- | :-- |
| Durable enqueue | `pipeline.submit()` persists `queued` **before** the API responds |
| Exactly-one receive | `video_repo.claim()` — a conditional update; the loser matches zero documents |
| Visibility timeout | `lease_expires_at`, extended by `heartbeat()` |
| Fencing | `claim_id`, required by every later write — *stronger* than a queue offers: a superseded worker's writes are **rejected**, not merely duplicated |
| Redrive policy | `attempt` against `MAX_ATTEMPTS` |
| Dead-letter | the `failed` state, queryable in the same store |
| Backoff | `not_before`, set by `schedule_retry()` |

So durability did not need to be added — it needed a process to consume it.

### 8.3 What this guarantees

* **An accepted job survives an API restart.** Acceptance is a database write,
  not an in-memory handoff, so there is nothing to lose. A worker started later
  — which has never seen the request — picks it up.
* **Duplicate execution cannot corrupt a result.** A terminal job is not
  re-claimable, and a re-run overwrites frame rows keyed
  `(video_id, frame_number)` rather than appending. `_prune_stale_frames`
  removes rows a *longer* previous attempt left behind, so a shorter re-run
  cannot report a stale frame as current.
* **A dead worker's job is recovered, continuously.** Its lease lapses and the
  next sweep requeues it. Recovery now runs inside every worker on an interval,
  not only at API startup.
* **Retries are bounded and classified.** Only failures that could plausibly
  succeed later are retried — a transport timeout, a throttle, an unreadable
  frame table. Deterministic failures (a missing object, an over-size object,
  an unreadable video) fail immediately rather than billing three times for the
  same answer. `source_store` carries an explicit `retryable` flag set at each
  raise site, not inferred from the message.
* **A graceful stop is graceful.** SIGTERM/SIGINT finish the current job, then
  exit. A hard kill leaves the lease to lapse; the job is requeued, never
  reported complete.

### 8.4 What it does NOT guarantee

* **Not at-most-once.** A job interrupted mid-analysis is re-run from the
  start. Safe because frame writes are idempotent, but Bedrock is billed again
  for the frames already analysed.
* **Not push delivery.** The worker polls (default 5s), so a job waits up to
  one poll interval. One poll is a single indexed query.
* **Not multi-host fairness** beyond "oldest `queued` first".
* **No backpressure.** Submissions are accepted regardless of queue depth.
  `GET /health/worker` exposes the depth; nothing throttles on it.
* **One job at a time per worker.** Concurrency is per-process — run more
  workers — so the cost stays visible to whoever provisions them.

### 8.5 Timeouts

Every boto3 client is built by `backend/aws_clients.py` with a finite connect
and read timeout and a bounded retry budget. This is load-bearing for the lease
model: a call that could hang forever would keep a dead-in-the-water worker
heartbeating, so its job would never look abandoned and would never be
recovered.

⚠️ **The two retry budgets multiply.** `AWS_MAX_ATTEMPTS` (botocore, per API
call) × `BEDROCK_MAX_ATTEMPTS` (the analyzer, per frame) is the worst case per
frame, and every attempt is billed. Raise one, not both.

### 8.6 Why not SQS

SQS was the obvious candidate and was rejected for this phase:

1. **It would add a second source of truth for pending work.** A message whose
   job is already `completed`, or a `queued` job whose message was lost, is a
   divergence the current design cannot have — there is one record.
2. **It duplicates what leases already do.** Visibility timeout and redrive
   restate `lease_expires_at` and `attempt`, and SQS has no equivalent of the
   `claim_id` fencing token.
3. **It needs a deployment-target decision the project has not made** —
   `CLAUDE.md` §9 question 9. The worker as built runs anywhere the API image
   runs.
4. **It could not be tested deterministically** without real AWS or a new local
   dependency. Every guarantee in §8.3 is covered by a test today.

SQS becomes the right answer when there are multiple worker hosts and
throughput matters more than simplicity. At that point the changes are
localised: `find_queued_jobs` and the worker's poll are the only things that
move, and `infra/iam_policy_least_privilege.json` records which permissions
would be added.

---

## 9. Security carried forward

Phase 3 and 3.5 controls are intact and still tested:

* every pipeline route authenticates and checks ownership; cross-user access is
  denied and returns 404;
* `video_id` is validated against `^vid_[0-9a-f]{8}$` before any lookup;
* storage keys and filenames are entirely server-generated;
* upload extension allowlist, streaming size limit, empty-file rejection;
* rate limiting on the credential endpoints;
* media tokens scoped to one video, redacted from this process's logs;
* `error_summary` is written by the pipeline as safe text — never an exception
  string, bucket name, table name, or path. Tests assert that an induced
  failure carrying `s3://secret-bucket/...` does not reach the client;
* model output is validated before persistence and before render.

---

## 10. What is verified, and what is not

| | Status |
| :-- | :-- |
| Orchestration, state machine, persistence calls, API contract | **Verified** by 500+ backend tests with S3, DynamoDB, Bedrock, MongoDB and FFmpeg mocked |
| Frontend polling, terminal detection, results, retry | **Verified** by 74 frontend tests with `fetch` stubbed |
| Live S3 / DynamoDB writes | **Not verified.** No AWS call was made |
| Live Bedrock inference and detection quality | **Not verified.** No model was invoked. Nothing here says anything about accuracy |
| Real FFmpeg extraction | **Not verified.** The binary is absent on this machine; one optional test is marked `requires_ffmpeg` |
| Real MongoDB | **Not verified.** 13 tests marked `requires_mongodb` skip |
| Deployment | **Not performed** |

A mocked pipeline test proves the orchestration is correct. It is not evidence
that live AWS or Bedrock integration works.

---

*See `docs/PIPELINE_OPERATIONS.md` for running, configuring, and verifying this
pipeline, and `docs/SECURITY_AUDIT.md` for the security posture.*
