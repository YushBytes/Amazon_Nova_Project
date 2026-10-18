import os
import shutil
import uuid
import json
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import uvicorn

# Actual imports for orchestrator and automation
from orchestrator import NovaFlowOrchestrator
from automation import NovaFlowAutomation

# Import routers
from auth import app as auth_app
from projects import router as projects_router

load_dotenv()

from fastapi.staticfiles import StaticFiles

app = FastAPI(title="NovaFlow API")

# Enable CORS for the Vite/Next.js dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for video playback
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Connect routers
app.include_router(projects_router)
app.mount("/auth", auth_app) # Keep auth as a sub-app or include its routes

orchestrator = NovaFlowOrchestrator()
automation = NovaFlowAutomation()

@app.get("/status")
async def get_status():
    return {"status": "online", "agent": "NovaFlow", "version": "2.0-aws"}

# New simplified goal execution for NovaFlow
class GoalRequest(BaseModel):
    goal: str

@app.post("/execute")
async def execute_goal(request: GoalRequest):
    steps = await orchestrator.decompose_goal(request.goal)
    return {
        "goal": request.goal,
        "steps": steps,
        "status": "plan_generated"
    }

# --- REAL-TIME ANALYSIS ORCHESTRATION ---

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.post("/upload-reel")
async def upload_reel(file: UploadFile = File(...)):
    """
    Handles local video upload. Does NOT start analysis.
    """
    video_id = f"vid_{uuid.uuid4().hex[:8]}"
    extension = file.filename.split('.')[-1] if '.' in file.filename else 'mp4'
    filename = f"{video_id}.{extension}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    base_url = os.getenv("API_URL", "http://localhost:8000")
    
    return {
        "video_id": video_id,
        "video_url": f"{base_url}/uploads/{filename}",
        "filename": filename,
        "status": "UPLOADED",
        "message": "Uplink established. Payload ready for analysis."
    }

@app.post("/analyze/{video_id}")
async def start_analysis(video_id: str, background_tasks: BackgroundTasks):
    """
    Explicitly triggers the AI analysis pipeline for a previously uploaded video.
    """
    # Find the file in the upload directory
    files = [f for f in os.listdir(UPLOAD_DIR) if f.startswith(video_id)]
    if not files:
        raise HTTPException(status_code=404, detail="Video payload not found.")
    
    file_path = os.path.join(UPLOAD_DIR, files[0])
    
    # Start extraction/analysis in background
    from extractor import extract_and_store
    from analyzer import check_pending_frames
    
    def run_pipeline(path, vid):
        print(f"PIPELINE_START: {vid}")
        extract_and_store(path, vid)
        print(f"ANALYSIS_START: {vid}")
        check_pending_frames(video_id=vid)
        print(f"PIPELINE_COMPLETE: {vid}")

    background_tasks.add_task(run_pipeline, file_path, video_id)
    
    return {
        "video_id": video_id,
        "status": "ANALYZING",
        "message": "Neural analysis initiated. Real-time feedback incoming."
    }

@app.get("/violations/{video_id}")
async def get_violations(video_id: str):
    """
    Fetches all detected violations for a specific video across all audited frames.
    """
    import boto3
    from boto3.dynamodb.conditions import Key
    
    TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME", "NovaFlowFrames")
    dynamodb = boto3.resource('dynamodb', region_name=os.getenv("AWS_REGION", "us-east-1"))
    table = dynamodb.Table(TABLE_NAME)
    
    try:
        response = table.query(
            KeyConditionExpression=Key('video_id').eq(video_id)
        )
        items = response.get('Items', [])
        
        all_detections = []
        for item in items:
            if 'detections' in item:
                # Add timestamp to each detection for frontend syncing
                for detection in item['detections']:
                    detection['timestamp'] = float(item.get('timestamp', 0))
                    all_detections.append(detection)
        
        return {
            "video_id": video_id,
            "detections": all_detections,
            "frame_count": len(items)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/submissions")
async def get_all_submissions():
    """
    Returns a list of all uploaded reels with their metadata (mocked for multi-agent demo).
    """
    files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith(('.mp4', '.mov', '.avi'))]
    submissions = []
    
    agent_pool = ["LLAVA-01", "BEDROCK-ALPHA", "NOVA-PROBE", "AGENT-K"]
    
    for f in files:
        video_id = f.split('.')[0]
        # In a real app, we'd query DynamoDB/MongoDB for this. Here we mock:
        submissions.append({
            "id": video_id,
            "filename": f,
            "title": f"SEQ_{video_id[-4:].upper()}",
            "agent": agent_pool[hash(video_id) % len(agent_pool)],
            "time": "12:32:01 LOCAL",
            "status": "VERIFIED" if hash(video_id) % 2 == 0 else "AUDIT_PENDING",
            "url": f"http://localhost:8000/uploads/{f}",
            "size": f"{os.path.getsize(os.path.join(UPLOAD_DIR, f)) / (1024*1024):.2f} MB"
        })
    
    return {"submissions": submissions}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
