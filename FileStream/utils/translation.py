from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from FileStream.config import Telegram

BOT_LINK = f"https://t.me/{Telegram.BOT_USERNAME}"
OWNER_LINK = f"https://t.me/{Telegram.OWNER_USERNAME}"
CHANNEL_LINK = f"https://t.me/{Telegram.UPDATES_CHANNEL}"


class LANG(object):

    START_TEXT = """
<b>Hey {}</b>

I'm <b>My File 2 Link</b> — send me any file and I'll give you a permanent <b>direct download</b> and <b>streaming</b> link in seconds.

<b>What you get</b>
• Instant direct links — no waiting, no ads
• Play videos right in your browser, VLC or MX Player
• Resumable downloads that work with IDM
• Works in private chats and channels

<i>Just send a file to begin.</i>

<b>@{}</b>"""

    HELP_TEXT = """
<b>How to use me</b>

<b>1.</b> Send or forward any file, video, audio or photo here
<b>2.</b> I'll instantly reply with your download and stream links
<b>3.</b> Share those links with anyone — no Telegram account needed

<b>For channels</b>
Add me as an admin with <i>Edit Messages</i> permission and I'll attach a download button to every file you post.

<b>Commands</b>
<code>/start</code> — restart the bot
<code>/files</code> — browse and manage your links
<code>/help</code> — show this message
<code>/about</code> — bot info

<b>Rules</b>
Adult, pirated and illegal content is strictly prohibited and will be removed on report.

<i>Need help? Contact <a href='{}'>Mr.Aman</a></i>"""

    ABOUT_TEXT = """
<b>About this bot</b>

<b>Name</b> — {}
<b>Username</b> — @{}
<b>Version</b> — <code>{}</code>
<b>Language</b> — Python 3
<b>Library</b> — Pyrogram + aiohttp
<b>Database</b> — MongoDB

<b>Owner</b> — <a href='{}'>Mr.Aman</a>

<i>Files are streamed straight from Telegram. Nothing is stored on the server.</i>"""

    STREAM_TEXT = """
<b>Your link is ready</b>

<b>File</b>
<code>{}</code>

<b>Size</b> — <code>{}</code>

<b>Download</b>
<code>{}</code>

<b>Watch online</b>
<code>{}</code>

<b>Get the file back</b>
<code>{}</code>

<i>Links stay active until you revoke them.</i>"""

    STREAM_TEXT_X = """
<b>Your link is ready</b>

<b>File</b>
<code>{}</code>

<b>Size</b> — <code>{}</code>

<b>Download</b>
<code>{}</code>

<b>Get the file back</b>
<code>{}</code>

<i>Links stay active until you revoke them.</i>"""

    BAN_TEXT = """
<b>Access restricted</b>

You can no longer use this bot. This usually happens after abuse of the service or a policy violation.

If you believe this is a mistake, contact <a href='{}'>Mr.Aman</a>."""

    JOIN_TEXT = "<b>One quick step</b>\n\nJoin our channel to unlock the bot, then tap <b>Try again</b>."

    NOT_AUTHORIZED_TEXT = "<b>Access restricted</b>\n\nThis is a private bot. Contact <a href='{}'>Mr.Aman</a> for access."

    ERROR_TEXT = "<b>Something went wrong</b>\n\nPlease try again. If it keeps happening, contact <a href='{}'>Mr.Aman</a>."


class BUTTON(object):

    START_BUTTONS = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton('How to use', callback_data='help'),
                InlineKeyboardButton('About', callback_data='about'),
            ],
            [
                InlineKeyboardButton('My files', callback_data='userfiles_1'),
                InlineKeyboardButton('Updates', url=CHANNEL_LINK),
            ],
            [InlineKeyboardButton('Close', callback_data='close')],
        ]
    )

    HELP_BUTTONS = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton('Home', callback_data='home'),
                InlineKeyboardButton('About', callback_data='about'),
            ],
            [
                InlineKeyboardButton('Contact Mr.Aman', url=OWNER_LINK),
            ],
            [InlineKeyboardButton('Close', callback_data='close')],
        ]
    )

    ABOUT_BUTTONS = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton('Home', callback_data='home'),
                InlineKeyboardButton('How to use', callback_data='help'),
            ],
            [
                InlineKeyboardButton('Contact Mr.Aman', url=OWNER_LINK),
            ],
            [InlineKeyboardButton('Close', callback_data='close')],
        ]
    )

    OWNER_BUTTON = InlineKeyboardMarkup(
        [[InlineKeyboardButton('Contact Mr.Aman', url=OWNER_LINK)]]
    )
