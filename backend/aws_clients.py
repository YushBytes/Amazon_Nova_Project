"""boto3 clients with finite timeouts and a bounded retry budget.

Phase 6. Before this module, exactly one client in the codebase
(`source_store`) was built with a timeout. The other nine - Bedrock, DynamoDB
and S3 in `analyzer`, `extractor`, `app`, `projects` and `video_stitcher` -
used botocore's defaults, which for a socket that stops responding means the
call can block far longer than any job should take.

That mattered more once Phase 4.1 introduced worker leases. A lease is only
meaningful if a stuck worker eventually stops being able to hold it: a call
that hangs indefinitely keeps the thread alive, keeps heartbeating, and the
job is never recovered because nothing ever looks abandoned. Finite timeouts
are what make the "abandoned" signal truthful.

RETRY BUDGETS MULTIPLY - READ THIS BEFORE RAISING EITHER

There are two layers, and the total is their product:

    AWS_MAX_ATTEMPTS        botocore's attempts for one API call (default 3)
    BEDROCK_MAX_ATTEMPTS    the analyzer's attempts for one frame  (default 3)

So one frame can cost up to nine model invocations, each billed. The
application layer exists because it can tell a throttle from a malformed
request; botocore's `standard` mode retries throttling and transient
transport errors and does NOT retry validation or permission errors, which is
the behaviour we want underneath. Raising both multiplies cost - raise one.

WHAT THIS MODULE DOES NOT DO

It does not cache a resource per region or per credential set beyond the
simple process-wide memo below, and it does not refresh credentials itself -
botocore owns that. `reset()` exists for tests that swap configuration.
"""

from __future__ import annotations

import logging
import threading

import boto3
from botocore.config import Config

import aws_resources
import config

logger = logging.getLogger(__name__)

# Guards the memo below. Clients are built lazily, and a worker can touch two
# services from two threads on its first job.
_lock = threading.Lock()
_clients: dict = {}
_resources: dict = {}


def client_config() -> Config:
    """The botocore config every client in this application shares.

    `standard` retry mode rather than `legacy`: it retries throttling and
    transient transport failures, respects a retry quota so a wide outage
    cannot amplify into a retry storm, and leaves deterministic client errors
    (validation, access denied, not found) un-retried. Retrying those would
    only turn one clear failure into three slow ones.

    Reads the three loaders directly rather than constructing `Settings`.
    These clients are built at module scope by the pipeline CLIs, and going
    through `Settings` would make `python backend/extractor.py` require a JWT
    signing key it never uses.
    """
    return Config(
        connect_timeout=config.load_aws_connect_timeout(),
        read_timeout=config.load_aws_read_timeout(),
        retries={
            "max_attempts": config.load_aws_max_attempts(),
            "mode": "standard",
        },
    )


def get_client(service: str):
    """A memoized boto3 client for `service`, with timeouts applied."""
    with _lock:
        existing = _clients.get(service)
        if existing is not None:
            return existing

    # Built outside the lock: client construction resolves credentials, which
    # can touch the filesystem or IMDS, and holding the lock through that would
    # serialise every first call in the process.
    built = boto3.client(
        service,
        region_name=aws_resources.aws_region(),
        config=client_config(),
    )

    with _lock:
        # Another thread may have won the race; keep whichever landed first so
        # callers always see the same object.
        return _clients.setdefault(service, built)


def get_resource(service: str):
    """A memoized boto3 resource for `service`, with timeouts applied."""
    with _lock:
        existing = _resources.get(service)
        if existing is not None:
            return existing

    built = boto3.resource(
        service,
        region_name=aws_resources.aws_region(),
        config=client_config(),
    )

    with _lock:
        return _resources.setdefault(service, built)


def dynamodb_table(name: str):
    """A Table handle on the shared DynamoDB resource."""
    return get_resource("dynamodb").Table(name)


def reset() -> None:
    """Drop every memoized client. For tests that vary configuration."""
    with _lock:
        _clients.clear()
        _resources.clear()


__all__ = (
    "client_config",
    "dynamodb_table",
    "get_client",
    "get_resource",
    "reset",
)
