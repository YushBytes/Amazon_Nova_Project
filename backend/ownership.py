"""Video identity validation and the per-video ownership check.

Phase 8. This logic lived in `app.py` as `require_video_owner`, which made it
unreachable from a router module: `app.py` imports the routers, so a router
importing `app` would be circular. Rather than write a second ownership check
for the remediation routes - the single most dangerous thing to duplicate in
this codebase, because a divergent copy is an IDOR - it moved here and both
call this one.

`app.require_video_owner` is kept as a thin delegate so existing call sites and
the external behaviour are unchanged.

The rules enforced here are the ones from CLAUDE.md §5 and must not drift:

* The video id must match the server-generated shape exactly. Nothing
  client-supplied is ever interpolated into a storage path, and a malformed id
  is rejected before any lookup.
* A MISSING ownership record is a DENIAL, not a pass. Videos uploaded before
  ownership tracking existed therefore cannot be reached through any route.
* 404 is returned for both "no such video" and "not your video", so the
  endpoint cannot be used to discover which ids exist.
"""

from __future__ import annotations

import logging
import re

from fastapi import HTTPException

import video_repo
from video_repo import OwnershipUnknown

logger = logging.getLogger(__name__)

# A stored video id is always server-generated: "vid_" + 8 lowercase hex.
VIDEO_ID_RE = re.compile(r"^vid_[0-9a-f]{8}$")


def validate_video_id(video_id: str) -> str:
    """Return the id if it has the server-generated shape, else raise 400.

    Separate from the ownership check so a caller that only needs the shape
    (the playback route derives an id from a filename) does not also perform a
    database read.
    """
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid video id")
    return video_id


async def require_video_owner(video_id: str, user: dict) -> dict:
    """Authorize the caller for this video, or raise.

    Returns the ownership record.

    Raises:
        HTTPException: 400 for a malformed id; 404 when the video does not
            exist, has no ownership record, or belongs to someone else.
    """
    validate_video_id(video_id)

    try:
        record = await video_repo.get_video(video_id)
    except OwnershipUnknown:
        logger.info("Denied access to a video with no ownership record")
        raise HTTPException(status_code=404, detail="Video not found")

    if record.get("owner") != user["sub"]:
        logger.warning("Denied cross-user access attempt to a video")
        raise HTTPException(status_code=404, detail="Video not found")

    return record


__all__ = ("VIDEO_ID_RE", "require_video_owner", "validate_video_id")
