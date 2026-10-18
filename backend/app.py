import logging
import os
import re
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
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# Actual imports for orchestrator and automation
from orchestrator import NovaFlowOrchestrator
from automation import NovaFlowAutomation

# Import routers
import video_repo
from auth import (
    create_media_token,
    decode_media_token,
    get_current_user,
    router as auth_router,
)
from config import ALLOWED_UPLOAD_CONTENT_TYPES, ALLOWED_UPLOAD_CONTENT_TYPE_PREFIXES
from config import get_settings
from projects import router as projects_router
from video_repo import OwnershipUnknown

load_dotenv()

logger = logging.getLogger(__name__)

# Fails fast in production when required security configuration is missing,
# malformed, or a known-insecure placeholder.
settings = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Best-effort index creation. A missing MongoDB must not prevent startup."""
    try:
        await video_repo.ensure_indexes()
    except Exception:
        logger.warning(
            "Could not create video ownership indexes; is MongoDB reachable?"
        )
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
    """Public liveness probe. Deliberately reveals no configuration."""
    return {"status": "online", "agent": "NovaFlow", "version": "2.0-aws"}


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


def _resolve_within_uploads(stored_filename: str) -> Path:
    """Join a server-generated filename to UPLOAD_DIR and assert containment.

    Defence in depth: the filename is already server-generated, and this
    re-checks that the resolved path has not escaped the upload directory.
    """
    candidate = (UPLOAD_DIR / stored_filename).resolve()
    if candidate != UPLOAD_DIR and UPLOAD_DIR not in candidate.parents:
        logger.error("Rejected a path that resolved outside the upload directory")
        raise HTTPException(status_code=400, detail="Invalid file reference")
    return candidate


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
    """Store a video and record the caller as its owner.

    Does NOT start analysis; call POST /analyze/{video_id} afterwards.
    """
    extension = _safe_extension(file.filename)
    _check_content_type(file.content_type)

    video_id = f"vid_{uuid.uuid4().hex[:8]}"
    stored_filename = f"{video_id}.{extension}"
    destination = _resolve_within_uploads(stored_filename)

    size_bytes = await _stream_to_disk(file, destination)

    try:
        await video_repo.record_video(
            video_id=video_id,
            owner=user["sub"],
            stored_filename=stored_filename,
            original_filename=file.filename,
            extension=extension,
            size_bytes=size_bytes,
        )
    except Exception:
        # Without an ownership record the file would be unreachable and
        # unattributable, so do not keep it.
        destination.unlink(missing_ok=True)
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

    path = _resolve_within_uploads(record["stored_filename"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    # Explicit content type and an attachment-safe disposition: never let a
    # stored file be interpreted as HTML on this origin.
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@app.post("/analyze/{video_id}")
async def start_analysis(
    video_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Trigger the analysis pipeline for a video the caller owns."""
    record = await require_video_owner(video_id, user)

    path = _resolve_within_uploads(record["stored_filename"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video payload not found.")

    from extractor import extract_and_store
    from analyzer import check_pending_frames

    def run_pipeline(file_path: str, vid: str):
        logger.info("Analysis pipeline started for %s", vid)
        try:
            extract_and_store(file_path, vid)
            check_pending_frames(video_id=vid)
            logger.info("Analysis pipeline finished for %s", vid)
        except Exception:
            # Background tasks run after the response; log rather than lose it.
            logger.exception("Analysis pipeline failed for %s", vid)

    background_tasks.add_task(run_pipeline, str(path), video_id)
    await video_repo.set_status(video_id, "ANALYZING")

    return {
        "video_id": video_id,
        "status": "ANALYZING",
        "message": "Neural analysis initiated. Real-time feedback incoming.",
    }


@app.get("/violations/{video_id}")
async def get_violations(video_id: str, user: dict = Depends(get_current_user)):
    """Return detections for a video the caller owns."""
    await require_video_owner(video_id, user)

    table_name = os.getenv("DYNAMODB_TABLE_NAME", "NovaFlowFrames")
    dynamodb = boto3.resource(
        "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
    )
    table = dynamodb.Table(table_name)

    try:
        response = table.query(KeyConditionExpression=Key("video_id").eq(video_id))
    except (BotoCoreError, ClientError):
        # Do not echo botocore detail (table names, ARNs) to the client.
        logger.exception("DynamoDB query failed for video %s", video_id)
        raise HTTPException(status_code=502, detail="Analysis store unavailable")

    items = response.get("Items", [])
    all_detections = []
    for item in items:
        for detection in item.get("detections", []) or []:
            detection["timestamp"] = float(item.get("timestamp", 0))
            all_detections.append(detection)

    return {
        "video_id": video_id,
        "detections": all_detections,
        "frame_count": len(items),
    }


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
