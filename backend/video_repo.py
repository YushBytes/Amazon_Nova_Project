"""Ownership records for uploaded videos.

Phase 3. The previous implementation stored uploads as `uploads/{video_id}.{ext}`
with no record of who uploaded them, and DynamoDB's NovaFlowFrames table keys
only on `video_id`. There was therefore nothing in the system that could answer
"whose video is this?", which made every per-video route an IDOR.

This module adds the smallest ownership store that the existing architecture
allows: a `videos` collection in the MongoDB database that already holds user
accounts. It introduces no new database or storage system, and the S3 +
DynamoDB analysis pipeline is unchanged - frames still key on `video_id`, and
authorization happens before any frame is read.

Ownership is recorded at upload time, which is the only moment the uploader's
identity is known. Videos uploaded before this change have no record; see
`OwnershipUnknown` and docs/SECURITY_AUDIT.md for how those are treated.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from db import get_database

logger = logging.getLogger(__name__)

COLLECTION = "videos"


class OwnershipUnknown(LookupError):
    """No ownership record exists for this video id.

    Raised for videos uploaded before ownership tracking existed, or for an id
    that was never issued. Callers must treat this as "deny", not "allow".
    """


async def _collection():
    database = await get_database()
    return database[COLLECTION]


async def ensure_indexes() -> None:
    """Create the indexes this collection relies on.

    Safe to call repeatedly. `video_id` is unique so a duplicate id cannot
    produce two ownership records with different owners.
    """
    col = await _collection()
    await col.create_index("video_id", unique=True)
    await col.create_index("owner")


async def record_video(
    *,
    video_id: str,
    owner: str,
    stored_filename: str,
    original_filename: str | None,
    extension: str,
    size_bytes: int,
) -> dict:
    """Persist the ownership record for a freshly uploaded video.

    `original_filename` is retained as display metadata only and is never used
    to build a filesystem path or storage key.
    """
    document = {
        "video_id": video_id,
        "owner": owner,
        "stored_filename": stored_filename,
        "original_filename": original_filename,
        "extension": extension,
        "size_bytes": size_bytes,
        "status": "UPLOADED",
        "created_at": datetime.now(timezone.utc),
    }
    col = await _collection()
    await col.insert_one(document)
    return document


async def get_video(video_id: str) -> dict:
    """Return the ownership record, or raise OwnershipUnknown."""
    col = await _collection()
    record = await col.find_one({"video_id": video_id})
    if not record:
        raise OwnershipUnknown(video_id)
    return record


async def is_owner(video_id: str, username: str) -> bool:
    """True only when an ownership record exists and names this user."""
    try:
        record = await get_video(video_id)
    except OwnershipUnknown:
        return False
    return record.get("owner") == username


async def list_videos_for_owner(username: str, limit: int = 100) -> list[dict]:
    """Return this user's videos, newest first. Never another user's."""
    col = await _collection()
    cursor = col.find({"owner": username}).sort("created_at", -1).limit(limit)
    return [doc async for doc in cursor]


async def set_status(video_id: str, status: str) -> None:
    """Record a coarse lifecycle status against the ownership record."""
    col = await _collection()
    await col.update_one(
        {"video_id": video_id},
        {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
    )
