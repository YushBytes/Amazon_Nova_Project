import os
import uuid
import boto3
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from auth import decode_token, oauth2_scheme

router = APIRouter(prefix="/projects", tags=["projects"])

# AWS Configuration
REGION = os.getenv("AWS_REGION", "us-east-1")
TABLE_NAME = "NovaFlowProjects"

# Initialize DynamoDB
dynamodb = boto3.resource('dynamodb', region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

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

def get_current_user(token: str = Depends(oauth2_scheme)):
    payload = decode_token(token)
    return payload

# --- Routes ---

@router.post("/", response_model=ProjectResponse)
async def create_project(data: ProjectCreate, user: dict = Depends(get_current_user)):
    if user["role"] != "client":
        raise HTTPException(status_code=403, detail="Only clients can create projects")
    
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
            FilterExpression=boto3.dynamodb.conditions.Attr("status").eq("SUBMITTED")
        )
        return [ProjectResponse(**item) for item in response.get("Items", [])]
    except Exception as e:
        print(f"DynamoDB Error: {e}")
        return []

@router.patch("/{project_id}/accept")
async def accept_project(project_id: str, user: dict = Depends(get_current_user)):
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="Only agents can accept projects")
    
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
async def initialize_upload(project_id: str, user: dict = Depends(get_current_user)):
    """
    Called by Agent to get a pre-signed URL to upload the Reel to S3.
    """
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="Only agents can upload reels")
    
    # 1. Verify project assignment
    try:
        item = table.get_item(Key={"project_id": project_id, "sk": "metadata"}).get("Item")
        if not item or item.get("agent_id") != user["sub"]:
            raise HTTPException(status_code=403, detail="You are not assigned to this project")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error")

    # 2. Generate S3 Pre-signed URL
    s3_client = boto3.client('s3', region_name=REGION)
    BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "novaflow-media")
    s3_key = f"raw/{project_id}/{user['sub']}/reel.mp4"
    
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
async def client_review(project_id: str, data: ReviewAction, user: dict = Depends(get_current_user)):
    if user["role"] != "client":
        raise HTTPException(status_code=403, detail="Only clients can review projects")
    
    # 1. Verify project ownership and status
    try:
        item = table.get_item(Key={"project_id": project_id, "sk": "metadata"}).get("Item")
        if not item or item.get("client_id") != user["sub"]:
            raise HTTPException(status_code=403, detail="Unauthorized")
        if item.get("status") != "CLIENT_REVIEW":
             raise HTTPException(status_code=400, detail="Project not ready for review")
    except Exception:
        raise HTTPException(status_code=500, detail="Database error")

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
    # 1. Get Project Context from DynamoDB
    item = table.get_item(Key={"project_id": project_id, "sk": "metadata"}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Project not found")

    # 2. Construct Prompt for Nova Pro (Placeholder for Bedrock call)
    # The prompt would include the video requirements, the analysis report, and the user's message.
    # For now, we return a simulated agentic response.
    
    response_msg = f"Based on the video requirements: '{item.get('requirements')}', the reel seems to align well. Regarding your question: '{request.message}', the AI detected minor lighting issues in the intro but they are within compliance range."

    return {"response": response_msg}
