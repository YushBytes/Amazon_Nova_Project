import logging
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

# Actual imports for orchestrator and automation
from orchestrator import NovaFlowOrchestrator
from automation import NovaFlowAutomation

# Import routers
import aws_clients
import config
import aws_resources
import db_init
import pipeline
import source_store
import video_repo
from auth import (
    create_media_token,
    decode_media_token,
    get_current_user,
    router as auth_router,
)
from config import ALLOWED_UPLOAD_CONTENT_TYPES, ALLOWED_UPLOAD_CONTENT_TYPE_PREFIXES
from config import get_settings
from logging_setup import install_log_redaction
from job_state import JobState, coerce
from pipeline import AlreadyRunning, PipelineError, RetryLimitReached
from projects import router as projects_router
from rate_limit import RateLimitMiddleware, build_credential_rules
from source_store import SourceMissing, SourceStoreError
from video_repo import OwnershipUnknown

load_dotenv()

logger = logging.getLogger(__name__)

# Fails fast in production when required security configuration is missing,
# malformed, or a known-insecure placeholder.
settings = get_settings()

# Media playback URLs carry a short-lived token in the query string, so strip
# those values from this process's log records before anything is emitted.
# External proxy and CDN logs are a separate, deployment-level concern.
install_log_redaction()

def _recovery_on_startup() -> bool:
    """Whether to sweep for abandoned jobs when the process starts.

    On by default. An operator running several instances behind a load
    balancer may want exactly one of them doing this, and a test process does
    not want it at all, so it is a switch rather than unconditional.
    """
    raw = (os.getenv("RECOVER_JOBS_ON_STARTUP") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _dispatch_recovered_job(video_id: str) -> None:
    """Run a recovered job on a worker thread.

    `BackgroundTasks` is request-scoped and there is no request here, so the
    thread is started directly. The job is already durably queued, so losing
    this thread costs a wait for the next sweep, not the job.
    """
    import threading

    threading.Thread(
        target=pipeline.run_job,
        args=(video_id,),
        name=f"novaflow-recovery-{video_id}",
        daemon=True,
    ).start()


def _dispatch_analysis(background_tasks: BackgroundTasks, video_id: str) -> str:
    """Hand a durably-queued job to an executor, according to the mode.

    The job is ALREADY persisted as `queued` before this is called, so this
    function cannot lose work: in `worker` mode it deliberately does nothing
    and a separate `python backend/worker.py` process claims the job on its
    next poll. That is what lets the API restart, or be redeployed, without
    stranding accepted jobs - there is no in-process handoff to drop.

    `inline` mode keeps the Phase 4 behaviour of running the job in a
    FastAPI background thread, which is convenient for single-process local
    development and for the test suite but is not durable.

    Returns the mode used, for the response and the log line.
    """
    mode = settings.job_execution_mode
    if mode == config.INLINE_MODE:
        background_tasks.add_task(pipeline.run_job, video_id)
    return mode


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Repeatable database initialization.

    Best effort: a missing or unreachable MongoDB must not prevent the process
    from starting. `initialize_database` never modifies account data, and it
    skips the unique username index (logging counts only) when duplicates
    would make it fail. Run it deliberately with strict=True to surface that
    as an error; see docs/SECURITY_OPERATIONS.md.
    """
    try:
        results = await db_init.initialize_database(strict=False)
        logger.info("Database initialization results: %s", results)
    except Exception:
        logger.warning(
            "Database initialization skipped; is MongoDB reachable?"
        )

    # Phase 4.1: reclaim jobs abandoned by a previous process.
    #
    # This runs ONCE, here, at startup. It is not a continuous sweep, and the
    # application does not pretend to have one: a job abandoned while this
    # process stays up waits for the next restart or for an operator to run
    # `python backend/recover_jobs.py`. See docs/PIPELINE_OPERATIONS.md for the
    # scheduler configuration that makes recovery continuous.
    #
    # Recovered jobs are requeued AND dispatched, because this process is also
    # the worker. Dispatch is best effort: the requeue is the durable part.
    if _recovery_on_startup():
        try:
            counts = await pipeline.recover_abandoned_jobs(
                dispatch=_dispatch_recovered_job
            )
            if counts.get("scanned"):
                logger.info("Startup job recovery: %s", counts)
        except Exception:
            # Never prevent the application from serving because a sweep
            # failed; the jobs stay abandoned and visibly non-terminal.
            logger.exception("Startup job recovery failed")

    yield


app = FastAPI(
    title="NovaFlow API",
    lifespan=lifespan,
    # The interactive docs enumerate every route; keep them out of production
    # unless an operator opts in with EXPOSE_API_DOCS=true.
    docs_url="/docs" if settings.expose_docs else None,
    redoc_url="/redoc" if settings.expose_docs else None,
    openapi_url="/openapi.json" if settings.expose_docs else None,
)

# Throttle the credential endpoints. Added before CORS so the limit applies to
# the request regardless of origin, and implemented as ASGI middleware so a
# malformed body cannot slip past the counter (a route decorator runs only
# after validation). Counters are per-process; see rate_limit.py.
app.add_middleware(
    RateLimitMiddleware,
    rules=build_credential_rules(settings),
    trusted_proxy_count=settings.trusted_proxy_count,
    enabled=settings.rate_limit_enabled,
)

# Exact origins only. A wildcard would let any site call the API with a
# victim's token; config rejects "*" outright.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

UPLOAD_DIR = Path("uploads").resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Connect routers. Auth is included (not mounted) so its routes share this
# app's CORS policy and appear in one OpenAPI schema, without exposing a
# second /auth/docs surface. External paths are unchanged.
app.include_router(auth_router)
app.include_router(projects_router)

orchestrator = NovaFlowOrchestrator()
automation = NovaFlowAutomation()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return a generic 500 and keep the detail server-side.

    Previously several routes returned `str(exc)` to the client, which leaked
    bucket names, table names, and botocore internals. The traceback is still
    logged for operators.
    """
    logger.exception(
        "Unhandled error serving %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/status")
async def get_status():
    """Public liveness probe. Deliberately reveals no configuration.

    Retained unchanged for backward compatibility. `/health/live` and
    `/health/ready` are the Phase 6 probes and are what an orchestrator
    should use; this one is an unauthenticated banner and nothing more.
    """
    return {"status": "online", "agent": "NovaFlow", "version": "2.0-aws"}


# ---------------------------------------------------------------------------
# Health and readiness (Phase 6)
# ---------------------------------------------------------------------------
#
# Liveness and readiness answer different questions and must not be the same
# endpoint:
#
#   /health/live   Can this process still serve? Touches NOTHING external, so
#                  a MongoDB outage cannot make every replica fail its
#                  liveness probe and get killed - which would turn a
#                  recoverable dependency blip into a cluster-wide restart
#                  loop and guarantee downtime instead of degradation.
#
#   /health/ready  Should this process receive traffic? Checks the
#                  dependencies a request actually needs, each with a short
#                  finite timeout. 503 when it cannot serve correctly, so a
#                  load balancer drains it while leaving it running.
#
# Neither reveals a bucket name, connection string, credential, or exception
# text: a probe endpoint is usually unauthenticated.

async def _check_with_timeout(name: str, coro_factory, timeout: float) -> dict:
    """Run one dependency check under a hard timeout.

    A readiness probe that can hang is worse than one reporting a dependency
    as down: the orchestrator's own probe timeout fires, which it may treat as
    a failure of the whole pod rather than of one dependency. Every check here
    therefore returns within `timeout` whatever the dependency does.

    The returned detail is a fixed phrase per outcome, never the exception
    text, which for a driver error can contain the connection string.
    """
    import anyio

    try:
        with anyio.fail_after(timeout):
            await coro_factory()
    except TimeoutError:
        logger.warning("Readiness check %s timed out", name)
        return {"name": name, "ok": False, "detail": "timed out"}
    except Exception as exc:
        # Type only. A pymongo error renders the full URI in its message.
        logger.warning("Readiness check %s failed: %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": "unreachable"}
    return {"name": name, "ok": True, "detail": "ok"}


async def _check_mongodb() -> None:
    """Ping the database that holds accounts, ownership and the job queue."""
    from db import get_database

    database = await get_database()
    await database.command("ping")


@app.get("/health/live")
async def health_live():
    """Liveness: this process is up and the event loop is responsive.

    Deliberately checks no dependency. A 200 here means only "do not restart
    me"; it is NOT a statement that the application can serve a request. Use
    /health/ready for that.
    """
    return {"status": "alive", "mode": settings.job_execution_mode}


@app.get("/health/ready")
async def health_ready(response: Response):
    """Readiness: the dependencies a request needs are usable.

    Checks, each under READINESS_TIMEOUT_SECONDS:

    * configuration - already validated at startup, so this reports the fact
      rather than re-deriving it;
    * MongoDB - required by every authenticated route, since accounts,
      ownership and job state all live there.

    S3, DynamoDB and Bedrock are deliberately NOT probed. They are needed by
    the worker, not by the API's request path, and probing them would mean a
    network call per probe against services billed per request - and would
    make the API drain itself for an outage it can still serve reads during.
    `/health/worker` covers whether work is actually progressing.

    Returns 503 with the same body shape when any check fails, so a caller can
    always read `checks` rather than having to parse an error.
    """
    checks = [
        {"name": "configuration", "ok": True, "detail": "validated at startup"},
        await _check_with_timeout(
            "mongodb", _check_mongodb, settings.readiness_timeout_seconds
        ),
    ]
    ready = all(check["ok"] for check in checks)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@app.get("/health/worker")
async def health_worker(response: Response):
    """Whether analysis work is actually being consumed.

    INFERRED FROM THE QUEUE, NOT PROBED. The worker is a separate process -
    possibly on another host - and this endpoint does not contact it. What it
    can say truthfully is whether the queue looks like something is draining
    it:

    * `abandoned` counts jobs whose lease lapsed. A worker that died leaves
      these behind, and a healthy deployment returns them to `queued` within
      one recovery interval.
    * `queued` with a persistently non-zero value and no `extracting` or
      `analyzing` is the signature of no worker running at all - the common
      misconfiguration when JOB_EXECUTION_MODE is `worker` and nobody started
      one.

    Counts only, never ids or owners. 503 when jobs are abandoned, so this can
    drive an alert; a queue backlog alone is NOT unhealthy, because a backlog
    is the normal state of a queue under load.
    """
    try:
        counts = await video_repo.count_by_state()
        abandoned = len(await video_repo.find_expired_leases())
    except Exception as exc:
        logger.warning("Worker health could not be read: %s", type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unknown", "detail": "job store unreachable"}

    active = counts.get(JobState.EXTRACTING.value, 0) + counts.get(
        JobState.ANALYZING.value, 0
    )
    healthy = abandoned == 0
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "degraded",
        "execution_mode": settings.job_execution_mode,
        "counts": counts,
        "active": active,
        "abandoned": abandoned,
        "note": (
            "Inferred from the job queue; the worker process is not contacted."
        ),
    }


# ---------------------------------------------------------------------------
# Goal decomposition
# ---------------------------------------------------------------------------

class GoalRequest(BaseModel):
    goal: str


@app.post("/execute")
async def execute_goal(request: GoalRequest, user: dict = Depends(get_current_user)):
    """Decompose a goal. Authenticated: it spends model capacity."""
    steps = await orchestrator.decompose_goal(request.goal)
    return {"goal": request.goal, "steps": steps, "status": "plan_generated"}


# ---------------------------------------------------------------------------
# Upload validation helpers
# ---------------------------------------------------------------------------

# A stored filename is always server-generated: "vid_" + 8 hex + "." + a
# whitelisted alphanumeric extension. Nothing from the client reaches a path.
_VIDEO_ID_RE = re.compile(r"^vid_[0-9a-f]{8}$")


def _safe_extension(filename: Optional[str]) -> str:
    """Return a validated lowercase extension from a client filename.

    The client filename is treated purely as a hint. Only the final suffix is
    considered, it must be alphanumeric, and it must appear in the configured
    allowlist. Any separators, traversal sequences, or unexpected suffixes are
    rejected rather than sanitised, so no client-controlled fragment can ever
    be interpolated into a path.
    """
    allowed = settings.allowed_video_extensions
    if not filename:
        raise HTTPException(status_code=400, detail="A filename is required")

    # Path().suffix handles both separators and strips directory components.
    suffix = Path(filename.replace("\\", "/")).suffix.lower().lstrip(".")
    if not suffix:
        raise HTTPException(
            status_code=400,
            detail=f"File must have an extension. Supported: {sorted(allowed)}",
        )
    if not suffix.isalnum() or suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Supported: {sorted(allowed)}",
        )
    return suffix


def _check_content_type(content_type: Optional[str]) -> None:
    """Secondary signal only. A matching content type proves nothing about the
    bytes, and a mismatch is still worth rejecting early."""
    if not content_type:
        return
    normalized = content_type.split(";")[0].strip().lower()
    if normalized in ALLOWED_UPLOAD_CONTENT_TYPES:
        return
    if normalized.startswith(ALLOWED_UPLOAD_CONTENT_TYPE_PREFIXES):
        return
    raise HTTPException(
        status_code=400, detail="Unsupported content type for a video upload"
    )


async def _stream_to_disk(upload: UploadFile, destination: Path) -> int:
    """Copy the upload to disk in chunks, enforcing the size limit.

    Streaming avoids holding an arbitrarily large body in memory. If the limit
    is exceeded the partial file is removed and the request is rejected with
    413 rather than filling the disk.
    """
    limit = settings.max_upload_bytes
    chunk_size = 1024 * 1024
    written = 0
    try:
        with destination.open("wb") as buffer:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            f"File exceeds the maximum upload size of "
                            f"{limit} bytes"
                        ),
                    )
                buffer.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        logger.exception("Failed to persist an upload")
        raise HTTPException(status_code=500, detail="Could not store the upload")

    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    return written


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

async def require_video_owner(video_id: str, user: dict) -> dict:
    """Authorize the caller for this video, or raise.

    Returns the ownership record. A missing record is a denial, not a pass:
    videos uploaded before ownership tracking existed cannot be accessed
    through these routes.

    404 is returned for both "no such video" and "not your video" so the
    endpoint cannot be used to probe which ids exist.
    """
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid video id")

    try:
        record = await video_repo.get_video(video_id)
    except OwnershipUnknown:
        logger.info("Denied access to a video with no ownership record")
        raise HTTPException(status_code=404, detail="Video not found")

    if record.get("owner") != user["sub"]:
        logger.warning("Denied cross-user access attempt to a video")
        raise HTTPException(status_code=404, detail="Video not found")

    return record


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.post("/upload-reel")
async def upload_reel(
    file: UploadFile = File(...), user: dict = Depends(get_current_user)
):
    """Store a video durably in S3 and record the caller as its owner.

    Does NOT start analysis; call POST /analyze/{video_id} afterwards.

    Phase 4.1 ordering, which is what makes the result consistent:

    1. stream the body to a unique temporary file, enforcing the size limit;
    2. put that file in S3 under a server-generated key;
    3. write the ownership record naming that object;
    4. remove the temporary file, always.

    If (2) fails there is no record and no object, so nothing is orphaned. If
    (3) fails the object is deleted as compensating cleanup, because without a
    record it is unattributable and unreachable - no route can ever name it.
    """
    extension = _safe_extension(file.filename)
    _check_content_type(file.content_type)

    video_id = f"vid_{uuid.uuid4().hex[:8]}"
    # Retained on the record for legacy playback and for the operator
    # backfill. It is no longer where the bytes live.
    stored_filename = f"{video_id}.{extension}"

    # mkdtemp guarantees a unique directory, so two concurrent uploads cannot
    # collide on a staging path even if they somehow shared a video id.
    staging_dir = tempfile.mkdtemp(prefix=f"novaflow-upload-{video_id}-")
    staged = Path(staging_dir) / f"source.{extension}"

    try:
        size_bytes = await _stream_to_disk(file, staged)

        try:
            # boto3 is synchronous; a 100 MiB PUT on the event loop would
            # stall every other request on this worker.
            ref = await run_in_threadpool(
                source_store.upload_source,
                local_path=staged,
                video_id=video_id,
                extension=extension,
                size_bytes=size_bytes,
            )
        except SourceStoreError as exc:
            # Safe message from source_store; no bucket name or botocore text.
            logger.error("Durable upload failed for a new video")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            )
    finally:
        # The staged copy is never the durable one. Remove it on every path.
        shutil.rmtree(staging_dir, ignore_errors=True)

    try:
        await video_repo.record_video(
            video_id=video_id,
            owner=user["sub"],
            stored_filename=stored_filename,
            original_filename=file.filename,
            extension=extension,
            size_bytes=size_bytes,
            s3_bucket=ref.bucket,
            s3_key=ref.key,
        )
    except Exception:
        # Compensating cleanup: an object with no record is an orphan that no
        # route can reach and no user can be billed for.
        await run_in_threadpool(source_store.delete_source, ref)
        logger.exception("Could not record video ownership; upload rolled back")
        raise HTTPException(status_code=500, detail="Could not store the upload")

    base_url = os.getenv("API_URL", "http://localhost:8000")
    # A browser <video> element cannot send an Authorization header, so the
    # playback URL carries a short-lived token scoped to this one video.
    media_token = create_media_token(user["sub"], video_id)

    return {
        "video_id": video_id,
        "video_url": f"{base_url}/uploads/{stored_filename}?access_token={media_token}",
        "filename": stored_filename,
        "original_filename": file.filename,
        "size_bytes": size_bytes,
        "status": "UPLOADED",
        "message": "Uplink established. Payload ready for analysis.",
    }


# ---------------------------------------------------------------------------
# Protected media access
# ---------------------------------------------------------------------------

@app.get("/uploads/{stored_filename}")
async def serve_upload(
    request: Request,
    stored_filename: str,
    access_token: Optional[str] = Query(default=None),
):
    """Serve an uploaded video to its owner only.

    Replaces the previous public StaticFiles mount, which served every upload
    to anyone and - combined with unrestricted file extensions - allowed
    arbitrary content to be hosted on the application's own origin.

    Authorization is a short-lived media token in the `access_token` query
    parameter, bound to this one video id. A query parameter is used because a
    browser <video> element cannot send an Authorization header.
    """
    # Derive the video id from the server-generated filename shape.
    video_id = stored_filename.split(".")[0]
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=404, detail="Not found")

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_media_token(access_token)
    if claims.get("vid") != video_id:
        # A media token is bound to one video and cannot be replayed for another.
        raise HTTPException(status_code=404, detail="Not found")

    try:
        record = await video_repo.get_video(video_id)
    except OwnershipUnknown:
        raise HTTPException(status_code=404, detail="Not found")

    if record.get("owner") != claims.get("sub"):
        raise HTTPException(status_code=404, detail="Not found")

    # Phase 4.1: the bytes normally live in S3. A pre-4.1 record still points
    # at local disk, so that path is kept rather than breaking old uploads.
    if source_store.has_durable_source(record):
        range_header = request.headers.get("range")
        try:
            stream = await run_in_threadpool(
                source_store.open_stream, record, range_header
            )
        except SourceMissing:
            raise HTTPException(status_code=404, detail="Not found")
        except SourceStoreError:
            # No bucket name, key, or botocore detail reaches the client.
            raise HTTPException(status_code=502, detail="Video unavailable")

        # Explicit content type plus nosniff: never let a stored object be
        # interpreted as HTML on this origin. Range headers come from S3 so
        # the browser can still seek, which FileResponse used to handle.
        return StreamingResponse(
            stream.chunks,
            status_code=stream.status,
            media_type="video/mp4",
            headers=stream.headers,
        )

    path = source_store.local_fallback_path(record, UPLOAD_DIR)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _require_source(record: dict) -> None:
    """Refuse to queue a job whose source bytes cannot be reached.

    Phase 4 checked that the local file existed, which is why a restarted
    container returned "Video payload not found" for every job. A record with
    a durable S3 reference is accepted without a HEAD request: the pipeline
    handles a missing object by failing the job with a truthful reason, and
    paying a round trip on every submit to pre-empt a rare case is not worth
    it. A legacy record is still checked on disk, because for those the file
    really is the only copy.
    """
    if source_store.has_durable_source(record):
        return
    if source_store.local_fallback_path(record, UPLOAD_DIR) is not None:
        return
    raise HTTPException(
        status_code=404,
        detail="The uploaded video is no longer available. Please upload it again.",
    )


@app.post("/analyze/{video_id}")
async def start_analysis(
    video_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Queue analysis for a video the caller owns.

    Returns once the job is durably QUEUED - not once analysis has finished.
    The response carries the real job state; poll GET /jobs/{video_id} for
    progress and the terminal outcome.

    A second request while a run is in flight returns 409 rather than starting
    a duplicate. A finished job can be retried while attempts remain.
    """
    record = await require_video_owner(video_id, user)
    _require_source(record)

    try:
        updated = await pipeline.submit(video_id)
    except AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RetryLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Queued durably first, then handed to an executor, so a job can never be
    # executing without a record saying so. No path is passed: the worker
    # fetches the source from S3 using the stored reference, which is what
    # lets a different instance - or this one after a restart - run the job.
    mode = _dispatch_analysis(background_tasks, video_id)
    logger.info("Job accepted for %s (execution mode %s)", video_id, mode)

    job = video_repo.job_view(updated)
    return {
        "video_id": video_id,
        # Retained for backwards compatibility with the Phase 2/3 client.
        "status": job["state"],
        "job": job,
        "message": "Analysis queued.",
    }


@app.get("/jobs/{video_id}")
async def get_job(video_id: str, user: dict = Depends(get_current_user)):
    """Current analysis state for a video the caller owns.

    This is the authoritative progress source. `is_terminal` tells a client
    when to stop polling; `state` is never "completed" unless the analysis
    finished and its results were persisted.
    """
    record = await require_video_owner(video_id, user)
    return video_repo.job_view(record)


@app.post("/analyze/{video_id}/retry")
async def retry_analysis(
    video_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Re-run analysis for a job that has reached a terminal state.

    Bounded by MAX_ATTEMPTS so a persistently failing video cannot be retried
    indefinitely, which would keep spending model capacity.
    """
    record = await require_video_owner(video_id, user)

    state = coerce(record.get("status"))
    if state.is_active:
        raise HTTPException(
            status_code=409, detail="Analysis is already running for this video."
        )
    if state is JobState.UPLOADED:
        raise HTTPException(
            status_code=409,
            detail="This video has not been analysed yet; start an analysis instead.",
        )

    _require_source(record)

    try:
        updated = await pipeline.submit(video_id, allow_retry=True)
    except AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RetryLimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except PipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    mode = _dispatch_analysis(background_tasks, video_id)
    logger.info("Retry accepted for %s (execution mode %s)", video_id, mode)

    job = video_repo.job_view(updated)
    return {"video_id": video_id, "status": job["state"], "job": job}


# Frames fetched per /violations page. DynamoDB returns at most 1 MB per
# query regardless, so this bounds the work and the response size.
VIOLATIONS_PAGE_SIZE = 200
MAX_VIOLATION_PAGES = 25


@app.get("/violations/{video_id}")
async def get_violations(
    video_id: str,
    cursor: Optional[str] = Query(
        default=None,
        description="Opaque frame_number cursor from a previous response.",
    ),
    limit: int = Query(default=VIOLATIONS_PAGE_SIZE, ge=1, le=1000),
    user: dict = Depends(get_current_user),
):
    """Detections for a video the caller owns, with the job's real state.

    Paginated by frame number: a long video can hold more frames than one
    DynamoDB query returns, and the previous single-call implementation
    silently truncated at the 1 MB page boundary.

    `job` carries the authoritative state. A client must not treat an empty
    `detections` list as "compliant" unless `job.state` is "completed".
    """
    record = await require_video_owner(video_id, user)

    # Shared, timed client (Phase 6). Previously a fresh untimed resource was
    # built per request, so a hung DynamoDB call blocked the request worker
    # with no bound and every call paid client construction.
    table_name = aws_resources.frames_table_name()
    table = aws_clients.dynamodb_table(table_name)

    query_kwargs = {
        "KeyConditionExpression": Key("video_id").eq(video_id),
        "Limit": limit,
    }
    if cursor is not None:
        try:
            start_frame = int(cursor)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid cursor")
        query_kwargs["ExclusiveStartKey"] = {
            "video_id": video_id,
            "frame_number": start_frame,
        }

    try:
        response = table.query(**query_kwargs)
    except (BotoCoreError, ClientError):
        # Do not echo botocore detail (table names, ARNs) to the client.
        logger.exception("DynamoDB query failed for video %s", video_id)
        raise HTTPException(status_code=502, detail="Analysis store unavailable")

    items = response.get("Items", []) or []

    all_detections = []
    audited_frames = 0
    failed_frames = 0
    for item in items:
        status = item.get("status")
        if status == "audited":
            audited_frames += 1
        elif status == "audit_failed":
            failed_frames += 1

        timestamp = _as_float(item.get("timestamp"))
        for detection in item.get("detections", []) or []:
            # Frame context the UI needs to seek and attribute the finding.
            detection["timestamp"] = timestamp
            detection["frame_number"] = _as_int(item.get("frame_number"))
            all_detections.append(_normalise_detection(detection))

    last_key = response.get("LastEvaluatedKey") or None
    next_cursor = (
        str(_as_int(last_key.get("frame_number"))) if last_key else None
    )

    return {
        "video_id": video_id,
        "detections": all_detections,
        # Frames in THIS page. `job.frames_extracted` is the total.
        "frame_count": len(items),
        "audited_frames": audited_frames,
        "failed_frames": failed_frames,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "job": video_repo.job_view(record),
    }


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalise_detection(detection: dict) -> dict:
    """Convert stored values into JSON-friendly types.

    DynamoDB returns numbers as Decimal, which is not JSON serialisable.
    Optional fields stay absent when the model never supplied them, so the
    client can tell "no confidence given" from "confidence zero".
    """
    out = {
        "category": str(detection.get("category") or "violation"),
        "description": str(detection.get("description") or ""),
        "timestamp": _as_float(detection.get("timestamp")),
        "frame_number": _as_int(detection.get("frame_number")),
    }
    if detection.get("risk_level") is not None:
        out["risk_level"] = str(detection["risk_level"])
    if detection.get("confidence") is not None:
        out["confidence"] = _as_float(detection["confidence"])
    box = detection.get("bounding_box")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        out["bounding_box"] = [_as_float(v) for v in box]
    return out


@app.get("/submissions")
async def get_all_submissions(user: dict = Depends(get_current_user)):
    """List the caller's own uploads.

    Previously this listed every file on disk for every caller, with the status
    and assigned agent fabricated from hash(video_id). It now returns only the
    requester's videos, from real ownership records.
    """
    records = await video_repo.list_videos_for_owner(user["sub"])
    base_url = os.getenv("API_URL", "http://localhost:8000")

    submissions = []
    for record in records:
        video_id = record["video_id"]
        media_token = create_media_token(user["sub"], video_id)
        created = record.get("created_at")
        submissions.append(
            {
                "id": video_id,
                "filename": record.get("stored_filename"),
                "original_filename": record.get("original_filename"),
                "title": record.get("original_filename") or video_id,
                "status": record.get("status", "UPLOADED"),
                "created_at": created.isoformat() if created else None,
                "size_bytes": record.get("size_bytes"),
                "url": (
                    f"{base_url}/uploads/{record.get('stored_filename')}"
                    f"?access_token={media_token}"
                ),
            }
        )

    return {"submissions": submissions}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
