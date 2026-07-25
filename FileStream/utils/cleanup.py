"""
Link lifetime: revoking and 24-hour expiry.

A generated link exists in three places at once:

  * the MongoDB record that backs the public /watch and /dl URLs
  * the copy parked in the private log channel (FLOG_CHANNEL)
  * the messages sitting in the uploader's private chat with the bot

Dropping only the database row leaves the file fully recoverable, so every
path that kills a link -- the "revoke" button, the owner's /del command, the
admin dashboard, and the background expiry sweep -- routes through
purge_file() and clears all three together.
"""

import asyncio
import logging
import time
from typing import Iterable, Optional, Sequence

from pyrogram.errors import FloodWait

from FileStream.bot import FileStream
from FileStream.config import Telegram, Links
from FileStream.utils.database import Database

logger = logging.getLogger(__name__)

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


# ---------------------[ EXPIRY MATHS ]---------------------#

def expires_at(file_info) -> Optional[float]:
    """Epoch second at which this link dies, or None when expiry is switched off."""
    if not Links.ENABLED or not file_info:
        return None
    stamp = file_info.get("expires_at")
    if stamp:
        return float(stamp)
    # Records created before expiry existed fall back to their upload time.
    created = file_info.get("time")
    if created:
        try:
            return float(created) + Links.EXPIRY_SECONDS
        except (TypeError, ValueError):
            return None
    return None


def is_expired(file_info) -> bool:
    deadline = expires_at(file_info)
    return bool(deadline and deadline <= time.time())


def remaining_text(file_info) -> str:
    """Human readable time left, for captions and the file menu."""
    deadline = expires_at(file_info)
    if not deadline:
        return "never"
    left = int(deadline - time.time())
    if left <= 0:
        return "expired"
    hours, rest = divmod(left, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------[ MESSAGE DELETION ]---------------------#

async def _delete_messages(chat_id, message_ids: Sequence[int]) -> int:
    """Best-effort delete. A purge must never abort because a message is already gone."""
    ids = [int(m) for m in dict.fromkeys(message_ids or []) if m]
    if not chat_id or not ids:
        return 0

    for _ in range(2):
        try:
            await FileStream.delete_messages(chat_id=int(chat_id), message_ids=ids)
            return len(ids)
        except FloodWait as e:
            wait = min(int(getattr(e, "value", 5) or 5), 30)
            logger.warning("FloodWait %ss while deleting in %s", wait, chat_id)
            await asyncio.sleep(wait)
        except Exception as e:
            # Already deleted, bot lost admin rights, user blocked the bot, or the
            # message is older than Telegram's delete window. All survivable.
            logger.warning("Could not delete %d message(s) in %s: %s", len(ids), chat_id, e)
            return 0
    return 0


# ---------------------[ THE ONE TRUE PURGE ]---------------------#

async def purge_file(file_info, *, reason: str = "revoked",
                     skip_message_ids: Iterable[int] = ()) -> None:
    """Kill a link everywhere.

    skip_message_ids protects the message the user is currently interacting
    with, so a callback handler can still edit it after the purge.
    """
    if not file_info:
        return

    _id = file_info.get("_id")
    skip = {int(m) for m in (skip_message_ids or ())}

    # 1. The copy stored in the log channel ('the group').
    if Links.DELETE_FROM_LOG:
        await _delete_messages(Telegram.FLOG_CHANNEL, file_info.get("log_msg_ids") or [])

    # 2. The upload and the link message in the uploader's private chat.
    if Links.DELETE_USER_CHAT:
        chat_id = file_info.get("user_chat_id") or file_info.get("user_id")
        # Channel uploads have no private chat to tidy up.
        if chat_id and not str(chat_id).startswith("-100"):
            wanted = [m for m in (file_info.get("user_msg_ids") or []) if int(m) not in skip]
            await _delete_messages(chat_id, wanted)

    # 3. The database record that makes the public URL resolve.
    await db.delete_one_file(_id)
    if file_info.get("user_id"):
        await db.count_links(file_info["user_id"], "-")

    logger.info("Purged file %s (%s)", _id, reason)


async def purge_by_id(db_id, *, reason: str = "revoked",
                      skip_message_ids: Iterable[int] = ()) -> bool:
    """Convenience wrapper for callers that only hold an id."""
    try:
        file_info = await db.get_file(db_id)
    except Exception:
        return False
    await purge_file(file_info, reason=reason, skip_message_ids=skip_message_ids)
    return True


# ---------------------[ BACKGROUND EXPIRY SWEEP ]---------------------#

async def expiry_worker() -> None:
    """Continuously retire links that have outlived LINK_EXPIRY_HOURS."""
    if not Links.ENABLED:
        logger.info("Link expiry disabled (LINK_EXPIRY_HOURS=0) -- links live forever.")
        return

    logger.info(
        "Link expiry active: links live %sh, sweeping every %ss",
        Links.EXPIRY_HOURS, Links.CLEANUP_INTERVAL,
    )
    await db.ensure_expiry_index()
    await asyncio.sleep(15)  # let the clients finish booting first

    while True:
        try:
            expired = await db.get_expired_files(
                time.time(), Links.EXPIRY_SECONDS, limit=Links.SWEEP_BATCH
            )
            for file_info in expired:
                await purge_file(file_info, reason="expired")
                await asyncio.sleep(0.35)  # stay under Telegram's delete rate limit
            if expired:
                logger.info("Expiry sweep retired %d link(s)", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Expiry sweep failed; will retry next cycle")

        await asyncio.sleep(Links.CLEANUP_INTERVAL)
