"""Phase 4: Nova Pro request/response handling and validation.

Model output was previously parsed with a greedy regex and stored verbatim:
whatever JSON the model produced went to DynamoDB and then to the browser, and
malformed output was indistinguishable from a clean frame.

These tests treat the response as untrusted input. Bedrock is mocked
throughout — **no test here calls a real model**, and none is evidence that
live Bedrock integration works.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import analyzer
from detections import (
    AnalysisParseError,
    BoundingBox,
    Detection,
    FrameAnalysis,
    extract_json_object,
    parse_frame_analysis,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bedrock_response(text):
    body = MagicMock()
    body.read.return_value = json.dumps(
        {"output": {"message": {"content": [{"text": text}]}}}
    ).encode()
    return {"body": body}


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel")


GOOD_RESPONSE = json.dumps(
    {
        "scene_description": "A person holding a can.",
        "violations": [
            {
                "category": "competitor_logo",
                "description": "Competitor branding visible.",
                "confidence": 0.91,
                "bounding_box": [100, 200, 300, 400],
                "risk_level": "High",
            }
        ],
        "recommendation": "Blur the region.",
    }
)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make backoff instant so retry tests do not actually wait."""
    monkeypatch.setattr(analyzer.time, "sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------

def test_request_uses_the_configured_nova_model():
    """The model must not drift silently; it is a named constant."""
    assert analyzer.MODEL_ID == "amazon.nova-pro-v1:0"


def test_request_body_carries_the_frame_and_the_system_prompt():
    from prompts import SYSTEM_PROMPT

    body = json.loads(analyzer.build_request_body(b"\xff\xd8jpegbytes\xff\xd9"))

    assert body["schemaVersion"] == "messages-v1"
    assert body["system"][0]["text"] == SYSTEM_PROMPT
    content = body["messages"][0]["content"]
    image = next(part for part in content if "image" in part)
    assert image["image"]["format"] == "jpeg"
    # Base64, not raw bytes: raw bytes are not JSON serialisable.
    assert isinstance(image["image"]["source"]["bytes"], str)
    assert body["inferenceConfig"]["maxNewTokens"] == analyzer.MAX_OUTPUT_TOKENS


def test_request_body_is_json_serialisable():
    """The pre-Phase-2 code put raw bytes in the payload, which always raised."""
    assert isinstance(analyzer.build_request_body(b"\x00\x01\x02"), str)


# ---------------------------------------------------------------------------
# response validation
# ---------------------------------------------------------------------------

def test_valid_response_is_parsed_into_detections():
    result = parse_frame_analysis(GOOD_RESPONSE)
    assert result.ok is True
    assert result.detection_count == 1
    detection = result.detections[0]
    assert detection.category == "competitor_logo"
    assert detection.confidence == pytest.approx(0.91)
    assert detection.bounding_box == [100.0, 200.0, 300.0, 400.0]
    assert detection.risk_level == "high"


def test_clean_frame_is_a_successful_parse_with_no_detections():
    """Distinct from a failure: the model answered, and found nothing."""
    result = parse_frame_analysis('{"scene_description":"empty","violations":[]}')
    assert result.ok is True
    assert result.detection_count == 0
    assert result.error is None


@pytest.mark.parametrize(
    "text",
    ["", "   ", "no json at all", "I cannot analyse this image.", "{unclosed"],
)
def test_unparseable_response_raises(text):
    with pytest.raises(AnalysisParseError):
        parse_frame_analysis(text)


def test_multiple_json_objects_are_handled():
    """The old greedy regex spanned the first '{' to the last '}'.

    On a response with two objects that produced invalid JSON, which the bare
    except then turned into a "clean frame".
    """
    text = 'Thinking: {"step":1} Result: {"violations":[],"scene_description":"ok"} done'
    result = parse_frame_analysis(text)
    assert result.ok is True
    assert result.scene_description == "ok"


def test_fenced_code_block_is_handled():
    text = "```json\n" + GOOD_RESPONSE + "\n```"
    assert parse_frame_analysis(text).detection_count == 1


def test_braces_inside_strings_do_not_break_parsing():
    text = '{"scene_description":"a sign reading {SALE}","violations":[]}'
    result = parse_frame_analysis(text)
    assert "{SALE}" in result.scene_description


def test_violations_not_a_list_is_a_parse_error():
    with pytest.raises(AnalysisParseError):
        parse_frame_analysis('{"violations":"lots"}')


def test_a_single_violation_object_is_accepted():
    result = parse_frame_analysis(
        '{"violations":{"category":"logo","description":"x"}}'
    )
    assert result.detection_count == 1


# ---------------------------------------------------------------------------
# hallucinated and malformed fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "box",
    [
        [400, 400, 100, 100],      # inverted
        [0, 0, 0, 0],              # degenerate
        [-5, 10, 50, 60],          # out of range low
        [10, 10, 5000, 60],        # out of range high
        [1, 2, 3],                 # too short
        [1, 2, 3, 4, 5],           # too long
        ["a", "b", "c", "d"],      # non-numeric
        [1, 2, 3, None],
        [True, False, True, False],
        "100,200,300,400",
        None,
    ],
)
def test_unusable_bounding_boxes_are_dropped_not_rendered(box):
    """A hallucinated box must cost the box, not the whole detection."""
    result = parse_frame_analysis(
        json.dumps({"violations": [{"category": "logo", "bounding_box": box}]})
    )
    assert result.ok is True
    assert result.detection_count == 1
    assert result.detections[0].bounding_box is None


def test_valid_box_is_preserved():
    assert BoundingBox.parse([0, 0, 1000, 1000]) is not None
    assert BoundingBox.parse([10, 20, 30, 40]).as_list() == [10.0, 20.0, 30.0, 40.0]


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (0.0, 0.0),
        (0.5, 0.5),
        (1.0, 1.0),
        (91, 0.91),            # percentage rescaled
        (100, 1.0),
        ("high", None),        # words are not numbers
        ("0.9", None),         # strings are not accepted
        (-0.5, None),
        (150, None),
        (None, None),
        (True, None),          # bool is not a confidence
    ],
)
def test_confidence_is_only_kept_when_meaningful(confidence, expected):
    """A fabricated default is worse than an absent value."""
    result = parse_frame_analysis(
        json.dumps({"violations": [{"category": "x", "confidence": confidence}]})
    )
    got = result.detections[0].confidence
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_entry_without_a_category_is_discarded_and_counted():
    result = parse_frame_analysis(
        json.dumps({"violations": [{"description": "no subject"}, {"category": "ok"}]})
    )
    assert result.detection_count == 1
    assert result.discarded_entries == 1


def test_unknown_risk_level_becomes_none_not_a_guess():
    result = parse_frame_analysis(
        json.dumps({"violations": [{"category": "x", "risk_level": "catastrophic"}]})
    )
    assert result.detections[0].risk_level is None


def test_box_2d_alias_is_accepted():
    """audit_manager.py used `box_2d`; tolerate it on input."""
    result = parse_frame_analysis(
        json.dumps({"violations": [{"category": "x", "box_2d": [1, 2, 3, 4]}]})
    )
    assert result.detections[0].bounding_box == [1.0, 2.0, 3.0, 4.0]


def test_oversized_text_is_truncated_not_stored_whole():
    result = parse_frame_analysis(
        json.dumps({"scene_description": "x" * 99999, "violations": []})
    )
    assert len(result.scene_description) <= 4000


# ---------------------------------------------------------------------------
# Bedrock error handling
# ---------------------------------------------------------------------------

def test_successful_invocation_returns_a_validated_analysis(no_sleep):
    bedrock = MagicMock()
    bedrock.invoke_model.return_value = _bedrock_response(GOOD_RESPONSE)

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is True
    assert result.detection_count == 1
    assert bedrock.invoke_model.call_count == 1
    assert bedrock.invoke_model.call_args.kwargs["modelId"] == analyzer.MODEL_ID


def test_throttling_is_retried_then_succeeds(no_sleep):
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = [
        _client_error("ThrottlingException"),
        _client_error("ThrottlingException"),
        _bedrock_response(GOOD_RESPONSE),
    ]

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is True
    assert bedrock.invoke_model.call_count == 3


def test_retries_are_bounded(no_sleep):
    """A persistent throttle must not retry forever."""
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = _client_error("ThrottlingException")

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is False
    from config import get_settings

    assert bedrock.invoke_model.call_count == get_settings().bedrock_max_attempts


@pytest.mark.parametrize(
    "code",
    [
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "UnrecognizedClientException",
    ],
)
def test_permanent_errors_are_not_retried(code, no_sleep):
    """Retrying an authorization failure just burns the budget."""
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = _client_error(code)

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is False
    assert bedrock.invoke_model.call_count == 1, f"{code} should fail immediately"
    assert code in result.error


def test_transport_errors_are_retried(no_sleep):
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = [
        EndpointConnectionError(endpoint_url="https://bedrock.invalid"),
        _bedrock_response(GOOD_RESPONSE),
    ]

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is True
    assert bedrock.invoke_model.call_count == 2


def test_malformed_response_is_a_failure_not_a_clean_frame(no_sleep):
    """THE critical distinction for this layer."""
    bedrock = MagicMock()
    bedrock.invoke_model.return_value = _bedrock_response("I cannot help with that.")

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is False
    assert result.detection_count == 0
    assert "MALFORMED_RESPONSE" in result.error
    # Not retried: another call would not make the model answer differently.
    assert bedrock.invoke_model.call_count == 1


def test_unexpected_response_shape_is_a_failure(no_sleep):
    bedrock = MagicMock()
    body = MagicMock()
    body.read.return_value = json.dumps({"unexpected": "shape"}).encode()
    bedrock.invoke_model.return_value = {"body": body}

    with patch.object(analyzer, "bedrock_runtime", bedrock):
        result = analyzer.analyze_frame_with_nova(b"frame")

    assert result.ok is False


def test_analysis_failure_never_logs_frame_content(no_sleep, caplog):
    """A Bedrock error body can echo the request, which holds frame data."""
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = ClientError(
        {"Error": {"Code": "ValidationException",
                   "Message": "image bytes: SECRETFRAMEDATA"}},
        "InvokeModel",
    )

    with caplog.at_level("DEBUG"), patch.object(analyzer, "bedrock_runtime", bedrock):
        analyzer.analyze_frame_with_nova(b"SECRETFRAMEDATA")

    assert "SECRETFRAMEDATA" not in caplog.text


def test_backoff_grows_and_is_bounded():
    delays = [analyzer._backoff_seconds(n) for n in range(1, 7)]
    assert delays[0] < delays[1] < delays[2]
    assert max(delays) <= 8.5, delays


# ---------------------------------------------------------------------------
# the JSON extractor in isolation
# ---------------------------------------------------------------------------

def test_extract_json_object_prefers_an_analysis_shaped_object():
    text = '{"other":1} {"violations":[],"scene_description":"x"}'
    assert extract_json_object(text)["scene_description"] == "x"


def test_extract_json_object_raises_on_nothing_usable():
    with pytest.raises(AnalysisParseError):
        extract_json_object("plain prose with no braces")


def test_detection_model_rejects_an_empty_category():
    assert Detection.parse({"category": "   "}) is None
    assert Detection.parse({"category": None}) is None
    assert Detection.parse("not a dict") is None


def test_frame_analysis_failure_helper_is_not_ok():
    from detections import failed_analysis

    result = failed_analysis("ThrottlingException")
    assert result.ok is False
    assert result.detection_count == 0
    assert isinstance(result, FrameAnalysis)


# ---------------------------------------------------------------------------
# Phase 6: the per-video frame read is a Query, not a Scan
# ---------------------------------------------------------------------------
#
# `check_pending_frames(video_id=...)` was a Scan with a filter on both
# `status` and `video_id`, so finding one video's frames read the whole table
# and got slower and more expensive with every video ever uploaded. The table
# is keyed (video_id HASH, frame_number RANGE), so one partition is exactly
# that video's frames and Query answers it directly. No index was added and
# no IAM permission changed - Query was already granted.
#
# `fake_frames_table` models the parts of Query that affect correctness:
# filters apply after the key condition, Limit bounds items READ rather than
# returned, and a truncated read carries LastEvaluatedKey.

def _pending(video_id, number):
    return {
        "video_id": video_id,
        "frame_number": number,
        "status": analyzer.STATUS_PENDING,
        "s3_key": f"frames/{video_id}/frame_{number:04d}.jpg",
        "s3_bucket": "test-bucket",
    }


def test_per_video_read_uses_query_and_never_scans(fake_frames_table):
    """The point of the change: no Scan on the pipeline path."""
    for number in range(3):
        fake_frames_table.put_item(Item=_pending("vid_aaaa1111", number))

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert len(frames) == 3
    assert fake_frames_table.query_calls, "the per-video read must use Query"
    assert fake_frames_table.scan_calls == [], "it must not Scan"


def test_query_is_scoped_to_the_requested_video(fake_frames_table):
    """Another video's frames must never appear in this video's batch."""
    for number in range(3):
        fake_frames_table.put_item(Item=_pending("vid_aaaa1111", number))
    for number in range(2):
        fake_frames_table.put_item(Item=_pending("vid_bbbb2222", number))

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert {f["video_id"] for f in frames} == {"vid_aaaa1111"}
    assert len(frames) == 3


def test_query_returns_only_pending_frames(fake_frames_table):
    """The status filter still applies, now over one partition."""
    fake_frames_table.put_item(Item=_pending("vid_aaaa1111", 0))
    audited = _pending("vid_aaaa1111", 1) | {"status": analyzer.STATUS_AUDITED}
    failed = _pending("vid_aaaa1111", 2) | {"status": analyzer.STATUS_FAILED}
    fake_frames_table.put_item(Item=audited)
    fake_frames_table.put_item(Item=failed)

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert [f["frame_number"] for f in frames] == [0]


def test_query_pages_through_every_frame(fake_frames_table):
    """Pagination must not stop at the first page.

    One Query call returns at most 1 MB, so a video with many frames spans
    several pages. Reading only the first would silently analyse a prefix of
    the video and then report the job complete.
    """
    for number in range(25):
        fake_frames_table.put_item(Item=_pending("vid_aaaa1111", number))
    fake_frames_table.page_size = 4

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert [f["frame_number"] for f in frames] == list(range(25))
    assert len(fake_frames_table.query_calls) >= 6, "it must have paged"


def test_query_continues_past_a_page_with_no_matches(fake_frames_table):
    """A filtered page can be empty while more frames remain.

    Limit bounds items read, and the filter runs afterwards, so a page of
    already-audited frames comes back empty with a LastEvaluatedKey set.
    Treating empty as "done" would skip every pending frame behind it - the
    subtle bug this test exists to prevent.
    """
    # Frames 0-3 are already audited; 4-7 are still pending.
    for number in range(4):
        fake_frames_table.put_item(
            Item=_pending("vid_aaaa1111", number) | {"status": analyzer.STATUS_AUDITED}
        )
    for number in range(4, 8):
        fake_frames_table.put_item(Item=_pending("vid_aaaa1111", number))
    fake_frames_table.page_size = 4

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert [f["frame_number"] for f in frames] == [4, 5, 6, 7], (
        "an empty filtered page must not end pagination"
    )


def test_query_frames_arrive_in_sort_key_order(fake_frames_table):
    """Query returns items in sort-key order, so frames need no re-sorting."""
    for number in (7, 2, 9, 0, 4):
        fake_frames_table.put_item(Item=_pending("vid_aaaa1111", number))

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert [f["frame_number"] for f in frames] == [0, 2, 4, 7, 9]


def test_query_pagination_is_bounded(fake_frames_table, monkeypatch):
    """A runaway table cannot make one read spin forever."""
    for number in range(40):
        fake_frames_table.put_item(Item=_pending("vid_aaaa1111", number))
    fake_frames_table.page_size = 1
    monkeypatch.setattr(analyzer, "MAX_QUERY_PAGES", 3)

    frames = analyzer._query_pending_frames("vid_aaaa1111")

    assert len(fake_frames_table.query_calls) == 3
    assert len(frames) == 3


def test_an_empty_partition_returns_nothing_without_error(fake_frames_table):
    assert analyzer._query_pending_frames("vid_nothing") == []
    assert fake_frames_table.scan_calls == []


def test_the_maintenance_path_still_scans_every_video(fake_frames_table):
    """With no video id there is no key to query on, so Scan is correct.

    `status` is not a key and there is no GSI on it. This path is only the
    maintenance CLI; adding an index for it would be infrastructure and cost
    for a convenience no request uses.
    """
    fake_frames_table.put_item(Item=_pending("vid_aaaa1111", 0))
    fake_frames_table.put_item(Item=_pending("vid_bbbb2222", 0))

    frames = analyzer._scan_pending_frames()

    assert len(frames) == 2
    assert fake_frames_table.scan_calls, "the all-videos path legitimately scans"


def test_check_pending_frames_routes_by_whether_a_video_is_given(fake_frames_table):
    """The public entry point picks the right access pattern."""
    fake_frames_table.put_item(Item=_pending("vid_aaaa1111", 0))

    with patch.object(analyzer, "_query_pending_frames", return_value=[]) as query, \
         patch.object(analyzer, "_scan_pending_frames", return_value=[]) as scan:
        analyzer.check_pending_frames(video_id="vid_aaaa1111")
        assert query.called and not scan.called

    with patch.object(analyzer, "_query_pending_frames", return_value=[]) as query, \
         patch.object(analyzer, "_scan_pending_frames", return_value=[]) as scan:
        analyzer.check_pending_frames()
        assert scan.called and not query.called
