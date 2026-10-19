import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from decimal import Decimal
from dotenv import load_dotenv

from config import get_settings
from detections import (
    AnalysisParseError,
    FrameAnalysis,
    failed_analysis,
    parse_frame_analysis,
)
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

# ---------------------------------------------------------------------------
# Model configuration, in one place
# ---------------------------------------------------------------------------

# The model this pipeline is built and prompted for. Switching it changes
# detection behaviour, so it is a single named constant rather than a literal
# repeated at each call site. It is NOT read from the environment: an operator
# silently pointing this at a different model would invalidate every stored
# result without the pipeline version changing.
MODEL_ID = "amazon.nova-pro-v1:0"

MAX_OUTPUT_TOKENS = 1000

# Stored alongside each result so a detection can be traced to the code that
# produced it. Kept in step with video_repo.PIPELINE_VERSION.
PIPELINE_VERSION = "4.0"

# Safety bound on scan pagination.
MAX_SCAN_PAGES = 50

# Bedrock error codes worth retrying. Everything else (validation,
# authorization, model-not-found) is permanent and must fail immediately
# rather than burning the attempt budget.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)

# Permanent failures. Named so a reader can see they are deliberately not
# retried.
PERMANENT_ERROR_CODES = frozenset(
    {
        "ValidationException",
        "AccessDeniedException",
        "UnrecognizedClientException",
        "ResourceNotFoundException",
        "ModelErrorException",
    }
)


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") or type(exc).__name__


def build_request_body(image_bytes: bytes) -> str:
    """Build the Bedrock invoke_model body for one frame.

    Nova's native message format: an image part carrying base64 bytes plus a
    text instruction, with the compliance contract supplied as a system
    prompt. Kept as its own function so the request shape is testable without
    a Bedrock call.
    """
    payload = {
        "schemaVersion": "messages-v1",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": "jpeg",
                            "source": {
                                "bytes": base64.b64encode(image_bytes).decode("utf-8")
                            },
                        }
                    },
                    {
                        "text": (
                            "Analyze this video frame for brand-compliance "
                            "violations. Respond with only the JSON object "
                            "described in your instructions."
                        )
                    },
                ],
            }
        ],
        "system": [{"text": SYSTEM_PROMPT}],
        "inferenceConfig": {"maxNewTokens": MAX_OUTPUT_TOKENS},
    }
    return json.dumps(payload)


def _read_model_text(response) -> str:
    """Pull the assistant text out of a Bedrock invoke_model response."""
    body = json.loads(response.get("body").read())
    try:
        return body["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AnalysisParseError(
            "the model response did not contain assistant text"
        ) from exc

def analyze_frame_with_nova(image_bytes: bytes) -> FrameAnalysis:
    """Analyse one frame with Amazon Nova Pro and validate the result.

    Returns a FrameAnalysis either way. `result.ok` is False when the frame
    could not be audited, which the caller must keep distinct from "audited
    with zero detections" - conflating them would report unanalysed footage as
    compliant.

    Retries are bounded (`BEDROCK_MAX_ATTEMPTS`, default 3) with exponential
    backoff, and only for the error codes in RETRYABLE_ERROR_CODES. Validation
    and authorization failures are permanent and fail on the first attempt.
    """
    settings = get_settings()
    max_attempts = settings.bedrock_max_attempts
    body = build_request_body(image_bytes)

    last_reason = "analysis did not complete"

    for attempt in range(1, max_attempts + 1):
        try:
            response = bedrock_runtime.invoke_model(modelId=MODEL_ID, body=body)
            raw_text = _read_model_text(response)
            return parse_frame_analysis(raw_text)

        except ClientError as exc:
            code = _error_code(exc)
            if code in RETRYABLE_ERROR_CODES and attempt < max_attempts:
                delay = _backoff_seconds(attempt)
                logger.warning(
                    "Bedrock %s on attempt %d/%d; retrying in %.1fs",
                    code, attempt, max_attempts, delay,
                )
                time.sleep(delay)
                last_reason = f"{code} after {attempt} attempt(s)"
                continue
            # Permanent, or the budget is spent. Log the code only: a Bedrock
            # error body can echo request content, which includes frame data.
            logger.error("Bedrock call failed with %s", code)
            return failed_analysis(f"{code}")

        except BotoCoreError as exc:
            code = type(exc).__name__
            if attempt < max_attempts:
                delay = _backoff_seconds(attempt)
                logger.warning(
                    "Bedrock transport error %s on attempt %d/%d; retrying in %.1fs",
                    code, attempt, max_attempts, delay,
                )
                time.sleep(delay)
                last_reason = f"{code} after {attempt} attempt(s)"
                continue
            logger.error("Bedrock transport error: %s", code)
            return failed_analysis(code)

        except AnalysisParseError as exc:
            # The model answered but not usably. Retrying rarely helps and
            # costs another call, so record the failure.
            logger.warning("Unusable model response: %s", exc)
            return failed_analysis(f"MALFORMED_RESPONSE: {exc}")

        except Exception as exc:
            logger.exception("Unexpected failure analysing a frame")
            return failed_analysis(f"UNEXPECTED: {type(exc).__name__}")

    return failed_analysis(last_reason)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with a small deterministic jitter.

    Jitter is derived from the attempt number rather than `random`, so tests
    are reproducible while concurrent workers still stagger slightly.
    """
    base = min(2.0 ** (attempt - 1), 8.0)
    return base + (attempt % 3) * 0.1



def _detection_item(detection) -> dict:
    """Serialise one validated detection for DynamoDB.

    Optional fields are omitted entirely when absent rather than written as
    null or a default, so a reader can tell "the model gave no confidence"
    from "the model said zero".
    """
    item = {
        "category": detection.category,
        "description": detection.description,
    }
    if detection.risk_level is not None:
        item["risk_level"] = detection.risk_level
    if detection.confidence is not None:
        # DynamoDB has no float type; Decimal round-trips exactly.
        item["confidence"] = Decimal(str(detection.confidence))
    if detection.bounding_box is not None:
        item["bounding_box"] = [Decimal(str(v)) for v in detection.bounding_box]
    return item


def _record_frame_result(
    video_id: str, frame_number, status: str, analysis: FrameAnalysis
) -> int:
    """Write one frame's terminal audit state to DynamoDB.

    Idempotent: the same frame audited twice produces the same item, so a
    retry overwrites rather than appending. Returns the number of detections
    stored.

    Keeps the established attribute contract (`status`, `analysis_report`,
    `detections`, `updated_at`) and adds traceability fields.
    """
    audited = status == STATUS_AUDITED

    # Only an audited frame contributes detections. A failed frame stores an
    # empty list, so a reader can never mistake a failure for "no violations".
    detections = [_detection_item(d) for d in analysis.detections] if audited else []

    report = {
        "scene_description": analysis.scene_description if audited else "",
        "recommendation": analysis.recommendation if audited else "",
        "detection_count": len(detections),
        "discarded_entries": analysis.discarded_entries,
    }
    if analysis.error:
        report["error"] = analysis.error

    table.update_item(
        Key={"video_id": video_id, "frame_number": frame_number},
        UpdateExpression=(
            "SET #s = :s, analysis_report = :r, detections = :d, "
            "updated_at = :t, model_id = :m, pipeline_version = :p"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status,
            ":r": json.dumps(report),
            ":d": detections,
            ":t": datetime.now(timezone.utc).isoformat(),
            ":m": MODEL_ID,
            ":p": PIPELINE_VERSION,
        },
    )
    return len(detections)


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
        # DynamoDB returns at most 1 MB per page, so one scan call is not the
        # whole queue. Page until exhausted, bounded so a runaway table cannot
        # spin forever.
        pending_frames = []
        pages = 0
        while True:
            response = table.scan(**scan_kwargs)
            pending_frames.extend(response.get('Items', []))
            cursor = response.get('LastEvaluatedKey')
            pages += 1
            if not cursor or pages >= MAX_SCAN_PAGES:
                if cursor:
                    logger.warning(
                        "Stopped scanning pending frames after %d page(s); "
                        "more may remain", pages,
                    )
                break
            scan_kwargs['ExclusiveStartKey'] = cursor
        pending_frames.sort(key=lambda x: int(x['frame_number']))
    except Exception as e:
        # A scan failure means the batch is unknown, not that there is nothing
        # to do. Surface it instead of returning as if the queue were empty.
        raise FrameAuditError(
            f"Could not scan {DYNAMO_TABLE} for pending frames: {type(e).__name__}"
        ) from e

    summary = {
        "pending": len(pending_frames),
        "audited": 0,
        "failed": 0,
        "detections": 0,
    }

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

            # 2. Analyse the frame. Returns a FrameAnalysis either way;
            #    `ok` False means no usable report was produced.
            analysis = analyze_frame_with_nova(frame_bytes)
        except Exception as e:
            # No result could be obtained for this frame at all (missing S3
            # object, malformed metadata, transport failure). Record the
            # failure so the frame is not retried forever as still-pending.
            logger.error(
                "Frame %s/%s could not be analysed: %s", vid, fnum, type(e).__name__
            )
            analysis = failed_analysis(type(e).__name__)

        if analysis.ok:
            status = STATUS_AUDITED
            summary["audited"] += 1
        else:
            status = STATUS_FAILED
            summary["failed"] += 1

        try:
            stored = _record_frame_result(vid, fnum, status, analysis)
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
            summary["detections"] += stored
            logger.info(
                "Frame %s/%s audited: %d detection(s) stored", vid, fnum, stored
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
            print(result.model_dump_json(indent=2))
        # Test a specific video ID from DB
        else:
            check_pending_frames(video_id=target)
    else:
        # Audit all pending frames
        print("Auditing all pending frames in DynamoDB...")
        check_pending_frames()