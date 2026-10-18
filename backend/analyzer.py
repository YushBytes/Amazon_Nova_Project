import sys
import boto3
import os
from datetime import datetime
from dotenv import load_dotenv
from prompts import SYSTEM_PROMPT

# Load configurations
load_dotenv()

S3_BUCKET = os.getenv("S3_BUCKET_NAME", "novaflow-media")
DYNAMO_TABLE = os.getenv("DYNAMODB_TABLE_NAME", "NovaFlowFrames")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Initialize AWS clients
s3_client = boto3.client('s3', region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
bedrock_runtime = boto3.client('bedrock-runtime', region_name=AWS_REGION)
table = dynamodb.Table(DYNAMO_TABLE)

def analyze_frame_with_nova(image_bytes):
    """
    Analyzes a given image using Amazon Nova 2 Pro via Bedrock.
    """
    model_id = "amazon.nova-pro-v1:0"
    
    # Construct the multimodal message for Nova
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_bytes
                        }
                    },
                    {
                        "type": "text",
                        "text": "Analyze this image for brand safety and compliance violations."
                    }
                ]
            }
        ]
    }
    
    # Note: Nova 2 Pro schema might differ slightly from Anthropic on Bedrock
    # Adapting to Nova specific request structure if needed
    # (Using the standardized Bedrock invoke format)
    
    import json
    import base64
    
    # Actually Nova Pro on Bedrock expects its own format or standard message format
    # For Nova 2 Pro, let's use the native format:
    
    nova_payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": base64.b64encode(image_bytes).decode('utf-8')}}},
                    {"text": "Analyze this image carefully for brand safety violations."}
                ]
            }
        ],
        "system": [{"text": SYSTEM_PROMPT}],
        "inferenceConfig": {"maxNewTokens": 1000}
    }

    try:
        response = bedrock_runtime.invoke_model(
            modelId=model_id,
            body=json.dumps(nova_payload)
        )
        
        response_body = json.loads(response.get('body').read())
        raw_text = response_body['output']['message']['content'][0]['text']
        
        # Extract JSON from the response text
        import re
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except:
                return {"scene_description": raw_text, "violations": [], "error": "JSON_PARSE_FAILED"}
        return {"scene_description": raw_text, "violations": [], "raw_report": raw_text}
    except Exception as e:
        print(f"Error calling Amazon Nova: {e}")
        return {"error": str(e), "violations": []}

def check_pending_frames(video_id=None):
    """
    Checks DynamoDB for frames marked as pending_audit,
    runs them through Nova 2 Pro, and updates their status.
    """
    from boto3.dynamodb.conditions import Attr
    
    print(f"Checking for pending frames in {DYNAMO_TABLE}...")
    
    # For scale, you'd use a GSI on status. For POC, we'll scan.
    scan_kwargs = {'FilterExpression': Attr('status').eq('pending_audit')}
    if video_id:
        scan_kwargs['FilterExpression'] &= Attr('video_id').eq(video_id)
        
    try:
        response = table.scan(**scan_kwargs)
        pending_frames = response.get('Items', [])
        # Sort by frame number manually if needed
        pending_frames.sort(key=lambda x: int(x['frame_number']))
    except Exception as e:
        print(f"DynamoDB Scan Error: {e}")
        return

    if not pending_frames:
        print(f"No pending frames found{' for video ' + video_id if video_id else ''}.")
        return
        
    print(f"Found {len(pending_frames)} pending frames. Starting Amazon Nova 2 Pro analysis...")
    
    for meta in pending_frames:
        vid = meta['video_id']
        fnum = meta['frame_number']
        s3_key = meta['s3_key']
        bucket = meta['s3_bucket']
        
        print(f"\nAnalyzing frame {fnum} from video {vid} (s3://{bucket}/{s3_key})...")
        
        try:
            # 1. Retrieve image bytes from S3
            s3_obj = s3_client.get_object(Bucket=bucket, Key=s3_key)
            frame_bytes = s3_obj['Body'].read()
            
            # 2. Analyze the image with Nova
            analysis = analyze_frame_with_nova(frame_bytes)
            
            print(f"--- FRAME {fnum} ANALYSIS REPORT (NOVA) ---")
            print(json.dumps(analysis, indent=2))
            print("-" * 40)
            
            # 3. Update DynamoDB
            table.update_item(
                Key={'video_id': vid, 'frame_number': fnum},
                UpdateExpression="SET #s = :s, analysis_report = :r, detections = :d, updated_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "audited",
                    ":r": json.dumps(analysis),
                    ":d": analysis.get('violations', []),
                    ":t": datetime.now().isoformat()
                }
            )
            print(f"-> Successfully updated DynamoDB with {len(analysis.get('violations', []))} detections.")
            
        except Exception as e:
            print(f"-> Error auditing frame {fnum}: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        
        # Test a local image file
        if target.endswith(('.jpg', '.png', '.jpeg')):
            print(f"Analyzing local file: {target}...")
            with open(target, "rb") as f:
                print(analyze_frame_with_nova(f.read()))
        # Test a specific video ID from DB
        else:
            check_pending_frames(video_id=target)
    else:
        # Audit all pending frames
        print("Auditing all pending frames in DynamoDB...")
        check_pending_frames()