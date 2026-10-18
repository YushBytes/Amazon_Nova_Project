import base64
import json
import logging
import os
import re
import sys
from datetime import datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

from prompts import SYSTEM_PROMPT

# Load configurations
load_dotenv()

logger = logging.getLogger(__name__)

# Frame audit lifecycle states, written to the DynamoDB `status` attribute.
# extractor.py creates frames as STATUS_PENDING; this module moves each one
# to exactly one terminal state so a processed frame is never ambiguous.
STATUS_PENDING = "pending_audit"
STATUS_AUDITED = "audited"
STATUS_FAILED = "audit_failed"


class FrameAuditError(RuntimeError):
    """Raised when the audit batch cannot run at all.

    Used for failures affecting the whole batch (for example the DynamoDB
    scan failing), as distinct from a single frame failing, which is
    recorded against that frame and does not abort the run.
    """

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
    
    # Nova Pro on Bedrock expects its own native message format.
    
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
        
        # Extract the JSON object from the response text.
        # NOTE: this greedy pattern spans the first '{' to the last '}' and is
        # unreliable when a response contains several JSON objects. Replacing it
        # with schema-validated parsing is tracked for a later phase; for now a
        # parse failure is reported as a failure rather than as a clean frame.
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                logger.warning("Model response was not valid JSON; frame will not be audited")
                return {"scene_description": raw_text, "violations": [], "error": "JSON_PARSE_FAILED"}
        logger.warning("Model response contained no JSON object; frame will not be audited")
        return {
            "scene_description": raw_text,
            "violations": [],
            "raw_report": raw_text,
            "error": "NO_JSON_IN_RESPONSE",
        }
    except (BotoCoreError, ClientError) as e:
        # Log the exception type, not its body: Bedrock errors can echo request
        # content, which includes user frame data.
        logger.error("Bedrock invocation failed: %s", type(e).__name__)
        return {"error": f"{type(e).__name__}: {e}", "violations": []}
    except (KeyError, IndexError, TypeError) as e:
        logger.error("Unexpected Bedrock response shape: %s", type(e).__name__)
        return {"error": f"MALFORMED_RESPONSE: {type(e).__name__}", "violations": []}

def _record_frame_result(video_id, frame_number, status, analysis):
    """Write a single frame's terminal audit state to DynamoDB.

    Preserves the existing attribute contract: `status`, `analysis_report`
    (a JSON string), `detections` (a list) and `updated_at`.
    """
    table.update_item(
        Key={'video_id': video_id, 'frame_number': frame_number},
        UpdateExpression=(
            "SET #s = :s, analysis_report = :r, detections = :d, updated_at = :t"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status,
            ":r": json.dumps(analysis),
            # Only an audited frame contributes detections. A failed frame
            # stores an empty list, so a reader can never mistake a failure
            # for "no violations found".
            ":d": analysis.get('violations', []) if status == STATUS_AUDITED else [],
            ":t": datetime.now().isoformat(),
        },
    )


def check_pending_frames(video_id=None):
    """Audit every frame currently marked `pending_audit`.

    Each processed frame ends in exactly one terminal state: STATUS_AUDITED
    when the model returned a usable report, or STATUS_FAILED when it did not.
    One frame failing does not abort the batch, but the failure is recorded
    rather than silently skipped.

    Returns:
        dict: {"pending": int, "audited": int, "failed": int}

    Raises:
        FrameAuditError: if the pending frames cannot be enumerated at all.
    """
    from boto3.dynamodb.conditions import Attr

    logger.info("Checking for pending frames in %s", DYNAMO_TABLE)

    # At scale this should use a GSI on `status`; the scan is retained to
    # preserve existing behaviour and is tracked for a later phase.
    scan_kwargs = {'FilterExpression': Attr('status').eq(STATUS_PENDING)}
    if video_id:
        scan_kwargs['FilterExpression'] &= Attr('video_id').eq(video_id)

    try:
        response = table.scan(**scan_kwargs)
        pending_frames = response.get('Items', [])
        pending_frames.sort(key=lambda x: int(x['frame_number']))
    except Exception as e:
        # A scan failure means the batch is unknown, not that there is nothing
        # to do. Surface it instead of returning as if the queue were empty.
        raise FrameAuditError(
            f"Could not scan {DYNAMO_TABLE} for pending frames: {type(e).__name__}"
        ) from e

    summary = {"pending": len(pending_frames), "audited": 0, "failed": 0}

    if not pending_frames:
        logger.info(
            "No pending frames found%s", f" for video {video_id}" if video_id else ""
        )
        return summary

    logger.info(
        "Found %d pending frame(s); starting Amazon Nova analysis", len(pending_frames)
    )

    for meta in pending_frames:
        vid = meta['video_id']
        fnum = meta['frame_number']

        try:
            s3_key = meta['s3_key']
            bucket = meta['s3_bucket']

            # 1. Retrieve the frame bytes from S3.
            s3_obj = s3_client.get_object(Bucket=bucket, Key=s3_key)
            frame_bytes = s3_obj['Body'].read()

            # 2. Analyse the frame. This returns a dict either way; an "error"
            #    key means no usable report was produced.
            analysis = analyze_frame_with_nova(frame_bytes)
        except Exception as e:
            # No result could be obtained for this frame at all (missing S3
            # object, malformed metadata, transport failure). Record the
            # failure so the frame is not retried forever as still-pending.
            logger.error(
                "Frame %s/%s could not be analysed: %s", vid, fnum, type(e).__name__
            )
            analysis = {"error": f"{type(e).__name__}: {e}", "violations": []}

        if analysis.get("error"):
            status = STATUS_FAILED
            summary["failed"] += 1
        else:
            status = STATUS_AUDITED
            summary["audited"] += 1

        try:
            _record_frame_result(vid, fnum, status, analysis)
        except Exception as e:
            # The result could not be persisted, so do not report an audit
            # that was never stored.
            if status == STATUS_AUDITED:
                summary["audited"] -= 1
                summary["failed"] += 1
            logger.error(
                "Could not persist audit result for frame %s/%s: %s",
                vid, fnum, type(e).__name__,
            )
            continue

        # Log counts only. The analysis body contains scene descriptions of
        # user video content and must not be written to logs.
        if status == STATUS_AUDITED:
            logger.info(
                "Frame %s/%s audited: %d detection(s) stored",
                vid, fnum, len(analysis.get('violations', [])),
            )
        else:
            logger.warning("Frame %s/%s marked %s", vid, fnum, STATUS_FAILED)

    logger.info(
        "Audit batch complete: %d audited, %d failed, of %d pending",
        summary["audited"], summary["failed"], summary["pending"],
    )
    return summary

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        
        # Test a local image file
        if target.endswith(('.jpg', '.png', '.jpeg')):
            print(f"Analyzing local file: {target}...")
            with open(target, "rb") as f:
                result = analyze_frame_with_nova(f.read())
            # Explicit local CLI inspection of a developer-supplied file.
            print(json.dumps(result, indent=2))
        # Test a specific video ID from DB
        else:
            check_pending_frames(video_id=target)
    else:
        # Audit all pending frames
        print("Auditing all pending frames in DynamoDB...")
        check_pending_frames()