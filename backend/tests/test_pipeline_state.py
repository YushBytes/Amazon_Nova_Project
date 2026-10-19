"""Phase 4: the job state machine and its persistence.

Before Phase 4 a video's only state was a free-text string set to "ANALYZING"
when analysis was *scheduled* and never updated again. A job that failed in the
background looked identical to one still running, and one that finished looked
identical to one that had just started.

These tests pin the properties that make a reported state trustworthy:

* COMPLETED is unreachable except from ANALYZING, so scheduling work can never
  produce it;
* every non-terminal state can reach FAILED, so nothing is stranded;
* transitions are conditional, so two workers cannot both advance one job;
* records written before Phase 4 are still readable.
"""

import pytest

import job_state
import video_repo
from job_state import InvalidTransition, JobState, coerce


# ---------------------------------------------------------------------------
# the state machine itself (no I/O)
# ---------------------------------------------------------------------------

def test_completed_is_only_reachable_from_analyzing():
    """Scheduling a job must never be able to mark it complete."""
    for state in JobState:
        allowed = job_state.can_transition(state, JobState.COMPLETED)
        assert allowed is (state is JobState.ANALYZING), (
            f"{state.value} -> completed should be "
            f"{state is JobState.ANALYZING}"
        )


def test_every_non_terminal_state_can_fail():
    """No job may be strandable mid-pipeline."""
    for state in (
        JobState.UPLOADED,
        JobState.QUEUED,
        JobState.EXTRACTING,
        JobState.ANALYZING,
    ):
        assert job_state.can_transition(state, JobState.FAILED), state


def test_terminal_states_only_re_enter_through_queued():
    """A finished job may be retried, but only by being re-queued.

    It must not jump straight back into a working state, or into the other
    terminal state.
    """
    for state in (JobState.COMPLETED, JobState.FAILED):
        assert job_state.can_transition(state, JobState.QUEUED), state
        for target in (
            JobState.UPLOADED,
            JobState.EXTRACTING,
            JobState.ANALYZING,
            JobState.COMPLETED,
            JobState.FAILED,
        ):
            assert not job_state.can_transition(state, target), (
                f"{state.value} -> {target.value} should be refused"
            )


def test_pipeline_cannot_skip_extraction():
    """A job must not jump straight to analysis."""
    assert not job_state.can_transition(JobState.QUEUED, JobState.ANALYZING)
    assert not job_state.can_transition(JobState.UPLOADED, JobState.ANALYZING)


def test_terminal_and_active_classification():
    assert JobState.COMPLETED.is_terminal and JobState.FAILED.is_terminal
    assert not JobState.UPLOADED.is_terminal
    for state in (JobState.QUEUED, JobState.EXTRACTING, JobState.ANALYZING):
        assert state.is_active, state
    for state in (JobState.UPLOADED, JobState.COMPLETED, JobState.FAILED):
        assert not state.is_active, state


def test_assert_transition_rejects_an_illegal_move():
    with pytest.raises(InvalidTransition) as excinfo:
        job_state.assert_transition(JobState.UPLOADED, JobState.COMPLETED)
    assert "uploaded" in str(excinfo.value)
    assert "completed" in str(excinfo.value)


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("uploaded", JobState.UPLOADED),
        ("UPLOADED", JobState.UPLOADED),       # pre-Phase-4 casing
        ("ANALYZING", JobState.ANALYZING),     # pre-Phase-4 casing
        ("  queued  ", JobState.QUEUED),
        (None, JobState.UPLOADED),             # record predates the field
        ("", JobState.UPLOADED),
        ("nonsense", JobState.UPLOADED),       # unknown -> safe default
        (123, JobState.UPLOADED),
        (JobState.FAILED, JobState.FAILED),
    ],
)
def test_coerce_handles_legacy_and_junk_values(stored, expected):
    """An old or corrupt status must not make a record unreadable."""
    assert coerce(stored) is expected


def test_every_state_has_a_stage_label():
    for state in JobState:
        label = job_state.stage_label(state)
        assert label and isinstance(label, str)


def test_stage_labels_are_not_percentages():
    """Progress is reported as named stages; a percentage would be invented.

    The pipeline cannot know the frame count until extraction finishes, so any
    percentage before that point would be fabricated.
    """
    for label in job_state.STAGE_LABELS.values():
        assert "%" not in label


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

async def _seed(fake_db, video_id="vid_abcd1234", owner="alice"):
    return await video_repo.record_video(
        video_id=video_id,
        owner=owner,
        stored_filename=f"{video_id}.mp4",
        original_filename="clip.mp4",
        extension="mp4",
        size_bytes=2048,
    )


async def test_new_upload_starts_as_uploaded(fake_db):
    record = await _seed(fake_db)
    assert record["status"] == JobState.UPLOADED.value
    assert record["attempt"] == 0
    assert record["frames_extracted"] is None
    assert record["error_summary"] is None
    assert record["completed_at"] is None
    assert record["pipeline_version"] == video_repo.PIPELINE_VERSION


async def test_full_happy_path_transitions_persist(fake_db):
    await _seed(fake_db)

    queued = await video_repo.transition("vid_abcd1234", JobState.QUEUED)
    assert queued["status"] == JobState.QUEUED.value
    assert queued["attempt"] == 1

    extracting = await video_repo.transition(
        "vid_abcd1234", JobState.EXTRACTING, expected=JobState.QUEUED
    )
    assert extracting["started_at"] is not None

    analyzing = await video_repo.transition(
        "vid_abcd1234",
        JobState.ANALYZING,
        expected=JobState.EXTRACTING,
        fields={"frames_extracted": 7},
    )
    assert analyzing["frames_extracted"] == 7

    completed = await video_repo.transition(
        "vid_abcd1234",
        JobState.COMPLETED,
        expected=JobState.ANALYZING,
        fields={"frames_audited": 7, "detections_recorded": 2},
    )
    assert completed["status"] == JobState.COMPLETED.value
    assert completed["completed_at"] is not None
    assert completed["frames_audited"] == 7


async def test_illegal_transition_is_refused_and_persists_nothing(fake_db):
    await _seed(fake_db)

    with pytest.raises(InvalidTransition):
        await video_repo.transition("vid_abcd1234", JobState.COMPLETED)

    record = await video_repo.get_video("vid_abcd1234")
    assert record["status"] == JobState.UPLOADED.value


async def test_transition_with_a_wrong_expected_state_is_refused(fake_db):
    """The conditional guard is what stops two workers racing."""
    await _seed(fake_db)
    await video_repo.transition("vid_abcd1234", JobState.QUEUED)

    with pytest.raises(InvalidTransition):
        await video_repo.transition(
            "vid_abcd1234", JobState.EXTRACTING, expected=JobState.UPLOADED
        )

    record = await video_repo.get_video("vid_abcd1234")
    assert record["status"] == JobState.QUEUED.value


async def test_requeue_increments_attempt_and_clears_the_old_error(fake_db):
    """A stale failure must never be readable as the current outcome."""
    await _seed(fake_db)
    await video_repo.transition("vid_abcd1234", JobState.QUEUED)
    await video_repo.transition(
        "vid_abcd1234",
        JobState.FAILED,
        fields={"error_summary": "The video could not be decoded."},
    )

    failed = await video_repo.get_video("vid_abcd1234")
    assert failed["error_summary"] == "The video could not be decoded."
    assert failed["attempt"] == 1

    requeued = await video_repo.transition("vid_abcd1234", JobState.QUEUED)
    assert requeued["attempt"] == 2
    assert requeued["error_summary"] is None
    assert requeued["completed_at"] is None


async def test_record_progress_never_changes_state(fake_db):
    """Progress updates must not be able to complete a job."""
    await _seed(fake_db)
    await video_repo.transition("vid_abcd1234", JobState.QUEUED)

    await video_repo.record_progress(
        "vid_abcd1234", frames_extracted=3, status=JobState.COMPLETED.value
    )

    record = await video_repo.get_video("vid_abcd1234")
    assert record["status"] == JobState.QUEUED.value, "progress changed the state"
    assert record["frames_extracted"] == 3


async def test_transition_on_unknown_video_raises(fake_db):
    with pytest.raises(video_repo.OwnershipUnknown):
        await video_repo.transition("vid_00000000", JobState.QUEUED)


# ---------------------------------------------------------------------------
# the client-facing projection
# ---------------------------------------------------------------------------

async def test_job_view_omits_server_side_storage_details(fake_db):
    record = await _seed(fake_db)
    view = video_repo.job_view(record)

    for leaked in ("owner", "stored_filename", "extension", "_id"):
        assert leaked not in view, f"job_view leaked {leaked}"


async def test_job_view_reports_state_and_terminality(fake_db):
    await _seed(fake_db)
    await video_repo.transition("vid_abcd1234", JobState.QUEUED)
    record = await video_repo.transition(
        "vid_abcd1234", JobState.EXTRACTING, expected=JobState.QUEUED
    )

    view = video_repo.job_view(record)
    assert view["state"] == "extracting"
    assert view["stage"] == job_state.stage_label(JobState.EXTRACTING)
    assert view["is_terminal"] is False
    assert view["is_active"] is True
    assert view["max_attempts"] == job_state.MAX_ATTEMPTS


async def test_job_view_of_a_legacy_record_is_safe(fake_db):
    """A record written before Phase 4 has none of the new fields."""
    fake_db["videos"].insert_raw(
        {
            "video_id": "vid_legacy01",
            "owner": "alice",
            "stored_filename": "vid_legacy01.mp4",
            "status": "UPLOADED",
        }
    )
    record = await video_repo.get_video("vid_legacy01")
    view = video_repo.job_view(record)

    assert view["state"] == "uploaded"
    assert view["attempt"] == 0
    assert view["frames_extracted"] is None
    assert view["created_at"] is None


async def test_timestamps_are_utc_iso_strings(fake_db):
    record = await _seed(fake_db)
    view = video_repo.job_view(record)

    assert view["created_at"].endswith("+00:00"), view["created_at"]
    assert view["updated_at"].endswith("+00:00")
