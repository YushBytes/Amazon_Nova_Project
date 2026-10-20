import os
import uuid
import logging

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
import aws_resources
import aws_clients
from auth import get_current_user, require_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])

# AWS Configuration. Resolved through aws_resources (Phase 4.1) so the
# projects table is overridable per environment like every other resource
# name; it was previously the one hardcoded name in the codebase.
REGION = aws_resources.aws_region()
TABLE_NAME = aws_resources.projects_table_name()

# AWS clients come from aws_clients (Phase 6), which applies finite connect and
# read timeouts plus a bounded retry budget. Built at module scope as before, so
# the names below stay patchable by the test fakes.
dynamodb = aws_clients.get_resource("dynamodb")
table = aws_clients.dynamodb_table(TABLE_NAME)

# --- Schemas ---

class ProjectCreate(BaseModel):
    title: str
    requirements: str

class ProjectResponse(BaseModel):
    project_id: str
    title: str
    requirements: str
    client_id: str
    agent_id: Optional[str] = None
    status: str
    created_at: str

# --- Helpers ---

# get_current_user and require_roles are defined once in auth.py so every
# router enforces authentication the same way. Role checks below are made from
# the token's role claim, which is derived from stored account data at login;
# request bodies are never consulted for authorization.
require_client = require_roles("client")
require_agent = require_roles("agent")


def _fetch_project(project_id: str) -> dict:
    """Load a project's metadata, or raise 404.

    Separated from authorization so that a storage failure cannot be confused
    with an authorization decision.
    """
    try:
        item = table.get_item(
            Key={"project_id": project_id, "sk": "metadata"}
        ).get("Item")
    except (BotoCoreError, ClientError):
        logger.exception("DynamoDB read failed for project %s", project_id)
        raise HTTPException(status_code=502, detail="Project store unavailable")
    if not item:
        raise HTTPException(status_code=404, detail="Project not found")
    return item


def _require_participant(item: dict, user: dict) -> None:
    """Allow only the owning client or the assigned agent.

    Guards against reading another tenant's project by supplying its id. 404
    rather than 403 so the endpoint cannot confirm that an id exists.
    """
    username = user["sub"]
    if username not in (item.get("client_id"), item.get("agent_id")):
        logger.warning("Denied cross-user access attempt to a project")
        raise HTTPException(status_code=404, detail="Project not found")

# --- Routes ---

@router.post("/", response_model=ProjectResponse)
async def create_project(data: ProjectCreate, user: dict = Depends(require_client)):
    
    project_id = str(uuid.uuid4())
    item = {
        "project_id": project_id,
        "sk": "metadata",
        "title": data.title,
        "requirements": data.requirements,
        "client_id": user["sub"],
        "agent_id": None,
        "status": "SUBMITTED",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    try:
        table.put_item(Item=item)
    except Exception as e:
        # In a real scenario, check if table exists or use fallback
        print(f"DynamoDB Error: {e}")
        # For POC/Dev without real table, we might just return the object
        # raise HTTPException(status_code=500, detail="Failed to persist to DynamoDB")

    return ProjectResponse(**item)

@router.get("/marketplace", response_model=List[ProjectResponse])
async def get_marketplace(user: dict = Depends(get_current_user)):
    # In production, use a GSI for status=SUBMITTED
    # For now, we'll scan (limited for POC)
    try:
        response = table.scan(
            FilterExpression=Attr("status").eq("SUBMITTED")
        )
        return [ProjectResponse(**item) for item in response.get("Items", [])]
    except Exception as e:
        print(f"DynamoDB Error: {e}")
        return []

@router.patch("/{project_id}/accept")
async def accept_project(project_id: str, user: dict = Depends(require_agent)):
    
    try:
        table.update_item(
            Key={"project_id": project_id, "sk": "metadata"},
            UpdateExpression="SET agent_id = :a, #s = :s",
            ExpressionAttributeValues={
                ":a": user["sub"],
                ":s": "IN_PROGRESS",
                ":submitted": "SUBMITTED"
            },
            ExpressionAttributeNames={
                "#s": "status"
            },
            ConditionExpression="attribute_exists(project_id) AND #s = :submitted"
        )
        return {"message": "Project accepted successfully"}
    except Exception as e:
        print(f"Error accepting project: {e}")
        raise HTTPException(status_code=400, detail="Could not accept project. It might be already taken.")

# --- Upload & Analysis ---

class UploadInitResponse(BaseModel):
    upload_url: str
    s3_key: str

@router.post("/{project_id}/upload-init", response_model=UploadInitResponse)
async def initialize_upload(project_id: str, user: dict = Depends(require_agent)):
    """
    Called by Agent to get a pre-signed URL to upload the Reel to S3.
    """
    
    # 1. Verify project assignment.
    item = _fetch_project(project_id)
    if item.get("agent_id") != user["sub"]:
        logger.warning("Denied upload-init by an agent not assigned to the project")
        raise HTTPException(status_code=404, detail="Project not found")

    # 2. Generate S3 Pre-signed URL
    s3_client = aws_clients.get_client("s3")
    BUCKET_NAME = aws_resources.media_bucket()
    s3_key = aws_resources.raw_reel_key(project_id, user['sub'])
    
    try:
        upload_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': BUCKET_NAME,
                'Key': s3_key,
                'ContentType': 'video/mp4'
            },
            ExpiresIn=3600
        )
        
        # 3. Update Status to AI_AUDITING (simulating immediate transition after upload)
        table.update_item(
            Key={"project_id": project_id, "sk": "metadata"},
            UpdateExpression="SET #s = :s, reel_s3_key = :k",
            ExpressionAttributeValues={":s": "AI_AUDITING", ":k": s3_key},
            ExpressionAttributeNames={"#s": "status"}
        )
        
        return UploadInitResponse(upload_url=upload_url, s3_key=s3_key)
    except Exception as e:
        print(f"S3 Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate upload URL")

# --- Client Review & AI Chat ---

class ReviewAction(BaseModel):
    action: str # "APPROVE" | "CONVEY_REPORT" | "REJECT"
    feedback: Optional[str] = None
    post_time: Optional[str] = None # For schedule post

@router.post("/{project_id}/review")
async def client_review(project_id: str, data: ReviewAction, user: dict = Depends(require_client)):
    
    # 1. Verify project ownership and status. These HTTPExceptions must not be
    #    caught and converted to a 500: that previously masked the
    #    authorization decision behind a generic "Database error".
    item = _fetch_project(project_id)
    if item.get("client_id") != user["sub"]:
        logger.warning("Denied cross-user review attempt on a project")
        raise HTTPException(status_code=404, detail="Project not found")
    if item.get("status") != "CLIENT_REVIEW":
        raise HTTPException(status_code=400, detail="Project not ready for review")

    # 2. Update status based on action
    new_status = "COMPLETED" if data.action in ["APPROVE", "REJECT"] else "IN_PROGRESS"
    update_expr = "SET #s = :s"
    expr_vals = {":s": new_status}
    
    if data.action == "CONVEY_REPORT":
        update_expr += ", feedback = :f"
        expr_vals[":f"] = data.feedback or "Please refer to previous discussion."
    elif data.action == "APPROVE":
        update_expr += ", post_time = :p"
        expr_vals[":p"] = data.post_time or "INSTANT"

    try:
        table.update_item(
            Key={"project_id": project_id, "sk": "metadata"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_vals,
            ExpressionAttributeNames={"#s": "status"}
        )
        return {"status": new_status, "message": f"Project {data.action.lower()}ed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ChatRequest(BaseModel):
    message: str

@router.post("/{project_id}/chat")
async def ai_chat(project_id: str, request: ChatRequest, user: dict = Depends(get_current_user)):
    """
    Simulates a chat with Amazon Nova about the project/video quality.
    """
    # 1. Get project context, but only for a participant in this project.
    #    Without this check any authenticated user could read any project's
    #    requirements by supplying its id.
    item = _fetch_project(project_id)
    _require_participant(item, user)

    # 2. Construct Prompt for Nova Pro (Placeholder for Bedrock call)
    # The prompt would include the video requirements, the analysis report, and the user's message.
    # For now, we return a simulated agentic response.
    
    response_msg = f"Based on the video requirements: '{item.get('requirements')}', the reel seems to align well. Regarding your question: '{request.message}', the AI detected minor lighting issues in the intro but they are within compliance range."

    return {"response": response_msg}
