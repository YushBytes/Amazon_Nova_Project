# Pipeline Operations

**NovaFlow / Brand Guardian — Phase 4**
**Last updated:** 2026-10-19

Running, configuring, and verifying the video compliance pipeline. For how it
works, see `docs/PIPELINE_ARCHITECTURE.md`. For security procedures, see
`docs/SECURITY_OPERATIONS.md`.

No real secret, account id, bucket name, or table ARN appears here.

---

## 1. Local test setup

Nothing below requires AWS, MongoDB, or FFmpeg. All external services are
mocked in the default test run.

```bash
# Backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt \
                                      -r backend/requirements-dev.txt
cd backend && ../.venv/Scripts/python.exe -m pytest -q

# Frontend
cd frontend && npm ci
npm test && npm run typecheck && npm run lint && npm run build
```

Optional suites, skipped unless their service is present:

```bash
pytest -m requires_ffmpeg     # needs the ffmpeg/ffprobe binaries on PATH
pytest -m requires_mongodb    # needs MONGODB_TEST_URL (a disposable database)
pytest -m requires_aws        # not used by any current test
```

### Running the app locally

```bash
# 1. API. Leave JWT_SECRET_KEY unset in development: an ephemeral per-process
#    key is generated, so sessions end at restart.
cd backend && ../.venv/Scripts/python.exe -m uvicorn app:app --reload --port 8000

# 2. Worker — a SECOND terminal. Only needed when JOB_EXECUTION_MODE=worker.
#    In development the default is `inline`, so the API runs jobs itself and
#    you can skip this while getting started.
cd backend && ../.venv/Scripts/python.exe worker.py

# 3. Frontend
cd frontend && npm run dev
```

Which processes you need depends on `JOB_EXECUTION_MODE`:

| Mode | Processes | Durable? |
| :-- | :-- | :-- |
| `inline` (dev default) | API only | No — killing the API strands the running job until a sweep |
| `worker` (prod default) | API **and** at least one worker | Yes — acceptance is a database write |

The pipeline needs real AWS and FFmpeg to actually process a video. Without
them, upload and job registration work, and analysis fails with a truthful
terminal state — which is itself worth seeing.

### Health probes

```bash
curl localhost:8000/health/live     # process is up; touches no dependency
curl localhost:8000/health/ready    # config + MongoDB; 503 when not serveable
curl localhost:8000/health/worker   # queue depth and abandoned count
```

`/health/live` deliberately checks nothing external: if it depended on
MongoDB, a brief outage would fail every replica's liveness probe and get them
all restarted, turning degradation into guaranteed downtime. Use
`/health/ready` to decide whether to send traffic.

`/health/worker` is **inferred from the queue** — it does not contact the
worker process, which may be on another host.

---

## 2. Environment variables

Full list with placeholders in `.env.example`. Pipeline-specific:

| Variable | Default | What it bounds |
| :-- | :-- | :-- |
| `MAX_VIDEO_DURATION_SECONDS` | 60 | Longest clip accepted |
| `FRAME_SAMPLE_FPS` | 1 | Frames sampled per second |
| `MAX_FRAMES_PER_VIDEO` | 120 | Hard frame cap (cost backstop) |
| `FFMPEG_TIMEOUT_SECONDS` | 300 | One FFmpeg run |
| `BEDROCK_MAX_ATTEMPTS` | 3 | Retries per frame, retryable errors only |
| `MAX_UPLOAD_BYTES` | 104857600 | Upload size |
| `ALLOWED_VIDEO_EXTENSIONS` | mp4,mov,avi,webm,mkv | Accepted extensions |
| `S3_BUCKET_NAME` | novaflow-media | Frame storage |
| `DYNAMODB_TABLE_NAME` | NovaFlowFrames | Frame metadata and results |
| `AWS_REGION` | us-east-1 | Bedrock, S3, DynamoDB |

**Cost control.** Duration × fps sets how many Bedrock calls one upload makes.
At the defaults, a 60-second clip costs up to 60 model invocations. Lower
`FRAME_SAMPLE_FPS` or `MAX_VIDEO_DURATION_SECONDS` to reduce spend; raising
them raises cost linearly. There is no per-user quota.

The model id is **not** an environment variable. It is a constant in
`backend/analyzer.py`, because silently switching models would invalidate
stored results without the pipeline version changing.

---

## 3. Required AWS resources

### DynamoDB tables — no IaC creates these

| Table | Partition key | Sort key | Notes |
| :-- | :-- | :-- | :-- |
| `${DYNAMODB_TABLE_NAME}` (default `NovaFlowFrames`) | `video_id` (S) | `frame_number` (N) | Frame metadata and detections |
| `NovaFlowProjects` | `project_id` (S) | `sk` (S) | Pre-existing; name hardcoded in `projects.py` |

No TTL is required or used — frames persist until an operator removes them. If
you want automatic expiry, add a TTL attribute and a lifecycle decision; the
code does not write one.

Enable encryption at rest, and point-in-time recovery if the data matters.

### S3 bucket

One bucket, `${S3_BUCKET_NAME}`, with two prefixes in use:

| Prefix | Contents |
| :-- | :-- |
| `sources/{video_id}/` | Uploaded source videos (Phase 4.1) |
| `frames/{video_id}/` | Extracted frame JPEGs |
| `raw/{project_id}/{user}/` | Presigned project uploads (dead path, nothing reads it) |

Provision with `infra/cloudformation/storage.yaml`, which is the only supported
provisioning path and already expresses the hardening listed in
`docs/SECURITY_OPERATIONS.md` §7.4 — public-access block, encryption,
versioning, TLS-only bucket policy, optional access logging. Phase 5 deleted
the legacy `infra/init_s3.py`, which applied none of it and created
differently-named buckets. Set `S3_BUCKET_NAME` to the stack's
`MediaBucketNameOut` output; it is **required** when `NOVAFLOW_ENV=production`
and the application refuses to start without it.

### Bedrock

Grant model access for **`amazon.nova-pro-v1:0`** in `AWS_REGION`. Bedrock
requires explicit per-model opt-in; without it every frame fails with
`AccessDeniedException`, which the pipeline correctly records as a terminal
failure rather than a clean audit.

### IAM

Use `infra/iam_policy_least_privilege.json`. Phase 4 adds **no new AWS
actions** — the verified set is unchanged:

* Bedrock: `InvokeModel` on the Nova Pro model only
* S3: `GetObject`, `PutObject` on `frames/*` and `raw/*`; `ListBucket` by prefix
* DynamoDB: `PutItem`, `GetItem`, `UpdateItem`, `Query`, `Scan`

`Scan` is still required: `analyzer.check_pending_frames` scans for
`pending_audit` frames. Replacing it with a GSI on `status` would let you drop
`Scan`, and is a sensible later change.

### FFmpeg

The `ffmpeg` binary must be on `PATH` in the runtime. `backend/Dockerfile`
installs it via `apt-get`. Without it, extraction fails with
"Video processing is unavailable on this server (FFmpeg not found)" and the job
becomes `failed` — correct, but no video can be analysed.

---

## 4. Job states an operator will see

| State | Meaning | Action |
| :-- | :-- | :-- |
| `uploaded` | Stored, not submitted | None; the user starts it |
| `queued` | Accepted, worker not started | Should move within seconds |
| `extracting` | FFmpeg running | Bounded by `FFMPEG_TIMEOUT_SECONDS` |
| `analyzing` | Frames going to Bedrock | Roughly frames × per-call latency |
| `completed` | Every frame audited, summary persisted | None |
| `failed` | See `error_summary` | §5 |

```javascript
// Current state of one video
db.videos.findOne({video_id: "<id>"},
  {video_id:1, status:1, attempt:1, frames_extracted:1, frames_audited:1,
   frames_failed:1, detections_recorded:1, error_summary:1, failed_stage:1})

// How many jobs are in each state
db.videos.aggregate([{$group: {_id: "$status", count: {$sum: 1}}}])
```

---

## 5. Diagnosing a failed job

`error_summary` is written to be shown to the owner, so it never contains an
exception string, bucket name, or path. `failed_stage` says where it broke.

| Summary | Cause | Fix |
| :-- | :-- | :-- |
| `Video processing is unavailable on this server (FFmpeg not found).` | binary missing | install FFmpeg in the runtime |
| `The file could not be read as a video...` | corrupt or unsupported container | user re-exports the clip |
| `The file contains no video stream.` | audio-only or wrong file | user uploads a video |
| `The video is N s long; the limit for analysis is M s.` | over duration | raise `MAX_VIDEO_DURATION_SECONDS` (raises cost) or user trims |
| `No frames could be extracted from the video.` | decoded to nothing | inspect the file; check server logs for FFmpeg stderr |
| `The video could not be decoded.` | FFmpeg non-zero exit | server logs carry the stderr tail |
| `Video processing took too long and was stopped.` | FFmpeg timeout | raise `FFMPEG_TIMEOUT_SECONDS`, or the clip is pathological |
| `Frame storage is currently unavailable.` | S3 put failed | check credentials, bucket, IAM |
| `The analysis store is unavailable.` | DynamoDB scan failed | check the table exists and IAM allows `Scan` |
| `No frames could be analysed.` | every frame failed Bedrock | **usually missing model access**; check server logs for the error code |
| `N of M frames could not be analysed.` | partial failure, often throttling | retry; consider lowering fps |
| `Results could not be saved.` | MongoDB write failed after a successful audit | check MongoDB; retry |

The precise Bedrock error code is in the **server logs**, not the job summary —
deliberately, since provider messages can echo request content.

```bash
grep -iE "Bedrock .* failed with|Rate limit exceeded|FFmpeg exited" <log>
```

---

## 6. Running the worker, and recovering stuck jobs

**Phase 6 changed this section materially.** Analysis no longer runs inside the
API process, and recovery is no longer startup-only.

### 6.1 Start a worker

```bash
cd backend
python worker.py                  # run until stopped (SIGTERM/SIGINT = graceful)
python worker.py --once           # drain what is claimable, then exit (cron-style)
python worker.py --status         # queue depth + abandoned count, then exit
```

Requires `MONGODB_URL` (the queue lives there) and the same AWS credentials and
`S3_BUCKET_NAME` as the API. Run one or more; they coordinate through
conditional claims, so extra workers are always safe to add.

```bash
# Is anything consuming the queue?
curl -s localhost:8000/health/worker | python -m json.tool
```

⚠️ **With `JOB_EXECUTION_MODE=worker` and no worker running, submissions still
succeed and jobs accumulate in `queued`.** That is the most likely
misconfiguration in a new deployment. `GET /health/worker` is how you see it:
`queued` climbing with `active: 0`.

### 6.2 How a dead worker's job comes back

A worker heartbeats while it works, so a lapsed `lease_expires_at` means the
process holding the job is gone. Three things sweep for those:

| Mechanism | Frequency | Configure with |
| :-- | :-- | :-- |
| Inside every worker | every 60s | `WORKER_RECOVERY_INTERVAL_SECONDS` (0 disables) |
| API startup | once per boot | `RECOVER_JOBS_ON_STARTUP` |
| Operator / scheduler | on demand | `python backend/recover_jobs.py --apply` |

A sweep returns the job to `queued` (incrementing `attempt`) or, once the
attempt budget is spent, moves it to `failed` with a truthful summary. It never
touches a job whose lease is still live, and two concurrent sweeps cannot both
reclaim one job — the write is conditional on the stale claim.

**Recovery is therefore continuous while at least one worker is up**, which is
the honest scope: if every worker is down, nothing sweeps until one starts or
an operator runs the CLI. This is no longer "a job stays stuck forever", which
is what Phases 4–4.1 had to say here.

### 6.3 Find jobs that look wrong

```bash
cd backend
python worker.py --status          # counts by state + abandoned count

# The long-horizon view: non-terminal with no update for 30 minutes,
# including jobs whose lease is still live but which are making no progress.
python -c "
import anyio, pipeline
for r in anyio.run(pipeline.find_stuck_jobs):
    print(r['video_id'], r['status'], r.get('updated_at'))
"
```

`find_stuck_jobs` **only reports**. Nothing is failed automatically, because a
slow legitimate run must not be killed.

### 6.4 A job waiting on a retry backoff

A retryable failure requeues the job with `not_before` set, so it is `queued`
but deliberately not yet claimable. That is **not** stuck:

```javascript
db.videos.findOne({video_id: "<id>"}, {status: 1, attempt: 1, not_before: 1,
                                       last_retry_reason: 1})
```

A future `not_before` with a `last_retry_reason` is a job correctly waiting.
The recovery sweep skips these, so one failure cannot increment `attempt`
twice.

### 6.5 Manual intervention, when the budget is spent

```
POST /analyze/{video_id}/retry      # as the owner; bounded by MAX_ATTEMPTS
```

If attempts are exhausted and the job genuinely is orphaned:

```javascript
// Deliberate operator action. Confirm it really is orphaned first:
// check that no worker holds a live lease.
db.videos.updateOne(
  {video_id: "<id>", status: {$in: ["queued","extracting","analyzing"]}},
  {$set: {status: "failed", error_summary: "Abandoned by a server restart.",
          failed_stage: "worker", completed_at: new Date(),
          claim_id: null, lease_expires_at: null}}
)
```

**Do not** set a job to `completed` by hand. That state means "analysis
finished and results were persisted"; asserting it without the results makes
the UI report compliance that was never established.

**Do not** clear `claim_id` on a job a live worker is running — that is exactly
what the fencing token prevents, and doing it by hand lets two attempts write
to one job.

---

## 7. Retries and idempotency

* Retry is explicit: `POST /analyze/{id}/retry`, owner only.
* Bounded by `MAX_ATTEMPTS` (3, in `backend/job_state.py`). Exhausted retries
  return **429**, so a persistently failing video cannot keep spending model
  capacity.
* Re-queuing increments `attempt` and clears `error_summary`, so a stale
  failure is never read as current.
* Frame results are written with `update_item` keyed on
  (`video_id`, `frame_number`), so a re-run **overwrites** rather than
  appending. Counts are replaced, not summed — covered by a test.
* A duplicate submission while a run is in flight returns **409**; the
  `queued → extracting` claim is conditional, so a second worker is refused.

Re-running a completed job re-extracts and re-analyses everything, and costs
the same Bedrock spend again.

---

## 8. Staging smoke test

Needs real credentials, a real database, and FFmpeg. **None of this was
performed while writing this document.**

Prerequisites: everything in §3, plus the security prerequisites in
`docs/SECURITY_OPERATIONS.md` §9.1.

| Step | Expect |
| :-- | :-- |
| `GET /status` | 200 |
| Sign up, then sign in | 200 with a token |
| `POST /upload-reel` with a short real `.mp4` | 200; `filename` matches `vid_[0-9a-f]{8}\.mp4` |
| `GET /jobs/{id}` | `state: "uploaded"`, `is_active: false` |
| `POST /analyze/{id}` | 200, `job.state: "queued"` |
| `POST /analyze/{id}` again, immediately | **409** |
| Poll `GET /jobs/{id}` | `queued` → `extracting` → `analyzing` → `completed` |
| At `completed` | `frames_extracted` > 0, `frames_audited == frames_extracted`, `frames_failed == 0`, `completed_at` set, `model_id` is the Nova Pro id |
| `GET /violations/{id}` | detections with real timestamps; `job.state == "completed"` |
| S3 | objects under `frames/{video_id}/` |
| DynamoDB | one item per frame, `status: "audited"` |
| Second account requests the first's `video_id` | **404** on `/jobs`, `/violations`, `/analyze` |
| Upload a `.txt` renamed `.mp4` | job reaches `failed` with a decode message, **not** `completed` |
| Upload a clip over the duration limit | `failed`, summary names both lengths |

Then confirm nothing leaked:

```bash
grep -iE "access_token=ey|\$argon2|AKIA|s3://" <staging log>   # expect nothing
```

**Frontend:** upload → Launch Audit shows real stages; the status strip says
"Audit complete" only after the backend reports `completed`; a failed job shows
the backend summary and offers Retry; polling stops at the terminal state.

---

## 9. Monitoring

No metrics are emitted; there is no dashboard. What exists today is logs and
the database. Useful log lines:

| Pattern | Meaning |
| :-- | :-- |
| `Pipeline starting for` / `Pipeline complete for` | run boundaries |
| `Job .* failed during (extraction\|analysis\|persistence)` | stage failure |
| `Extraction complete for .*: N frame(s)` | extraction result |
| `Bedrock .* on attempt N/M; retrying` | throttling |
| `Bedrock call failed with <Code>` | permanent model error |
| `Stopped scanning pending frames after N page(s)` | scan hit its page bound |
| `Rate limit exceeded for` | credential endpoint throttled |

Worth watching: jobs stuck non-terminal (§6), the `failed` rate by
`failed_stage`, Bedrock throttling frequency, and disk use under `uploads/`
(nothing prunes it).

---

## 10. Known limitations

Stated plainly so none is mistaken for solved.

| Limitation | Impact |
| :-- | :-- |
| **Polling, not push** | A job waits up to one poll interval (default 5s) before a worker claims it |
| **Not at-most-once** | A job interrupted mid-analysis re-runs from the start. Frame writes are idempotent, but Bedrock is billed again for frames already analysed |
| **Recovery needs a live worker** | Sweeps run inside workers and at API startup. With every worker down, nothing sweeps until one starts or an operator runs `recover_jobs.py` |
| **No backpressure** | Submissions are accepted regardless of queue depth. `/health/worker` exposes depth; nothing throttles on it |
| **One job per worker** | Concurrency is per-process — run more workers. Deliberate: Bedrock is billed per frame, so cost stays visible to whoever provisions processes |
| **Rate limiting is per-process** | The effective limit multiplies by instance count |
| **1 fps sampling** | Violations under ~1 second can be missed |
| **No brand-rule configuration** | One static prompt; no per-client policies, and no legal/regulatory evaluation |
| **No remediation** | Nova Canvas and corrected-video export are out of scope and not implemented |
| **Partial audits fail the job** | Deliberate: a partly-analysed video is not "compliant". One throttled frame fails the whole job after its own bounded retries; job retry is the path |
| **`Scan` remains for the all-videos CLI** | `check_pending_frames()` with no video id. The request path is a Query (Phase 6); a GSI on `status` would remove the CLI scan too |
| **`/projects/marketplace` still scans** | Filters on `status`, which is not a key and has no GSI. The route is unused by the frontend, so an index was not added for it |
| **No per-user quota** | One user can submit many videos and spend model capacity |
| **No metrics exporter** | Structured logs and the database only. Nothing is shipped to a monitoring platform |
| **Detection quality unmeasured** | No live model call was ever made. Nothing here says anything about accuracy, precision, or recall |

---

## 11. What has and has not been verified

**Verified** by automated tests, all external services mocked:
orchestration and state transitions, persistence call shapes, API contract and
status codes, ownership enforcement, retry and idempotency, extraction failure
modes, model-response validation, Bedrock retry classification, frontend
polling and results. Phase 6 adds: durable acceptance before the response, a
worker with no knowledge of the submission running the job, two workers racing
(in turns and in real threads) producing exactly one run, a stale worker unable
to write, retry classification and bounded exhaustion, backoff interaction with
the recovery sweep, graceful stop, liveness independent of MongoDB, readiness
failing and timing out, and Query pagination including the empty-filtered-page
case.

**Not verified:** live S3 or DynamoDB writes, live Bedrock inference or
detection quality, real FFmpeg extraction, real MongoDB, and any deployment.
No AWS call has been made in any phase. In particular, no worker has been run
against real infrastructure and no process has actually been SIGKILLed — crash
recovery is verified as *logic* (a lapsed lease and its conditional reclaim),
not as observed behaviour under a real crash.

---

*See `docs/PIPELINE_ARCHITECTURE.md` for the design, `docs/SECURITY_OPERATIONS.md`
for security procedures, and `docs/RELEASE_CHECKLIST.md` for the release gate.*
