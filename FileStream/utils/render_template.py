import aiohttp
import jinja2
import urllib.parse
from FileStream.config import Telegram, Server
from FileStream.utils.database import Database
from FileStream.utils.human_readable import humanbytes
from FileStream.utils.translation import BOT_LINK
from FileStream.utils.cleanup import is_expired
from FileStream.server.exceptions import LinkExpired
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

async def render_page(db_id):
    file_data=await db.get_file(db_id)

    # The sweeper runs on an interval, so a link can be past its deadline while
    # its record is still around. Refuse it either way.
    if file_data and is_expired(file_data):
        raise LinkExpired
    
    if not file_data:
        with open("FileStream/template/dmca.html") as f:
            template = jinja2.Template(f.read())
        return template.render(bot_url=BOT_LINK)

    src = urllib.parse.urljoin(Server.URL, f'dl/{file_data["_id"]}')
    file_size = humanbytes(file_data['file_size'])
    file_name = file_data['file_name'].replace("_", " ")

    if str((file_data['mime_type']).split('/')[0].strip()) == 'video':
        template_file = "FileStream/template/play.html"
    else:
        template_file = "FileStream/template/dl.html"

    with open(template_file) as f:
        template = jinja2.Template(f.read())

    return template.render(
        file_name=file_name,
        file_url=src,
        file_size=file_size,
        db_id=str(file_data["_id"]),
        server_url=Server.URL.rstrip('/')
    )
