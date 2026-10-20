"""Regression tests for Phase 2 defect A: frame analysis never persisted results.

Audit reference: docs/PROJECT_AUDIT.md section 9.1.

`analyzer.py` imported `json` only inside `analyze_frame_with_nova`, so the
`json.dumps` calls inside `check_pending_frames` raised
`NameError: name 'json' is not defined`. That NameError was swallowed by a bare
`except Exception`, which meant the `table.update_item` call that persists
detections was never reached for any frame: every frame stayed
`status="pending_audit"` forever and `GET /violations/{id}` always returned an
empty list.

These tests pin down the real behaviour that matters:
  * the module can serialize analysis payloads at all (the missing import),
  * a successful analysis reaches the existing DynamoDB persistence path with
    the documented attribute contract,
  * a failed analysis is NOT recorded as a successful audit.

All AWS access is mocked. No test performs network I/O or incurs Bedrock cost.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import analyzer
from detections import Detection, FrameAnalysis, failed_analysis


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

PENDING_FRAME = {
    "video_id": "vid_test01",
    "frame_number": 0,
    "timestamp": "0.0",
    "s3_key": "frames/vid_test01/frame_0000.jpg",
    "s3_bucket": "test-bucket",
    "status": "pending_audit",
}

# Phase 4: analyze_frame_with_nova returns a validated FrameAnalysis rather
# than a raw dict, so the stand-in is built with the real model. The intent of
# every test below is unchanged.
VALID_ANALYSIS = FrameAnalysis(
    scene_description="A person holding a beverage can.",
    recommendation="Blur the logo region.",
    detections=[
        Detection(
            category="competitor_logo",
            description="Competitor branding visible on can.",
            confidence=0.91,
            bounding_box=[100.0, 200.0, 300.0, 400.0],
            risk_level="high",
        )
    ],
)


def _mock_table(items):
    """A stand-in for the module-level DynamoDB Table resource.

    Phase 6 moved the per-video read from Scan to Query, so `query` is what
    the pipeline path calls. `scan` is still stubbed because the maintenance
    path (no video id) legitimately uses it. Neither returns a
    `LastEvaluatedKey`, so both are single-page.
    """
    table = MagicMock()
    page = {"Items": list(items)}
    table.query.return_value = dict(page)
    table.scan.return_value = dict(page)
    return table


def _mock_s3(frame_bytes):
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = frame_bytes
    s3.get_object.return_value = {"Body": body}
    return s3


# --------------------------------------------------------------------------
# the missing import itself
# --------------------------------------------------------------------------

def test_json_is_available_at_module_scope():
    """`check_pending_frames` serializes with `json`, so the module needs it.

    This is the direct regression test for the NameError. Before the fix,
    `analyzer.json` did not exist because the import lived inside another
    function's body.
    """
    assert hasattr(analyzer, "json"), (
        "analyzer must import json at module scope; check_pending_frames "
        "serializes analysis reports with json.dumps"
    )
    assert analyzer.json.dumps({"violations": []}) == '{"violations": []}'


def test_regex_module_available_at_module_scope():
    """`analyze_frame_with_nova` extracts JSON with `re`; same import problem."""
    assert hasattr(analyzer, "re"), "analyzer must import re at module scope"


# --------------------------------------------------------------------------
# success path -> existing persistence contract
# --------------------------------------------------------------------------

def test_successful_analysis_is_persisted_to_dynamodb(frame_jpeg_bytes):
    """A successful analysis must reach the existing update_item call.

    Pre-fix this failed: update_item was never called at all, because
    json.dumps raised NameError two lines earlier.
    """
    table = _mock_table([PENDING_FRAME])
    s3 = _mock_s3(frame_jpeg_bytes)

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", return_value=VALID_ANALYSIS):
        analyzer.check_pending_frames(video_id="vid_test01")

    assert table.update_item.called, (
        "a successful analysis must be written back to DynamoDB"
    )
    kwargs = table.update_item.call_args.kwargs

    # Key contract: partition key video_id + sort key frame_number.
    assert kwargs["Key"] == {"video_id": "vid_test01", "frame_number": 0}

    values = kwargs["ExpressionAttributeValues"]
    assert values[":s"] == "audited"
    # detections is stored as a list for GET /violations/{video_id} to read.
    stored = values[":d"]
    assert len(stored) == 1
    assert stored[0]["category"] == "competitor_logo"
    # analysis_report is a JSON summary, not the raw model payload.
    report = json.loads(values[":r"])
    assert report["detection_count"] == 1
    assert report["scene_description"] == VALID_ANALYSIS.scene_description
    assert kwargs["ExpressionAttributeNames"] == {"#s": "status"}
    assert ":t" in values  # updated_at timestamp
    # Phase 4 traceability.
    assert values[":m"] == analyzer.MODEL_ID
    assert values[":p"] == analyzer.PIPELINE_VERSION


def test_detections_survive_round_trip_for_violations_endpoint(frame_jpeg_bytes):
    """The stored detections must carry the fields the frontend overlay reads.

    HUDView renders d.bounding_box[0..3], d.category and d.confidence, so the
    persisted shape has to preserve them.
    """
    table = _mock_table([PENDING_FRAME])
    s3 = _mock_s3(frame_jpeg_bytes)

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", return_value=VALID_ANALYSIS):
        analyzer.check_pending_frames(video_id="vid_test01")

    stored = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":d"]
    assert len(stored) == 1
    detection = stored[0]
    assert detection["category"] == "competitor_logo"
    # Stored as Decimal so DynamoDB round-trips it exactly.
    assert float(detection["confidence"]) == pytest.approx(0.91)
    assert len(detection["bounding_box"]) == 4


def test_frame_with_no_violations_is_still_audited(frame_jpeg_bytes):
    """A clean frame is a successful audit with an empty detection list."""
    clean = FrameAnalysis(scene_description="An empty room.", detections=[])
    table = _mock_table([PENDING_FRAME])
    s3 = _mock_s3(frame_jpeg_bytes)

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", return_value=clean):
        analyzer.check_pending_frames(video_id="vid_test01")

    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":s"] == "audited"
    assert values[":d"] == []


# --------------------------------------------------------------------------
# failure paths must NOT look like success
# --------------------------------------------------------------------------

def test_bedrock_error_is_not_recorded_as_a_successful_audit(frame_jpeg_bytes):
    """A model error must not leave the frame marked 'audited'.

    analyze_frame_with_nova returns {"error": ..., "violations": []} when the
    Bedrock call fails. Treating that as a clean audit would report "no
    violations found" for a frame that was never actually analysed.
    """
    failed = failed_analysis("ThrottlingException: rate exceeded")
    table = _mock_table([PENDING_FRAME])
    s3 = _mock_s3(frame_jpeg_bytes)

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", return_value=failed):
        analyzer.check_pending_frames(video_id="vid_test01")

    assert table.update_item.called, "a failed analysis must still be recorded"
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":s"] != "audited", (
        "a frame whose analysis failed must not be marked as audited"
    )
    assert values[":s"] == analyzer.STATUS_FAILED


def test_unparseable_model_output_is_not_a_successful_audit(frame_jpeg_bytes):
    """A JSON parse failure is a failure, not a clean frame."""
    unparseable = failed_analysis("JSON_PARSE_FAILED")
    table = _mock_table([PENDING_FRAME])
    s3 = _mock_s3(frame_jpeg_bytes)

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", return_value=unparseable):
        analyzer.check_pending_frames(video_id="vid_test01")

    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":s"] == analyzer.STATUS_FAILED


def test_s3_fetch_failure_marks_frame_failed_not_pending(frame_jpeg_bytes):
    """A frame whose bytes cannot be fetched must not be left pending forever.

    Leaving status='pending_audit' makes the frame indistinguishable from one
    that has not been processed yet, so it would be retried endlessly while the
    job silently appears incomplete.
    """
    table = _mock_table([PENDING_FRAME])
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("NoSuchKey")

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3):
        analyzer.check_pending_frames(video_id="vid_test01")

    assert table.update_item.called, (
        "an unfetchable frame must be recorded as failed, not silently skipped"
    )
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":s"] == analyzer.STATUS_FAILED


def test_summary_reports_real_counts(frame_jpeg_bytes):
    """The batch result must distinguish audited frames from failed ones."""
    frames = [
        dict(PENDING_FRAME, frame_number=0),
        dict(PENDING_FRAME, frame_number=1),
    ]
    table = _mock_table(frames)
    s3 = _mock_s3(frame_jpeg_bytes)

    results = [VALID_ANALYSIS, failed_analysis("boom")]

    with patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", side_effect=results):
        summary = analyzer.check_pending_frames(video_id="vid_test01")

    assert summary["audited"] == 1
    assert summary["failed"] == 1
    assert summary["pending"] == 2
    # Phase 4 also reports how many detections were stored.
    assert summary["detections"] == 1


def test_no_pending_frames_is_not_an_error():
    """An empty queue is a no-op, not a failure."""
    table = _mock_table([])
    with patch.object(analyzer, "table", table):
        summary = analyzer.check_pending_frames(video_id="vid_none")

    assert summary["pending"] == 0
    assert summary["audited"] == 0
    assert summary["failed"] == 0
    assert not table.update_item.called


def test_frame_enumeration_failure_is_surfaced_not_swallowed():
    """If reading the frame table fails, that must be reported, not ignored."""
    table = MagicMock()
    table.query.side_effect = RuntimeError("ResourceNotFoundException")
    table.scan.side_effect = RuntimeError("ResourceNotFoundException")

    with patch.object(analyzer, "table", table):
        with pytest.raises(analyzer.FrameAuditError):
            analyzer.check_pending_frames(video_id="vid_test01")


# --------------------------------------------------------------------------
# logging must not leak frame content
# --------------------------------------------------------------------------

def test_analysis_content_is_not_logged(frame_jpeg_bytes, caplog):
    """Scene descriptions describe user video content; keep them out of logs."""
    table = _mock_table([PENDING_FRAME])
    s3 = _mock_s3(frame_jpeg_bytes)

    with caplog.at_level("DEBUG"), \
         patch.object(analyzer, "table", table), \
         patch.object(analyzer, "s3_client", s3), \
         patch.object(analyzer, "analyze_frame_with_nova", return_value=VALID_ANALYSIS):
        analyzer.check_pending_frames(video_id="vid_test01")

    logged = caplog.text
    assert "A person holding a beverage can" not in logged
    assert "Blur the logo region" not in logged
