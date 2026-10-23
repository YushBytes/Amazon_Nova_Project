"""Phase 10.1: the operator recovery CLI, and its dry-run safety boundary.

`recover_jobs.py` is what a scheduler invokes to make job recovery recurring.
At the Phase 10.1 baseline it was the least-covered implemented module in the
backend (22% statements), and the untested part was the part that writes.

The properties asserted here, in descending order of what getting them wrong
would cost:

1. **Dry run is the default.** Running with no arguments must not requeue
   anything. A regression that inverted this would silently mutate an
   operator's production database the moment they ran the script to *look* at
   it. Asserted by spying on the write path, not by reading output text.
2. **A sweep never dispatches.** `_apply` passes `dispatch=None` on purpose:
   the module docstring's reason is that running analysis from a short-lived
   cron container would spend Bedrock capacity from a process nobody is
   watching. Every frame of a dispatched job is billed, so this is a cost
   guard, not a style choice.
3. **A missing `MONGODB_URL` refuses rather than guessing.** `db.py` defaults
   to `mongodb://localhost:27017`, so without this check the script would
   cheerfully sweep whatever happens to be listening locally - which is not
   the deployment's database.
4. Failures exit non-zero instead of raising a traceback at an operator, and
   `--limit` reaches the sweep rather than being silently dropped.

Nothing here touches MongoDB: `pipeline` and `video_repo` are patched at the
module boundary, which is the same convention the other CLI-level tests use.
"""

import importlib
from unittest.mock import patch

import pytest

import recover_jobs


@pytest.fixture(autouse=True)
def _mongodb_url_set(monkeypatch):
    """The CLI refuses to run without this; most tests want it present."""
    monkeypatch.setenv("MONGODB_URL", "mongodb://test-host:27017")


@pytest.fixture
def sweep():
    """Patch the two modules the CLI reaches, recording every call.

    `recover_jobs` imports them inside its functions, so patching the module
    objects in `sys.modules` is what the CLI will actually see.
    """
    import pipeline
    import video_repo

    calls = {"recover": [], "expired": 0, "stuck": 0}

    async def fake_recover(dispatch=None, *, limit=None):
        calls["recover"].append({"dispatch": dispatch, "limit": limit})
        return {"scanned": 3, "requeued": 2, "failed": 1, "skipped": 0}

    async def fake_expired():
        calls["expired"] += 1
        return [
            {"video_id": "vid_aaaa1111", "status": "analyzing", "attempt": 1},
            {"video_id": "vid_bbbb2222", "status": "extracting", "attempt": 3},
        ]

    async def fake_stuck(*a, **k):
        calls["stuck"] += 1
        return [{"video_id": "vid_cccc3333"}]

    with patch.object(pipeline, "recover_abandoned_jobs", fake_recover), \
         patch.object(pipeline, "find_stuck_jobs", fake_stuck), \
         patch.object(video_repo, "find_expired_leases", fake_expired):
        yield calls


# ===========================================================================
# 1. The dry-run boundary
# ===========================================================================

def test_the_default_invocation_writes_nothing(sweep, capsys):
    """THE safety property of this script. No argv means report only."""
    assert recover_jobs.main([]) == 0

    assert sweep["recover"] == [], (
        "a bare invocation reached the write path; dry run is supposed to be "
        "the default and --apply the only way to write"
    )
    # It did do the read-only work.
    assert sweep["expired"] == 1
    assert sweep["stuck"] == 1


def test_apply_is_the_only_path_that_writes(sweep):
    assert recover_jobs.main(["--apply"]) == 0
    assert len(sweep["recover"]) == 1


def test_a_dry_run_reports_the_actions_it_would_take(sweep, capsys):
    recover_jobs.main([])
    out = capsys.readouterr().out

    assert "Abandoned (lease lapsed): 2" in out
    assert "Stuck (no progress 30m):  1" in out
    # attempt 1 of 3 can still be retried; attempt 3 cannot.
    assert "requeue" in out
    assert "fail (no attempts left)" in out
    assert "Re-run with --apply" in out


def test_a_dry_run_prints_no_owner(sweep, capsys):
    """Video ids are opaque; owners are user data and deliberately omitted."""
    import video_repo

    async def with_owners():
        return [
            {
                "video_id": "vid_aaaa1111",
                "status": "analyzing",
                "attempt": 0,
                "owner": "alice@example.com",
            }
        ]

    with patch.object(video_repo, "find_expired_leases", with_owners):
        recover_jobs.main([])

    out = capsys.readouterr().out
    assert "vid_aaaa1111" in out
    assert "alice" not in out
    assert "example.com" not in out


# ===========================================================================
# 2. A sweep must never dispatch
# ===========================================================================

def test_apply_never_dispatches_the_jobs_it_requeues(sweep):
    """Dispatching from a cron container would bill Bedrock unwatched."""
    recover_jobs.main(["--apply"])

    assert sweep["recover"][0]["dispatch"] is None, (
        "the recovery sweep passed a dispatcher; it must requeue only, or a "
        "short-lived container will run paid analysis nobody is watching"
    )


def test_apply_reports_the_real_counts(sweep, capsys):
    recover_jobs.main(["--apply"])
    out = capsys.readouterr().out

    assert "scanned=3" in out
    assert "requeued=2" in out
    assert "failed=1" in out
    assert "skipped=0" in out


def test_nothing_requeued_omits_the_follow_up_note(sweep, capsys):
    import pipeline

    async def nothing(dispatch=None, *, limit=None):
        return {"scanned": 0, "requeued": 0, "failed": 0, "skipped": 0}

    with patch.object(pipeline, "recover_abandoned_jobs", nothing):
        recover_jobs.main(["--apply"])

    out = capsys.readouterr().out
    assert "requeued=0" in out
    assert "next startup" not in out


# ===========================================================================
# 3. Configuration
# ===========================================================================

@pytest.mark.parametrize("value", ["", "   "])
def test_a_missing_mongodb_url_refuses_and_touches_nothing(
    sweep, monkeypatch, capsys, value
):
    """`db.py` defaults to localhost, so an unset URL must not be a sweep."""
    monkeypatch.setenv("MONGODB_URL", value)

    assert recover_jobs.main(["--apply"]) == 2

    assert sweep["recover"] == []
    assert sweep["expired"] == 0
    assert "MONGODB_URL is not set" in capsys.readouterr().err


def test_the_refusal_names_no_connection_string(sweep, monkeypatch, capsys):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    recover_jobs.main([])
    captured = capsys.readouterr()
    assert "mongodb://" not in captured.err
    assert "mongodb://" not in captured.out


def test_the_limit_flag_reaches_the_sweep(sweep):
    recover_jobs.main(["--apply", "--limit", "7"])
    assert sweep["recover"][0]["limit"] == 7


def test_the_default_limit_is_the_pipeline_cap(sweep):
    import pipeline

    recover_jobs.main(["--apply"])
    assert sweep["recover"][0]["limit"] == pipeline.MAX_RECOVERIES_PER_SWEEP


def test_an_explicit_zero_limit_falls_back_to_the_cap(sweep):
    """`args.limit or CAP` treats 0 as unset - pinned so it is a decision."""
    import pipeline

    recover_jobs.main(["--apply", "--limit", "0"])
    assert sweep["recover"][0]["limit"] == pipeline.MAX_RECOVERIES_PER_SWEEP


# ===========================================================================
# 4. Failure handling
# ===========================================================================

def test_a_failing_sweep_exits_non_zero_without_raising(sweep):
    """An operator gets an exit code, not a traceback."""
    import pipeline

    async def boom(dispatch=None, *, limit=None):
        raise RuntimeError("database unreachable")

    with patch.object(pipeline, "recover_abandoned_jobs", boom):
        assert recover_jobs.main(["--apply"]) == 1


def test_a_failing_report_exits_non_zero(sweep):
    import video_repo

    async def boom():
        raise RuntimeError("database unreachable")

    with patch.object(video_repo, "find_expired_leases", boom):
        assert recover_jobs.main([]) == 1


def test_the_failure_path_does_not_leak_the_exception_to_stdout(sweep, capsys):
    import pipeline

    async def boom(dispatch=None, *, limit=None):
        raise RuntimeError("mongodb://user:pw@host/db is unreachable")

    with patch.object(pipeline, "recover_abandoned_jobs", boom):
        recover_jobs.main(["--apply"])

    captured = capsys.readouterr()
    assert "user:pw" not in captured.out
    assert "user:pw" not in captured.err


def test_an_unknown_flag_exits_two_without_sweeping(sweep):
    """argparse's own behaviour, pinned so a typo cannot become a sweep."""
    with pytest.raises(SystemExit) as exc:
        recover_jobs.main(["--aply"])
    assert exc.value.code == 2
    assert sweep["recover"] == []


def test_the_module_is_importable_without_running(sweep):
    """Importing must not sweep: the work is behind `if __name__`."""
    module = importlib.import_module("recover_jobs")
    assert hasattr(module, "main")
    assert sweep["recover"] == []
    assert sweep["expired"] == 0
