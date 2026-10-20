"""The durable analysis worker: claim a queued job, run it, repeat.

    python backend/worker.py              # run until stopped
    python backend/worker.py --once       # drain what is claimable, then exit
    python backend/worker.py --status     # print queue depth and exit

WHY THIS EXISTS

Through Phase 4.1 the API ran analysis itself, in a FastAPI `BackgroundTasks`
thread. That had three consequences the lease mechanism could mitigate but not
fix:

* a deploy or crash killed every in-flight job, and nothing resumed them until
  the next process start;
* long analyses occupied the web worker's threadpool, so upload and status
  requests queued behind frame extraction;
* the API could not be scaled separately from the thing doing the work.

THE QUEUE IS THE JOB RECORD

There is deliberately no SQS, no Celery, no Redis. The `videos` collection in
MongoDB is already a durable work queue, and it was one before this phase:

* `pipeline.submit()` persists the job as `queued` BEFORE the API responds, so
  acceptance is durable by construction and an accepted job survives an API
  restart. There is no in-memory handoff to lose.
* `video_repo.claim()` is a conditional update, so exactly one of N racing
  workers wins a job - that is the same primitive a queue's receive gives you.
* `lease_expires_at` plus `claim_id` is a visibility timeout plus a fencing
  token, which is strictly more than SQS offers: a reclaimed worker's writes
  are rejected rather than merely duplicated.
* `attempt` against MAX_ATTEMPTS is the redrive policy, and FAILED is the
  dead-letter state - queryable in the same store as everything else.

Introducing a real queue would add a second source of truth for "what work is
pending" that can disagree with the job state machine (a message whose job is
already completed, a completed job whose message was lost), require a
deployment target decision this repository has not made (CLAUDE.md section 9
question 9), and could not be tested without either real AWS or a new local
dependency. It is the right change once there are multiple worker hosts and
throughput matters; it is not the smallest reliable thing today. See
docs/PIPELINE_ARCHITECTURE.md for the comparison in full.

WHAT THIS GIVES AND WHAT IT DOES NOT

Given: an accepted job survives an API restart; a killed worker's job is
requeued by the next sweep rather than stranded; the API no longer executes
analysis; recovery runs continuously while a worker is up.

Not given: at-most-once execution (a job interrupted mid-analysis is re-run
from the start, which is safe because frame writes are keyed and idempotent -
see `_prune_stale_frames`), push delivery (this polls), or cross-host
fairness beyond "oldest queued first".

POLLING COST

One poll is a single indexed query against `(status, updated_at)`. At the
default 5s interval that is 12 queries a minute per worker while idle, which
is why the interval is configurable but floored at 1s.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow `python backend/worker.py` from the repository root.
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

logger = logging.getLogger("worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerState:
    """What this worker is doing, for the health endpoint and for operators.

    Deliberately counts and timestamps only - never an owner, filename, or
    error body - so it is safe to expose and safe to log. `video_id` is a
    server-generated opaque id and is included because tracing one job through
    the logs is the main thing an operator needs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = _now()
        self.last_poll_at: datetime | None = None
        self.last_job_finished_at: datetime | None = None
        self.current_video_id: str | None = None
        self.jobs_started = 0
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.jobs_skipped = 0
        self.polls = 0
        self.consecutive_poll_errors = 0
        self.stopping = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self.started_at.isoformat(),
                "last_poll_at": (
                    self.last_poll_at.isoformat() if self.last_poll_at else None
                ),
                "last_job_finished_at": (
                    self.last_job_finished_at.isoformat()
                    if self.last_job_finished_at
                    else None
                ),
                "current_video_id": self.current_video_id,
                "jobs_started": self.jobs_started,
                "jobs_completed": self.jobs_completed,
                "jobs_failed": self.jobs_failed,
                "jobs_skipped": self.jobs_skipped,
                "polls": self.polls,
                "consecutive_poll_errors": self.consecutive_poll_errors,
                "stopping": self.stopping,
            }

    # Each mutator takes the lock: the signal handler reads `stopping` from
    # another thread while the loop writes counters.

    def record_poll(self, failed: bool = False) -> None:
        with self._lock:
            self.polls += 1
            self.last_poll_at = _now()
            if failed:
                self.consecutive_poll_errors += 1
            else:
                self.consecutive_poll_errors = 0

    def record_job_start(self, video_id: str) -> None:
        with self._lock:
            self.jobs_started += 1
            self.current_video_id = video_id

    def record_job_end(self, outcome: str) -> None:
        with self._lock:
            self.current_video_id = None
            self.last_job_finished_at = _now()
            if outcome == "completed":
                self.jobs_completed += 1
            elif outcome == "skipped":
                self.jobs_skipped += 1
            else:
                self.jobs_failed += 1

    def request_stop(self) -> None:
        with self._lock:
            self.stopping = True


class Worker:
    """Poll for queued jobs, run them, and sweep for abandoned ones.

    Single-job-at-a-time by design. Frame extraction is CPU and disk bound and
    analysis is billed per frame, so concurrency belongs at the process level -
    run more workers - where the operator can see and cap the cost. A
    `WORKER_MAX_CONCURRENT_JOBS` knob would have hidden it inside one process.
    """

    def __init__(self, *, poll_seconds=None, recovery_seconds=None, batch=5):
        import config

        settings = config.get_settings()
        self.poll_seconds = (
            settings.worker_poll_seconds if poll_seconds is None else poll_seconds
        )
        self.recovery_seconds = (
            settings.worker_recovery_seconds
            if recovery_seconds is None
            else recovery_seconds
        )
        self.shutdown_grace = settings.worker_shutdown_grace_seconds
        self.batch = batch
        self.state = WorkerState()
        # Event rather than a bool: a stop during the idle pause must interrupt
        # the wait immediately, not after the full poll interval.
        self._stop = threading.Event()
        self._last_recovery_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Ask the loop to finish the current job and exit. Signal-safe."""
        self.state.request_stop()
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> None:
        """Turn SIGINT/SIGTERM into a graceful stop.

        A container stop sends SIGTERM. Without this the process dies mid-job,
        the lease is left to lapse, and the job waits a full lease period
        before recovery - correct, but slower than necessary. Handled only on
        the main thread; a worker embedded elsewhere installs nothing.
        """
        def handle(signum, _frame):
            logger.info("Signal %s received; finishing the current job", signum)
            self.stop()

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                # Not the main thread, or the platform disallows it.
                logger.debug("Could not install a handler for signal %s", sig)

    # -- the loop ----------------------------------------------------------

    def run_forever(self) -> dict:
        """Claim and run jobs until stopped. Returns the final counters."""
        logger.info(
            "Worker up: poll=%ss recovery=%ss",
            self.poll_seconds,
            self.recovery_seconds or "disabled",
        )
        while not self.stopping:
            try:
                did_work = self.run_once()
            except Exception:
                # A loop that dies on an unexpected error is worse than one
                # that logs and backs off: the queue would silently stop
                # draining with the process still "up".
                logger.exception("Worker iteration failed; backing off")
                self.state.record_poll(failed=True)
                did_work = False

            if not did_work and not self.stopping:
                self._stop.wait(self.poll_seconds)

        logger.info("Worker stopped: %s", self.state.snapshot())
        return self.state.snapshot()

    def run_once(self) -> bool:
        """One iteration: maybe sweep, then claim and run at most one job.

        Returns:
            True if a job was executed, so the caller polls again immediately
            rather than sleeping - a backlog drains at processing speed, not
            at one job per poll interval.
        """
        self._maybe_recover()

        import anyio
        import video_repo

        try:
            candidates = anyio.run(video_repo.find_queued_jobs, self.batch)
        except Exception:
            logger.exception("Could not read the job queue")
            self.state.record_poll(failed=True)
            return False

        self.state.record_poll()
        if not candidates:
            return False

        for record in candidates:
            if self.stopping:
                return False
            video_id = record.get("video_id")
            if not video_id:
                continue
            if self._run_job(video_id):
                return True

        # Every candidate was taken by another worker between the read and the
        # claim. Not an error - that is the race resolving correctly.
        return False

    def _run_job(self, video_id: str) -> bool:
        """Run one job. Returns False if it could not be claimed."""
        import pipeline

        self.state.record_job_start(video_id)
        started = time.monotonic()
        try:
            result = pipeline.run_job(video_id)
        except Exception:
            # run_job is documented never to raise; treat a breach of that as
            # a failed job rather than letting it kill the loop.
            logger.exception("Job %s raised out of run_job", video_id)
            self.state.record_job_end("failed")
            return True

        if result.get("skipped"):
            reason = result.get("reason")
            self.state.record_job_end("skipped")
            if reason == "not_claimable":
                logger.debug("Job %s was claimed by another worker", video_id)
                return False
            logger.info("Job %s skipped: %s", video_id, reason)
            return True

        state = result.get("state")
        elapsed = time.monotonic() - started
        self.state.record_job_end(
            "completed" if state == "completed" else "failed"
        )
        logger.info(
            "Job %s finished in %.1fs with state=%s", video_id, elapsed, state
        )
        return True

    def _maybe_recover(self) -> None:
        """Run the abandoned-job sweep if its interval has elapsed.

        This is what makes recovery continuous rather than startup-only. It is
        time-based rather than every-poll because the sweep reads every
        non-terminal job, which is more work than a queue poll.
        """
        if not self.recovery_seconds:
            return
        now = time.monotonic()
        if self._last_recovery_at and now - self._last_recovery_at < self.recovery_seconds:
            return
        self._last_recovery_at = now

        import anyio
        import pipeline

        try:
            counts = anyio.run(pipeline.recover_abandoned_jobs)
        except Exception:
            # Never fatal: the jobs stay visibly non-terminal and the next
            # sweep tries again.
            logger.exception("Recovery sweep failed")
            return
        if counts.get("requeued") or counts.get("failed"):
            logger.info("Recovery sweep requeued jobs: %s", counts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_status() -> int:
    import anyio
    import video_repo

    counts = anyio.run(video_repo.count_by_state)
    stuck = anyio.run(video_repo.find_expired_leases)
    print("Job counts by state:")
    for state, count in counts.items():
        print(f"  {state:<12} {count}")
    print(f"Abandoned (lease lapsed): {len(stuck)}")
    if stuck:
        print("Run `python backend/recover_jobs.py --apply` to requeue them,")
        print("or start a worker - it sweeps on its own interval.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Claim and run queued NovaFlow analysis jobs."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain currently claimable jobs, then exit (for a cron-style run)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print queue depth and abandoned-job count, then exit",
    )
    parser.add_argument("--poll-seconds", type=int, default=None)
    parser.add_argument("--recovery-seconds", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not (os.getenv("MONGODB_URL") or "").strip():
        print(
            "MONGODB_URL is not set. The job queue lives in MongoDB; point it "
            "at the database this deployment uses before starting a worker.",
            file=sys.stderr,
        )
        return 2

    try:
        import config

        config.get_settings()
    except Exception as exc:
        # Configuration errors are the common startup failure and their
        # messages are written to be safe to print.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.status:
        return _print_status()

    worker = Worker(
        poll_seconds=args.poll_seconds,
        recovery_seconds=args.recovery_seconds,
    )
    worker.install_signal_handlers()

    if args.once:
        drained = 0
        # Bounded so `--once` cannot run forever against a filling queue.
        while drained < 1000 and worker.run_once():
            drained += 1
        print(f"Processed {drained} job(s). {worker.state.snapshot()}")
        return 0

    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
