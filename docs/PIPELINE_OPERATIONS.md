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
# Backend. Leave JWT_SECRET_KEY unset in development: an ephemeral per-process
# key is generated, so sessions end at restart.
cd backend && ../.venv/Scripts/python.exe -m uvicorn app:app --reload --port 8000

# Frontend
cd frontend && npm run dev
```

The pipeline needs real AWS and FFmpeg to actually process a video. Without
them, upload and job registration work, and analysis fails with a truthful
terminal state — which is itself worth seeing.

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
| `frames/{video_id}/` | Extracted frame JPEGs |
| `raw/{project_id}/{user}/` | Presigned project uploads (dead path, nothing reads it) |

Apply the hardening in `docs/SECURITY_OPERATIONS.md` §7.4 — public-access
block, encryption, versioning, TLS-only bucket policy, access logging. None of
it is configured by `infra/init_s3.py`.

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

## 6. Stuck jobs — the known gap

Analysis runs in an in-process `BackgroundTasks` worker. **If the process dies
mid-run, the job stays `extracting` or `analyzing` forever.** Nothing resumes
it. This is a real limitation, not a bug to be worked around.

Find them:

```bash
cd backend
python -c "
import anyio, pipeline
rows = anyio.run(pipeline.find_stuck_jobs)
for r in rows:
    print(r['video_id'], r['status'], r.get('updated_at'))
"
```

Reports any non-terminal job with no update for 30 minutes. It **only
reports** — nothing is failed automatically, because a slow legitimate run must
not be killed.

To recover one, re-queue it (this increments `attempt`, so it is bounded):

```
POST /analyze/{video_id}/retry      # as the owner
```

If the attempt budget is exhausted, an operator can reset the record
deliberately:

```javascript
// Deliberate operator action. Confirm the job really is orphaned first.
db.videos.updateOne(
  {video_id: "<id>", status: {$in: ["queued","extracting","analyzing"]}},
  {$set: {status: "failed", error_summary: "Abandoned by a server restart.",
          failed_stage: "worker", completed_at: new Date()}}
)
```

**Do not** set a job to `completed` by hand. That state means "analysis
finished and results were persisted"; asserting it without the results makes
the UI report compliance that was never established.

A durable queue is the real fix, and is out of Phase 4 scope.

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
| **In-process worker; no crash recovery** | A restart orphans running jobs (§6) |
| **Source videos on local disk** | A replaced container loses them; the ownership record then points at a missing file |
| **No backpressure** | Many submissions run many background tasks at once |
| **1 fps sampling** | Violations under ~1 second can be missed |
| **No brand-rule configuration** | One static prompt; no per-client policies, and no legal/regulatory evaluation |
| **No remediation** | Nova Canvas, corrected-video export are out of scope and not implemented |
| **Partial audits fail the job** | Deliberate: a partly-analysed video is not "compliant". One throttled frame fails the whole job; retry is the path |
| **`Scan` in the analysis path** | Cost grows with table size; a GSI on `status` would fix it |
| **No per-user quota** | One user can submit many videos and spend model capacity |
| **`uploads/` is never pruned** | Disk grows without bound |
| **No metrics** | Logs and the database only |
| **Detection quality unmeasured** | No live model call was made. Nothing here says anything about accuracy, precision, or recall |

---

## 11. What has and has not been verified

**Verified** by automated tests, all external services mocked:
orchestration and state transitions, persistence call shapes, API contract and
status codes, ownership enforcement, retry and idempotency, extraction failure
modes, model-response validation, Bedrock retry classification, frontend
polling and results.

**Not verified:** live S3 or DynamoDB writes, live Bedrock inference or
detection quality, real FFmpeg extraction, real MongoDB, and any deployment.
No AWS call was made at any point in Phase 4.

---

*See `docs/PIPELINE_ARCHITECTURE.md` for the design, `docs/SECURITY_OPERATIONS.md`
for security procedures, and `docs/RELEASE_CHECKLIST.md` for the release gate.*
