"""Single source of truth for AWS resource names and prefixes.

Phase 4.1. Before this module, bucket and table names were read with
`os.getenv(...)` separately in `extractor.py`, `analyzer.py`, `app.py`,
`projects.py`, and `video_stitcher.py`. Three consequences:

* the default bucket name was repeated in five places, so changing it meant
  finding all five;
* `projects.py` hardcoded `NovaFlowProjects` with no environment override at
  all, while the frames table was overridable - an inconsistency the Phase 3.5
  IAM template had to call out in a comment rather than resolve;
* nothing documented which object prefixes the application actually writes, so
  a least-privilege bucket policy had to be guessed at.

Everything AWS-named now resolves here, from environment variables with
documented defaults. No value is invented: the defaults are the names the code
already used, and an operator overrides them per environment.

OBJECT PREFIX LAYOUT

    sources/{video_id}/source.{ext}            uploaded source video (4.1)
    frames/{video_id}/frame_{n:04d}.jpg        extracted frames
    raw/{project_id}/{agent}/reel.mp4          agent reel upload (projects.py)

Prefixes are part of the contract: the IAM policy and the bucket lifecycle
rules in infra/ are written against exactly these three, so adding a fourth
means updating both.
"""

from __future__ import annotations

import os

# --- Region ----------------------------------------------------------------

DEFAULT_REGION = "us-east-1"


def aws_region() -> str:
    """Region for every client this application builds.

    `AWS_REGION` first, then botocore's own `AWS_DEFAULT_REGION`, so a
    developer who has only the standard variable set is not surprised.
    """
    return (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    ).strip()


# --- S3 --------------------------------------------------------------------

# The name the application has always defaulted to. Kept as the default so an
# existing deployment is unaffected; it is a placeholder, not a real bucket.
DEFAULT_BUCKET = "novaflow-media"

SOURCE_PREFIX = "sources"
FRAME_PREFIX = "frames"
# projects.py issues presigned PUTs under this prefix. Named here so the
# bucket policy and the IAM template are written against the real layout
# rather than a guess.
RAW_PREFIX = "raw"

# Every prefix the application writes. infra/ and the IAM policy enumerate
# these; a new one must be added here and there together.
MANAGED_PREFIXES = (SOURCE_PREFIX, FRAME_PREFIX, RAW_PREFIX)


def media_bucket() -> str:
    """Bucket holding source videos, extracted frames, and attachments.

    One bucket with separate prefixes rather than three buckets: the access
    pattern and lifetime differ by prefix, which lifecycle rules express
    perfectly well, and a single bucket keeps the IAM policy to two
    statements.
    """
    return (os.getenv("S3_BUCKET_NAME") or DEFAULT_BUCKET).strip()


def source_key(video_id: str, extension: str) -> str:
    """Object key for a video's source file.

    Fully server-generated. `video_id` is issued by the server and validated
    against `_VIDEO_ID_RE` before it reaches here; `extension` has already
    passed the upload allowlist. No part of a client filename is used.
    """
    return f"{SOURCE_PREFIX}/{video_id}/source.{extension}"


def frame_key(video_id: str, frame_number: int) -> str:
    """Object key for one extracted frame."""
    return f"{FRAME_PREFIX}/{video_id}/frame_{frame_number:04d}.jpg"


def raw_reel_key(project_id: str, agent_id: str) -> str:
    """Object key for an agent's reel upload against a project."""
    return f"{RAW_PREFIX}/{project_id}/{agent_id}/reel.mp4"


# --- DynamoDB --------------------------------------------------------------

DEFAULT_FRAMES_TABLE = "NovaFlowFrames"
DEFAULT_PROJECTS_TABLE = "NovaFlowProjects"


def frames_table_name() -> str:
    """Frame metadata and per-frame detections. PK video_id (S), SK frame_number (N)."""
    return (os.getenv("DYNAMODB_TABLE_NAME") or DEFAULT_FRAMES_TABLE).strip()


def projects_table_name() -> str:
    """Projects and their submissions. PK project_id (S), SK sk (S).

    Previously hardcoded in `projects.py`. Now overridable like every other
    resource name, so a staging deployment does not have to share the
    production table.
    """
    return (os.getenv("DYNAMODB_PROJECTS_TABLE") or DEFAULT_PROJECTS_TABLE).strip()


__all__ = (
    "DEFAULT_BUCKET",
    "DEFAULT_FRAMES_TABLE",
    "DEFAULT_PROJECTS_TABLE",
    "DEFAULT_REGION",
    "FRAME_PREFIX",
    "MANAGED_PREFIXES",
    "RAW_PREFIX",
    "SOURCE_PREFIX",
    "aws_region",
    "frame_key",
    "frames_table_name",
    "media_bucket",
    "projects_table_name",
    "raw_reel_key",
    "source_key",
)
