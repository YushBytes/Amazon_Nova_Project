"""Repeatable, non-destructive database initialization.

Phase 3.5. Closes `docs/SECURITY_IMPLEMENTATION_PLAN.md` item 33: the
`users.username` unique index was never created, so `signup`'s read-then-write
existence check could be raced into creating two accounts with the same name.

The hard part is that creating a unique index on a collection that already
contains duplicates FAILS. Doing that at startup would either crash the app or,
worse, tempt someone into deleting rows to make it succeed. So this module:

  * detects duplicates first and refuses to attempt the index if any exist;
  * reports only COUNTS, never the usernames themselves, so the diagnostic is
    safe to put in a log or paste into an issue;
  * never deletes, merges, renames, or otherwise modifies an account;
  * is safe to run repeatedly, so it can live in application startup and also
    be invoked deliberately by an operator.

Username matching is deliberately left EXACT and case-sensitive, matching the
existing `find_one({"username": ...})` behaviour. Switching to case-insensitive
matching would make some existing accounts unreachable or collide two distinct
ones, which is an operator decision with data implications, not a code change
to slip in here. See `docs/SECURITY_OPERATIONS.md`.
"""

from __future__ import annotations

import logging

from db import get_database

logger = logging.getLogger(__name__)

USERS_COLLECTION = "users"
VIDEOS_COLLECTION = "videos"


class IndexCreationBlocked(RuntimeError):
    """Raised when existing data prevents a unique index from being created.

    The message carries counts only, never the conflicting values.
    """


async def find_duplicate_username_counts(database=None) -> dict[str, int]:
    """Summarise duplicate usernames without revealing any of them.

    Returns:
        {"groups": <number of usernames held by more than one account>,
         "extra_documents": <how many accounts would have to be resolved>}

    `extra_documents` is the number of accounts beyond the first in each group,
    i.e. exactly how many need an operator decision.
    """
    database = database if database is not None else await get_database()
    collection = database[USERS_COLLECTION]

    pipeline = [
        {"$group": {"_id": "$username", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
    ]

    groups = 0
    extra = 0
    try:
        cursor = collection.aggregate(pipeline)
        async for row in cursor:
            groups += 1
            extra += int(row.get("count", 1)) - 1
    except AttributeError:
        # The collection object does not support aggregation (for example a
        # lightweight test substitute). Fall back to counting in Python.
        seen: dict[str, int] = {}
        async for document in collection.find({}):
            name = document.get("username")
            if name is None:
                continue
            seen[name] = seen.get(name, 0) + 1
        for count in seen.values():
            if count > 1:
                groups += 1
                extra += count - 1

    return {"groups": groups, "extra_documents": extra}


async def ensure_user_indexes(database=None, *, strict: bool = False) -> bool:
    """Create the unique index on `users.username` when it is safe to do so.

    Args:
        database: an open database handle, or None to resolve one.
        strict: when True, raise IndexCreationBlocked if duplicates exist.
            When False (the default, used at startup), log and return False so
            a data problem cannot stop the application from serving.

    Returns:
        True if the index exists after this call, False if it was skipped.

    Raises:
        IndexCreationBlocked: only when `strict=True` and duplicates exist.
    """
    database = database if database is not None else await get_database()
    collection = database[USERS_COLLECTION]

    duplicates = await find_duplicate_username_counts(database)
    if duplicates["groups"]:
        message = (
            "Cannot create the unique index on users.username: "
            f"{duplicates['groups']} username(s) are held by more than one "
            f"account, affecting {duplicates['extra_documents']} extra "
            "account(s). Resolve these before creating the index. No account "
            "was modified. See docs/SECURITY_OPERATIONS.md."
        )
        if strict:
            raise IndexCreationBlocked(message)
        logger.error(message)
        return False

    # Safe to create. create_index is idempotent for an identical spec.
    await collection.create_index("username", unique=True)
    logger.info("users.username unique index is in place")
    return True


async def ensure_video_indexes(database=None) -> None:
    """Create the ownership indexes. Idempotent.

    `video_id` is unique so one id cannot carry two different owners.
    """
    database = database if database is not None else await get_database()
    collection = database[VIDEOS_COLLECTION]
    await collection.create_index("video_id", unique=True)
    await collection.create_index("owner")
    logger.info("video ownership indexes are in place")


async def ensure_remediation_indexes(database=None) -> None:
    """Create the remediation indexes. Idempotent.

    Phase 8. Delegated to `remediation_repo.ensure_indexes` so the collection
    that owns those indexes declares them, rather than this module carrying a
    second copy of the spec that could drift.

    Non-destructive like every other step here: creating an index never
    deletes, merges or rewrites a document.
    """
    import remediation_repo

    if database is None:
        await remediation_repo.ensure_indexes()
    else:
        collection = database[remediation_repo.COLLECTION]
        await collection.create_index("remediation_id", unique=True)
        await collection.create_index("owner")
        await collection.create_index("video_id")
        await collection.create_index("state")
    logger.info("remediation indexes are in place")


async def initialize_database(*, strict: bool = False) -> dict[str, bool]:
    """Run every initialization step. Safe to call on each startup.

    Returns a per-step outcome so a caller (or a test) can assert on it rather
    than inspecting logs.
    """
    database = await get_database()

    results: dict[str, bool] = {}

    try:
        results["video_indexes"] = True
        await ensure_video_indexes(database)
    except Exception:
        results["video_indexes"] = False
        logger.warning("Could not create video ownership indexes", exc_info=False)

    try:
        results["remediation_indexes"] = True
        await ensure_remediation_indexes(database)
    except Exception:
        results["remediation_indexes"] = False
        logger.warning("Could not create remediation indexes", exc_info=False)

    try:
        results["user_index"] = await ensure_user_indexes(database, strict=strict)
    except IndexCreationBlocked:
        if strict:
            raise
        results["user_index"] = False
    except Exception:
        results["user_index"] = False
        logger.warning("Could not create the users.username index", exc_info=False)

    return results


__all__ = (
    "IndexCreationBlocked",
    "USERS_COLLECTION",
    "VIDEOS_COLLECTION",
    "ensure_user_indexes",
    "ensure_video_indexes",
    "find_duplicate_username_counts",
    "initialize_database",
)
