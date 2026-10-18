import sys
import subprocess
import ffmpeg
import boto3
import io
import os
from datetime import datetime
from dotenv import load_dotenv

# Load configurations
load_dotenv()

# Configuration
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "novaflow-media")
DYNAMO_TABLE = os.getenv("DYNAMODB_TABLE_NAME", "NovaFlowFrames")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
FPS = 1
MAX_DURATION = 60  # seconds

# Initialize AWS clients
s3_client = boto3.client('s3', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
table = dynamodb.Table(DYNAMO_TABLE)

def get_video_duration(file_path):
    try:
        probe = ffmpeg.probe(file_path)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        
        if not video_stream:
            print(f"Warning: No video stream found in {file_path}.")
            return None
            
        duration = video_stream.get('duration')
        if duration is None:
            duration = probe.get('format', {}).get('duration')
            
        return float(duration) if duration else 0.0
    except Exception as e:
        print(f"Error probing video: {e}")
        return None

def extract_and_store(file_path, video_id=None):
    if not video_id:
        video_id = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    duration = get_video_duration(file_path)
    if duration is None:
        return
    
    if duration > MAX_DURATION:
        print(f"Video duration {duration}s exceeds limit of {MAX_DURATION}s. Rejecting.")
        return

    print(f"Extracting frames from {file_path} for video_id: {video_id}...")

    # Extraction process using FFmpeg
    process = (
        ffmpeg
        .input(file_path)
        .filter('fps', fps=FPS)
        .output('pipe:', format='image2pipe', vcodec='mjpeg', qscale=2)
        .run_async(pipe_stdout=True, pipe_stderr=True)
    )

    import threading
    def log_stderr(stderr):
        for line in iter(stderr.readline, b''):
            print(f"FFMPEG_STDERR: {line.decode().strip()}")

    stderr_thread = threading.Thread(target=log_stderr, args=(process.stderr,))
    stderr_thread.daemon = True
    stderr_thread.start()

    frame_count = 0
    buffer = bytearray()
    CHUNK_SIZE = 1024 * 1024 

    while True:
        chunk = process.stdout.read(CHUNK_SIZE)
        if not chunk and not buffer:
            if process.poll() is not None:
                break
            continue
        
        buffer.extend(chunk)
        
        while True:
            start = buffer.find(b"\xff\xd8")
            if start == -1:
                if not chunk and process.poll() is not None:
                    buffer.clear()
                break
            
            if start > 0:
                del buffer[:start]
            
            end = buffer.find(b"\xff\xd9")
            if end == -1:
                break
                
            frame_data = bytes(buffer[:end+2])
            del buffer[:end+2]
            
            # Store in S3
            s3_key = f"frames/{video_id}/frame_{frame_count:04d}.jpg"
            try:
                s3_client.put_object(
                    Bucket=S3_BUCKET,
                    Key=s3_key,
                    Body=frame_data,
                    ContentType='image/jpeg'
                )
                
                # Store metadata in DynamoDB
                table.put_item(Item={
                    "video_id": video_id,
                    "frame_number": frame_count,
                    "timestamp": str(frame_count / FPS),
                    "s3_key": s3_key,
                    "s3_bucket": S3_BUCKET,
                    "status": "pending_audit",
                    "created_at": datetime.now().isoformat()
                })
                
                print(f"Stored frame {frame_count} to S3 and DynamoDB")
                frame_count += 1
            except Exception as e:
                print(f"Error storing frame {frame_count}: {e}")
            
        if not chunk and process.poll() is not None:
            break

    process.wait()
    print(f"Extraction complete. Total frames stored: {frame_count}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extractor.py path/to/video.mp4 [video_id]")
        sys.exit(1)
    
    path = sys.argv[1]
    vid = sys.argv[2] if len(sys.argv) > 2 else None
    extract_and_store(path, vid)
