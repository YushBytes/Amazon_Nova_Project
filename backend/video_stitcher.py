import os
import subprocess
import boto3
from dotenv import load_dotenv
from datetime import datetime

# Load configurations
load_dotenv()

S3_BUCKET = os.getenv("S3_BUCKET_NAME", "novaflow-media")
DYNAMO_TABLE = os.getenv("DYNAMODB_TABLE_NAME", "NovaFlowFrames")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Initialize AWS clients
s3_client = boto3.client('s3', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
table = dynamodb.Table(DYNAMO_TABLE)

def reconstruct_video(video_id, output_filename="final_compliant_video.mp4"):
    """
    Downloads all frames from S3 (metadata from DynamoDB) and stitches them using FFmpeg.
    """
    print(f"Starting reconstruction for Video: {video_id}...")
    
    # 1. Fetch all frames from DynamoDB
    from boto3.dynamodb.conditions import Key
    try:
        response = table.query(
            KeyConditionExpression=Key('video_id').eq(video_id)
        )
        frames = response.get('Items', [])
        # Sort by frame number
        frames.sort(key=lambda x: int(x['frame_number']))
    except Exception as e:
        print(f"DynamoDB Query Error: {e}")
        return
    
    if not frames:
        print("No frames found for this video ID.")
        return
    
    # 2. Setup temporary directory for frames
    temp_dir = f"temp_frames_{video_id}"
    os.makedirs(temp_dir, exist_ok=True)
    
    print(f"Downloading {len(frames)} frames from S3 to temporary directory...")
    
    for f in frames:
        frame_num = int(f["frame_number"])
        s3_key = f["s3_key"]
        
        try:
            # Download from S3
            frame_path = os.path.join(temp_dir, f"frame_{frame_num:04d}.jpg")
            s3_client.download_file(S3_BUCKET, s3_key, frame_path)
        except Exception as e:
            print(f"Error downloading frame {frame_num} from S3: {e}")

    # 3. Trigger FFmpeg
    input_pattern = os.path.join(temp_dir, "frame_%04d.jpg")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-framerate", "1",
        "-i", input_pattern,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        output_filename
    ]
    
    print(f"Running FFmpeg: {' '.join(ffmpeg_cmd)}")
    try:
        subprocess.run(ffmpeg_cmd, check=True)
        print(f"Success! Final video created: {output_filename}")
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg failed: {e}")
    finally:
        # Cleanup temp directory
        import shutil
        print(f"Cleaning up {temp_dir}...")
        # shutil.rmtree(temp_dir) # Uncomment for production cleanup
        pass

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python video_stitcher.py video_id [output_name.mp4]")
    else:
        vid = sys.argv[1]
        out = sys.argv[2] if len(sys.argv) > 2 else "final_compliant_video.mp4"
        reconstruct_video(vid, out)
