"""Operator entry point for the abandoned-job recovery sweep.

    python backend/recover_jobs.py            # report only, change nothing
    python backend/recover_jobs.py --apply    # requeue abandoned jobs

Why this exists separately from the startup sweep: the application recovers
jobs when it boots, which handles the common case of a restart, but a job
abandoned while the process stays up is not noticed until the next boot. This
script is what a scheduler invokes to make recovery recurring. The required
deployment configuration is in docs/PIPELINE_OPERATIONS.md - nothing here
schedules itself, and the application does not claim continuous recovery.

It requeues only. It does not run the jobs: the application instances are the
workers, and they pick up queued jobs on their next startup sweep or when a
user retries. Running analysis from a short-lived cron container would spend
Bedrock capacity from a process nobody is watching.

Dry run is the default on purpose. `--apply` is the only way to write.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Allow `python backend/recover_jobs.py` from the repository root.
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

logger = logging.getLogger("recover_jobs")


async def _report() -> int:
    """Print what a sweep would do, without changing anything."""
    import pipeline
    import video_repo
    from job_state import MAX_ATTEMPTS, coerce

    abandoned = await video_repo.find_expired_leases()
    stuck = await pipeline.find_stuck_jobs()

    print(f"Lease period:            {video_repo.lease_seconds()}s")
    print(f"Abandoned (lease lapsed): {len(abandoned)}")
    print(f"Stuck (no progress 30m):  {len(stuck)}")

    if abandoned:
        print("")
        print("  job id          state       attempt  action")
        print("  --------------- ----------- -------  ------------------")
        for record in abandoned:
            attempt = int(record.get("attempt") or 0)
            action = "fail (no attempts left)" if attempt >= MAX_ATTEMPTS else "requeue"
            # Video ids are server-generated opaque identifiers, not user
            # data, so printing them in an operator console is fine. Owners
            # are deliberately not printed.
            print(
                f"  {str(record.get('video_id')):<15} "
                f"{coerce(record.get('status')).value:<11} "
                f"{attempt:<7}  {action}"
            )
        print("")
        print("Re-run with --apply to perform these actions.")

    return 0


async def _apply(limit: int) -> int:
    """Requeue abandoned jobs. Never dispatches them."""
    import pipeline

    counts = await pipeline.recover_abandoned_jobs(dispatch=None, limit=limit)
    print(
        f"scanned={counts['scanned']} requeued={counts['requeued']} "
        f"failed={counts['failed']} skipped={counts['skipped']}"
    )
    if counts["requeued"]:
        print(
            "Requeued jobs run on an application instance's next startup "
            "sweep, or when their owner retries."
        )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Find and requeue analysis jobs abandoned by a dead worker."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the recovery; without this the script only reports",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum jobs to reclaim in one sweep",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    if not (os.getenv("MONGODB_URL") or "").strip():
        print(
            "MONGODB_URL is not set. Point it at the database this deployment "
            "uses before running a recovery sweep.",
            file=sys.stderr,
        )
        return 2

    import pipeline

    limit = args.limit or pipeline.MAX_RECOVERIES_PER_SWEEP

    try:
        if args.apply:
            return asyncio.run(_apply(limit))
        return asyncio.run(_report())
    except Exception:
        logger.exception("Recovery sweep failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
