"""
Batch link delivery.

Sending ten files used to mean ten separate replies and ten manual copies.
This collects everything a user uploads in one burst and answers with a single
copy-once block -- or a links.txt when the list outgrows one message -- which
pastes straight into 1DM+ / IDM's "add multiple links" box.

The per-file replies are left untouched: they carry the stream button, the
revoke button and the file's own details. This is an extra summary on top.
"""

import asyncio
import io
import logging
from typing import Dict, List, Optional

from pyrogram.enums.parse_mode import ParseMode

from FileStream.bot import FileStream
from FileStream.config import Telegram, Server, Links, Batch
from FileStream.utils.database import Database
from FileStream.utils.human_readable import humanbytes

logger = logging.getLogger(__name__)

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

# Telegram caps a message at 4096 characters. Leave headroom for the wrapper.
_TEXT_BUDGET = 3400

_pending: Dict[int, "_Burst"] = {}


class _Burst:
    """Links generated for one user since they last went quiet."""

    __slots__ = ("chat_id", "items", "task")

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.items: List[dict] = []
        self.task: Optional[asyncio.Task] = None


def link_for(_id) -> str:
    return f"{Server.URL}dl/{_id}"


# ---------------------[ COLLECTING A BURST ]---------------------#

async def add(chat_id: int, user_id: int, item: dict) -> None:
    """Record one freshly minted link and restart the quiet-period timer."""
    if not Batch.ENABLED:
        return

    burst = _pending.get(user_id)
    if burst is None:
        burst = _Burst(chat_id)
        _pending[user_id] = burst

    burst.chat_id = chat_id
    if len(burst.items) < Batch.MAX_FILES:
        burst.items.append(item)

    # Every new file pushes the deadline back, so the summary only lands once
    # the user has actually stopped uploading.
    if burst.task and not burst.task.done():
        burst.task.cancel()
    burst.task = asyncio.create_task(_wait_then_flush(user_id))


async def _wait_then_flush(user_id: int) -> None:
    try:
        await asyncio.sleep(Batch.WINDOW)
    except asyncio.CancelledError:
        return  # another file arrived; a fresh timer has taken over
    try:
        burst = _pending.pop(user_id, None)
        if burst and len(burst.items) >= Batch.MIN_FILES:
            # A lone file already got a perfectly good reply of its own.
            await deliver(burst.chat_id, burst.items)
    except Exception:
        logger.exception("Could not deliver batch summary to %s", user_id)


# ---------------------[ DELIVERY ]---------------------#

async def deliver(chat_id: int, items: List[dict], title: Optional[str] = None) -> None:
    """Send one copy-everything summary for a list of links."""
    if not items:
        return

    urls = [it["dl"] for it in items]
    total = 0
    for it in items:
        try:
            total += int(it.get("size") or 0)
        except (TypeError, ValueError):
            pass

    heading = title or f"📦 <b>{len(items)} ʟɪɴᴋs ʀᴇᴀᴅʏ</b>"
    if total:
        heading += f" · {humanbytes(total)}"

    hint = (
        "Tᴀᴘ ᴛʜᴇ ʙʟᴏᴄᴋ ᴛᴏ ᴄᴏᴘʏ ᴇᴠᴇʀʏ ʟɪɴᴋ ᴀᴛ ᴏɴᴄᴇ, ᴛʜᴇɴ ɪɴ "
        "<b>1DM+</b> ᴛᴀᴘ <i>+ → ᴘᴀsᴛᴇ ʟɪɴᴋs</i>."
    )
    footer = (
        f"\n\n⏳ <i>Tʜᴇsᴇ ʟɪɴᴋs ᴇxᴘɪʀᴇ ɪɴ {Links.EXPIRY_HOURS} ʜᴏᴜʀs.</i>"
        if Links.ENABLED else ""
    )

    block = "\n".join(urls)
    body = f"{heading}\n\n{hint}\n\n<pre>{block}</pre>{footer}"

    if len(body) <= _TEXT_BUDGET:
        await FileStream.send_message(
            chat_id=chat_id,
            text=body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # Too many links for one message: hand over a plain URL list instead, which
    # 1DM+ and IDM can both import directly. No comments or blank lines -- some
    # importers choke on anything that isn't a bare URL.
    payload = io.BytesIO(("\n".join(urls) + "\n").encode("utf-8"))
    payload.name = "links.txt"
    caption = (
        f"{heading}\n\nTᴏᴏ ᴍᴀɴʏ ʟɪɴᴋs ғᴏʀ ᴏɴᴇ ᴍᴇssᴀɢᴇ, sᴏ ʜᴇʀᴇ ᴛʜᴇʏ ᴀʀᴇ ᴀs ᴀ ғɪʟᴇ.\n"
        f"Iɴ <b>1DM+</b> ᴛᴀᴘ <i>+ → Iᴍᴘᴏʀᴛ ʟɪɴᴋs ғʀᴏᴍ ғɪʟᴇ</i> ᴀɴᴅ ᴘɪᴄᴋ ɪᴛ.{footer}"
    )
    await FileStream.send_document(
        chat_id=chat_id,
        document=payload,
        file_name="links.txt",
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


# ---------------------[ ON DEMAND ]---------------------#

async def send_recent(chat_id: int, user_id: int, count: int) -> int:
    """Re-send the newest `count` links for a user. Backs the /links command."""
    count = max(1, min(int(count), Batch.MAX_FILES))
    cursor, _total = await db.find_files(user_id, [1, count])

    items = []
    async for f in cursor:
        items.append({
            "name": f.get("file_name"),
            "size": f.get("file_size"),
            "dl": link_for(f["_id"]),
        })

    if not items:
        return 0

    await deliver(chat_id, items, title=f"📎 <b>Yᴏᴜʀ ʟᴀsᴛ {len(items)} ʟɪɴᴋs</b>")
    return len(items)
