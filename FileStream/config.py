import os
from os import environ as env
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")


def _asset(name: str) -> str:
    """Local branded image shipped with the bot. Pyrogram accepts a file path."""
    return os.path.join(ASSETS_DIR, name)


def _int(key: str, default=None):
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        if default is None:
            raise ValueError(f"Missing required environment variable: {key}")
        return int(default)
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Environment variable {key} must be an integer, got: {raw!r}")


def _bool(key: str, default: bool = False) -> bool:
    return str(env.get(key, default)).strip().lower() in ("1", "true", "t", "yes", "y")


class Telegram:
    # ---- required ----
    API_ID = _int("API_ID")
    API_HASH = str(env.get("API_HASH", ""))
    BOT_TOKEN = str(env.get("BOT_TOKEN", ""))
    OWNER_ID = _int("OWNER_ID")
    DATABASE_URL = str(env.get("DATABASE_URL", ""))
    FLOG_CHANNEL = _int("FLOG_CHANNEL")   # channel that stores forwarded files
    ULOG_CHANNEL = _int("ULOG_CHANNEL")   # channel that logs new users

    # ---- branding ----
    BOT_USERNAME = str(env.get("BOT_USERNAME", "My_file_2_link_bot")).lstrip("@")
    OWNER_USERNAME = str(env.get("OWNER_USERNAME", "My_file_2_link_bot")).lstrip("@")
    UPDATES_CHANNEL = str(env.get("UPDATES_CHANNEL", "My_file_2_link_bot")).lstrip("@")

    # Branded images shipped in FileStream/assets. Override with a URL if you prefer.
    START_PIC = env.get("START_PIC", _asset("start.jpg"))
    FILE_PIC = env.get("FILE_PIC", _asset("files.jpg"))
    VERIFY_PIC = env.get("VERIFY_PIC", _asset("verify.jpg"))

    # ---- behaviour ----
    WORKERS = _int("WORKERS", 6)
    SESSION_NAME = str(env.get("SESSION_NAME", "MyFile2Link"))
    SLEEP_THRESHOLD = _int("SLEEP_THRESHOLD", 60)
    FORCE_SUB_ID = env.get("FORCE_SUB_ID", None)
    FORCE_SUB = _bool("FORCE_SUB", False)
    MULTI_CLIENT = False
    MODE = str(env.get("MODE", "primary"))
    SECONDARY = MODE.strip().lower() == "secondary"
    AUTH_USERS = sorted({int(x) for x in str(env.get("AUTH_USERS", "")).split()})


class Download:
    """Tuning for the parallel chunk downloader."""

    # Telegram caps upload.GetFile at 1 MB per call and this must stay a power-of-two
    # divisor of 1 MB, so leave it alone unless you know why you are changing it.
    CHUNK_SIZE = 1024 * 1024

    # How many 1 MB chunks of a SINGLE download may be in flight at once.
    MAX_PARALLEL_PARTS = max(1, min(_int("MAX_PARALLEL_PARTS", 40), 40))

    # Separate MTProto media connections opened per client, per data centre.
    # One connection serialises its own queue, so this is what the parallel
    # requests are actually spread across. Raise toward 16 if your line is
    # faster than the bot manages to saturate; lower it if Telegram throttles.
    SESSIONS_PER_DC = max(1, min(_int("SESSIONS_PER_DC", 12), 16))

    # Requests smaller than this stay single-shot; parallelism would only add
    # overhead. Default 1 MB, matching the "1 MB+" threshold.
    PARALLEL_MIN_BYTES = _int("PARALLEL_MIN_MB", 1) * 1024 * 1024

    # Process-wide ceiling on in-flight chunks across every active download.
    # Roughly the peak streaming RAM budget in MB.
    MAX_TOTAL_PARTS = max(MAX_PARALLEL_PARTS, _int("MAX_TOTAL_PARTS", 240))

    # Tail-latency control. If a single 1 MB chunk has not come back within this
    # many milliseconds, the same chunk is asked for again on a second
    # connection and whichever reply lands first wins. Bytes must reach the
    # client strictly in order, so one stalled chunk freezes the whole stream
    # even when forty other parts are already sitting in memory -- that is what
    # produces the drop-and-spike sawtooth. Set to 0 to disable hedging.
    HEDGE_AFTER_MS = max(0, _int("HEDGE_AFTER_MS", 2500))

    # Ceiling on requests queued onto any single connection. Stops a burst from
    # piling 40 requests onto 12 sockets, where the surplus only adds latency.
    MAX_INFLIGHT_PER_SESSION = max(1, _int("MAX_INFLIGHT_PER_SESSION", 6))


class Naming:
    """How uploaded files get named."""

    # Prefer the message caption over the document's own file name. Uploads very
    # often carry the real title in the caption while the document itself is
    # called something like "video_2026-07-25_13-47-11.mp4".
    USE_CAPTION = _bool("NAME_FROM_CAPTION", True)

    # Captions can be paragraphs long. Keep only the first N words.
    MAX_WORDS = max(1, _int("CAPTION_MAX_WORDS", 13))

    # Hard character cap, so 13 very long words cannot produce a name that
    # breaks Content-Disposition or the user's filesystem.
    MAX_CHARS = max(20, _int("CAPTION_MAX_CHARS", 120))


class Links:
    """How long a generated link stays alive, and what gets wiped when it dies."""

    # Hours a link remains valid. Set to 0 to disable expiry and keep links forever.
    EXPIRY_HOURS = _int("LINK_EXPIRY_HOURS", 24)
    EXPIRY_SECONDS = EXPIRY_HOURS * 3600
    ENABLED = EXPIRY_HOURS > 0

    # How often the background sweeper hunts for dead links, in seconds.
    CLEANUP_INTERVAL = max(60, _int("CLEANUP_INTERVAL", 300))

    # Maximum links purged in a single sweep, so one cycle can't hammer Telegram.
    SWEEP_BATCH = max(1, _int("SWEEP_BATCH", 200))

    # On revoke/expiry, also delete the stored copy from the log channel.
    DELETE_FROM_LOG = _bool("DELETE_FROM_LOG", True)

    # On revoke/expiry, also delete the upload and the link message from the
    # uploader's private chat with the bot.
    DELETE_USER_CHAT = _bool("DELETE_USER_CHAT", True)


class Batch:
    """Bundling many links into one copy-once message."""

    # Send a combined summary when a user uploads several files in one go.
    ENABLED = _bool("BATCH_LINKS", True)

    # Seconds of silence after the last upload before the summary is sent.
    WINDOW = max(2, _int("BATCH_WINDOW", 6))

    # A single file already gets its own reply, so don't summarise below this.
    MIN_FILES = max(2, _int("BATCH_MIN_FILES", 2))

    # Upper bound on links in one summary (also caps /links).
    MAX_FILES = max(2, min(_int("BATCH_MAX_FILES", 100), 500))


class Tunnel:
    """
    Cloudflare Zero Trust tunnel, started automatically with the bot.

    Paste the token from Zero Trust > Networks > Tunnels into TUNNEL_TOKEN and
    the bot runs cloudflared itself, so a machine with no public IP and no open
    ports can still serve links. The public hostname and its origin service are
    configured in the Cloudflare dashboard, not here.
    """

    TOKEN = str(env.get("TUNNEL_TOKEN", "")).strip()

    # A token is required, so an empty one disables the tunnel on its own.
    ENABLED = _bool("TUNNEL_ENABLED", True) and bool(TOKEN)

    # Explicit path to a cloudflared binary. Leave blank to search $PATH and
    # then fall back to the auto-installed copy.
    BINARY = str(env.get("CLOUDFLARED_PATH", "")).strip()

    # Download the official cloudflared release when none is installed.
    AUTO_INSTALL = _bool("TUNNEL_AUTO_INSTALL", True)
    INSTALL_DIR = str(env.get("TUNNEL_INSTALL_DIR", "bin")).strip() or "bin"

    # "auto", "quic" or "http2". Try http2 on networks that block UDP/7844.
    PROTOCOL = str(env.get("TUNNEL_PROTOCOL", "auto")).strip() or "auto"
    LOGLEVEL = str(env.get("TUNNEL_LOGLEVEL", "info")).strip() or "info"

    # Extra cloudflared flags, space separated.
    EXTRA_ARGS = str(env.get("TUNNEL_EXTRA_ARGS", "")).split()

    # Restart backoff, so a permanently bad token cannot become a hot loop.
    RESTART_DELAY = max(1, _int("TUNNEL_RESTART_DELAY", 5))
    MAX_RESTART_DELAY = max(RESTART_DELAY, _int("TUNNEL_MAX_RESTART_DELAY", 300))


class Server:
    PORT = _int("PORT", 8080)
    BIND_ADDRESS = str(env.get("BIND_ADDRESS", "0.0.0.0"))
    PING_INTERVAL = _int("PING_INTERVAL", 1200)
    HAS_SSL = _bool("HAS_SSL", False)
    NO_PORT = _bool("NO_PORT", False)
    FQDN = str(env.get("FQDN", BIND_ADDRESS))
    URL = "http{}://{}{}/".format(
        "s" if HAS_SSL else "", FQDN, "" if NO_PORT else ":" + str(PORT)
    )

    # Admin dashboard credentials. There is no default password on purpose —
    # if ADMIN_PASSWORD is unset the /admin dashboard stays disabled.
    ADMIN_USERNAME = str(env.get("ADMIN_USERNAME", ""))
    ADMIN_PASSWORD = str(env.get("ADMIN_PASSWORD", ""))
    ADMIN_ENABLED = bool(ADMIN_USERNAME and ADMIN_PASSWORD)
