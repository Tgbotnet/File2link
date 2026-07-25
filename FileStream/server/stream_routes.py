import time
import math
import logging
import mimetypes
import traceback
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream.config import Telegram, Server, Download
from FileStream.server.exceptions import FIleNotFound, InvalidHash, LinkExpired
from FileStream import utils, StartTime, __version__
import hmac
import os
from FileStream.utils.render_template import render_page
from FileStream.utils.cleanup import is_expired, purge_file
from FileStream.utils.translation import BOT_LINK, OWNER_LINK
import base64
import asyncio
import jinja2
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from FileStream.utils.database import Database
from FileStream.utils.human_readable import humanbytes
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

routes = web.RouteTableDef()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "template")


@routes.get("/static/{filename}", allow_head=True)
async def static_handler(request: web.Request):
    filename = os.path.basename(request.match_info["filename"])
    if not filename.endswith((".css", ".js", ".svg", ".png", ".jpg", ".ico", ".webp")):
        raise web.HTTPNotFound
    path = os.path.join(STATIC_DIR, filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@routes.get("/favicon.ico", allow_head=True)
async def favicon_handler(request: web.Request):
    return web.FileResponse(os.path.join(BASE_DIR, 'logo.ico'))

@routes.get("/logo.png", allow_head=True)
async def logo_png_handler(request: web.Request):
    return web.FileResponse(os.path.join(BASE_DIR, 'logo.png'))

def check_auth(request: web.Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
        if not Server.ADMIN_ENABLED:
            return False
        if hmac.compare_digest(username, Server.ADMIN_USERNAME) and hmac.compare_digest(password, Server.ADMIN_PASSWORD):
            return True
    except Exception:
        pass
    return False

@routes.get("/admin", allow_head=True)
async def admin_dashboard(request: web.Request):
    if not check_auth(request):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Admin Access"'},
            text="Unauthorized"
        )
    
    total_bandwidth = await db.get_total_bandwidth()
    bw_daily, bw_weekly, bw_monthly = await db.get_bandwidth_stats()
    top_users = await db.get_top_users_by_bandwidth(50)
    top_files = await db.get_top_files_by_bandwidth(50)
    reported_files = await db.get_reported_files(50)
    
    banned_docs = await db.black.find({}).to_list(length=None)
    banned_ids = [d["id"] for d in banned_docs]

    try:
        with open("FileStream/template/admin.html") as f:
            template = jinja2.Template(f.read())
        html_content = template.render(
            total_bandwidth=humanbytes(total_bandwidth),
            bw_daily=humanbytes(bw_daily),
            bw_weekly=humanbytes(bw_weekly),
            bw_monthly=humanbytes(bw_monthly),
            top_users=top_users,
            top_files=top_files,
            reported_files=reported_files,
            banned_docs=banned_docs,
            banned_ids=banned_ids,
            humanbytes=humanbytes
        )
        return web.Response(text=html_content, content_type='text/html')
    except BaseException as e:
        return web.Response(text=f"Error rendering admin: {e}", status=500)

@routes.post("/admin/warn")
async def admin_warn_user(request: web.Request):
    if not check_auth(request):
        return web.Response(status=401, text="Unauthorized")
    data = await request.post()
    user_id = data.get("user_id")
    try:
        await FileStream.send_message(
            chat_id=int(user_id),
            text="⚠️ **WARNING**\n\nYour bandwidth usage is abnormally high. Please refrain from directly embedding bots links on external sites or you may be banned.",
            parse_mode=ParseMode.MARKDOWN
        )
        return web.Response(text="User warned successfully.")
    except Exception as e:
        return web.Response(text=f"Failed to warn user: {e}", status=500)

@routes.post("/admin/ban")
async def admin_ban_user(request: web.Request):
    if not check_auth(request):
        return web.Response(status=401, text="Unauthorized")
    data = await request.post()
    user_id = data.get("user_id")
    try:
        await FileStream.send_message(
            chat_id=int(user_id),
            text="🚫 **BANNED**\n\nYou have been banned from using this service due to bandwidth abuse. Contact the developer if you think this is a mistake.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Contact Mr.Aman", url=OWNER_LINK)
            ]])
        )
    except Exception:
        pass # Send message could fail if they blocked the bot
    try:
        await db.ban_user(int(user_id))
        # purposely NOT deleting the user from db so they remain in the bandwidth table
        return web.Response(text="User banned successfully.")
    except Exception as e:
        return web.Response(text=f"Failed to ban user: {e}", status=500)

@routes.post("/admin/unban")
async def admin_unban_user(request: web.Request):
    if not check_auth(request):
        return web.Response(status=401, text="Unauthorized")
    data = await request.post()
    user_id = data.get("user_id")
    try:
        await FileStream.send_message(
            chat_id=int(user_id),
            text="✅ **UNBANNED**\n\nYour access to the bot has been restored! Please be mindful of your bandwidth usage.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass
    try:
        await db.unban_user(int(user_id))
        return web.Response(text="User unbanned successfully.")
    except Exception as e:
        return web.Response(text=f"Failed to unban user: {e}", status=500)

@routes.post("/admin/delete_file")
async def admin_delete_file(request: web.Request):
    if not check_auth(request):
        return web.Response(status=401, text="Unauthorized")
    data = await request.post()
    file_id = data.get("file_id")
    try:
        file_data = await db.get_file(file_id)
        if file_data:
            user_id = file_data.get("user_id")
            file_name = file_data.get("file_name", "Unknown_File")
            
            try:
                msg = f"🚫 **DMCA / TOS TAKEDOWN NOTICE** 🚫\n\nYour file **{file_name}** has been permanently revoked and wiped from our servers due to a Community Guidelines or Copyright violation.\n\nAll generated proxy links spanning across the web pointing to this payload are profoundly blocked."
                await FileStream.send_message(chat_id=user_id, text=msg)
            except Exception as e:
                logging.error(f"Failed to DMCA-Warn user {user_id}: {e}")
                
        if file_data:
            # Full purge: kills the link, the log-channel copy and the user's chat.
            await purge_file(file_data, reason="admin dashboard takedown")
        else:
            await db.delete_one_file(file_id)
        return web.Response(text="File wiped: link revoked and stored copy removed.")
    except Exception as e:
        return web.Response(text=f"Failed to wipe file: {e}", status=500)

@routes.post("/report/{db_id}")
async def report_file_route(request: web.Request):
    try:
        db_id = request.match_info["db_id"]
        # Atomically flag the file inside MongoDB bypassing all authentication 
        await db.report_file(db_id)
        return web.Response(text="Report securely transmitted to the Admin.", status=200)
    except Exception as e:
        return web.Response(text="Error logging report.", status=500)

@routes.get("/status", allow_head=True)
async def root_route_handler(_):
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "telegram_bot": "@" + FileStream.username,
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

@routes.get("/watch/{path}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return web.Response(text=await render_page(path), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except LinkExpired:
        with open("FileStream/template/dmca.html") as f:
            template = jinja2.Template(f.read())
        return web.Response(text=template.render(bot_url=BOT_LINK),
                            content_type='text/html', status=410)
    except FIleNotFound as e:
        with open("FileStream/template/dmca.html") as f:
            template = jinja2.Template(f.read())
        return web.Response(text=template.render(bot_url=BOT_LINK), content_type='text/html')
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass


@routes.get("/dl/{path}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return await media_streamer(request, path)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except LinkExpired:
        with open("FileStream/template/dmca.html") as f:
            template = jinja2.Template(f.read())
        return web.Response(text=template.render(bot_url=BOT_LINK),
                            content_type='text/html', status=410)
    except FIleNotFound as e:
        with open("FileStream/template/dmca.html") as f:
            template = jinja2.Template(f.read())
        return web.Response(text=template.render(bot_url=BOT_LINK), content_type='text/html')
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        traceback.print_exc()
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=str(e))

class_cache = {}

async def media_streamer(request: web.Request, db_id: str):
    range_header = request.headers.get("Range", 0)

    # Check expiry before doing any Telegram work. The sweeper runs on an
    # interval, so a link can be past its deadline but not yet purged.
    file_info_db = await db.get_file(db_id)
    if is_expired(file_info_db):
        raise LinkExpired
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if Telegram.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.headers.get('X-FORWARDED-FOR',request.remote)}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(db_id, multi_clients)
    logging.debug("after calling get_file_properties")

    user_id = file_info_db.get("user_id") if file_info_db else None

    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if not range_header or range_header == "bytes=0-":
        asyncio.create_task(db.inc_download_count(db_id))

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = Download.CHUNK_SIZE
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil((until_bytes + 1) / chunk_size) - (offset // chunk_size)

    # Only fan out once the request is big enough to be worth it. Small range
    # requests (player seeks, metadata probes) stay single-shot.
    if req_length >= Download.PARALLEL_MIN_BYTES:
        parallel = min(Download.MAX_PARALLEL_PARTS, part_count)
    else:
        parallel = 1

    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count,
        chunk_size, parallel
    )

    mime_type = file_id.mime_type
    file_name = utils.get_name(file_id)
    disposition = "attachment"

    if not mime_type:
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    #     disposition = "inline"
    
    async def body_generator_wrapper(body_gen):
        total_bytes = 0
        try:
            async for chunk in body_gen:
                total_bytes += len(chunk)
                yield chunk
        except Exception as e:
            logging.warning(f"Stream interrupted gracefully from client {index}: {str(e)}")
        finally:
            if total_bytes > 0:
                asyncio.create_task(db.update_bandwidth(db_id, user_id, total_bytes))

    return web.Response(
        status=206 if range_header else 200,
        body=body_generator_wrapper(body),
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
