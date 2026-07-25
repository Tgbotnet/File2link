"""
Cloudflare Zero Trust tunnel, started and supervised alongside the bot.

Drop a dashboard-issued tunnel token into TUNNEL_TOKEN and the bot runs
`cloudflared tunnel run --token ...` as a child process for its whole lifetime,
so a box with no public IP and no open ports still serves download links.

What this module does NOT do: decide which hostname points at the bot. With a
token-based (remotely managed) tunnel, the public hostname and its origin
service live in the Cloudflare dashboard, not in this repo. Set the public
hostname's service to http://localhost:<PORT> there, and set FQDN here to the
same hostname, or every generated link will still point at localhost.
"""
import asyncio
import base64
import binascii
import json
import logging
import os
import platform
import shutil
import stat
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp

from FileStream.config import BASE_DIR, Server, Tunnel

logger = logging.getLogger(__name__)

_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# uname machine -> the suffix cloudflare publishes releases under
_ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "i386": "386",
    "i686": "386",
}

_process: Optional[asyncio.subprocess.Process] = None
_supervisor: Optional[asyncio.Task] = None
_stopping = False


# --------------------------------------------------------------------- helpers

def _redact(text: str) -> str:
    """Never let the tunnel token reach the log file."""
    if Tunnel.TOKEN and Tunnel.TOKEN in text:
        return text.replace(Tunnel.TOKEN, "<TUNNEL_TOKEN>")
    return text


def tunnel_id_from_token(token: str) -> Optional[str]:
    """
    Pull the tunnel UUID out of the token, purely so the startup log can show
    which tunnel is being run. The token is base64 JSON of {a, t, s}; only 't'
    is ever read, never the secret.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8"))
        value = payload.get("t")
        return str(value) if value else None
    except (binascii.Error, ValueError, UnicodeDecodeError, TypeError):
        return None


def _asset_name() -> Optional[str]:
    """The release asset for this machine, or None if we shouldn't auto-install."""
    if platform.system() != "Linux":
        # macOS and Windows ship as archives and installers; not worth guessing.
        return None
    arch = _ARCH_MAP.get(platform.machine().lower())
    return f"cloudflared-linux-{arch}" if arch else None


async def _runs_ok(path: Path) -> Tuple[bool, str]:
    """Confirm a binary actually executes on this machine before relying on it."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path), "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return proc.returncode == 0, out.decode(errors="replace").strip()
    except (OSError, asyncio.TimeoutError) as err:
        return False, str(err)


async def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    timeout = aiohttp.ClientTimeout(total=600)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            with open(partial, "wb") as handle:
                async for block in response.content.iter_chunked(1 << 16):
                    handle.write(block)

    partial.replace(dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


async def ensure_binary() -> Optional[Path]:
    """
    Find a usable cloudflared, downloading one if allowed.

    Order: CLOUDFLARED_PATH, then $PATH, then a previously downloaded copy,
    then a fresh download from the official GitHub release.
    """
    candidates: List[Path] = []

    if Tunnel.BINARY:
        candidates.append(Path(Tunnel.BINARY).expanduser())

    found = shutil.which("cloudflared")
    if found:
        candidates.append(Path(found))

    install_dir = Path(Tunnel.INSTALL_DIR)
    if not install_dir.is_absolute():
        install_dir = Path(BASE_DIR) / install_dir
    local_copy = install_dir / "cloudflared"
    candidates.append(local_copy)

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            ok, version = await _runs_ok(candidate)
            if ok:
                logger.info("Using cloudflared at %s (%s)", candidate, version)
                return candidate
            logger.warning("cloudflared at %s is not runnable: %s", candidate, version)

    if not Tunnel.AUTO_INSTALL:
        logger.error(
            "cloudflared not found and TUNNEL_AUTO_INSTALL is off. "
            "Install it or set CLOUDFLARED_PATH."
        )
        return None

    asset = _asset_name()
    if not asset:
        logger.error(
            "No cloudflared build to auto-install for %s/%s. "
            "Install it manually and set CLOUDFLARED_PATH.",
            platform.system(), platform.machine(),
        )
        return None

    url = _RELEASE_BASE + "/" + asset
    logger.info("Downloading cloudflared from %s", url)
    try:
        await _download(url, local_copy)
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        logger.error("Could not download cloudflared: %s", err)
        return None

    ok, version = await _runs_ok(local_copy)
    if not ok:
        logger.error("Downloaded cloudflared will not run: %s", version)
        return None

    logger.info("Installed cloudflared at %s (%s)", local_copy, version)
    return local_copy


def _warn_about_url() -> None:
    """
    A tunnel that works while every link still says localhost is the single most
    likely way to misconfigure this, so say so loudly rather than silently.
    """
    host = (Server.FQDN or "").strip().lower()
    if host in ("", "0.0.0.0", "127.0.0.1", "localhost", "::1"):
        logger.warning(
            "Tunnel is on but FQDN is %r, so generated links still point at this "
            "machine. Set FQDN to your tunnel hostname, HAS_SSL=True and "
            "NO_PORT=True.", Server.FQDN,
        )
        return
    if not Server.HAS_SSL:
        logger.warning("Tunnel is on but HAS_SSL is False; links will use http://")
    if not Server.NO_PORT:
        logger.warning(
            "Tunnel is on but NO_PORT is False, so links include :%s. "
            "Cloudflare serves on 443, so set NO_PORT=True.", Server.PORT,
        )


# ------------------------------------------------------------------- supervisor

async def _pump(stream: asyncio.StreamReader) -> None:
    """Forward cloudflared's output into our logger, token redacted."""
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            continue  # absurdly long line; skip it rather than die
        if not raw:
            return
        line = _redact(raw.decode(errors="replace").rstrip())
        if not line:
            continue
        lowered = line.lower()
        if "registered tunnel connection" in lowered:
            logger.info("Cloudflare tunnel connection registered")
        elif "err" in lowered or "fail" in lowered or "unauthorized" in lowered:
            logger.warning("cloudflared: %s", line)
        else:
            logger.debug("cloudflared: %s", line)


def _command(binary: Path) -> List[str]:
    argv = [
        str(binary),
        "tunnel",
        "--no-autoupdate",
        "--loglevel", Tunnel.LOGLEVEL,
    ]
    if Tunnel.PROTOCOL and Tunnel.PROTOCOL != "auto":
        argv += ["--protocol", Tunnel.PROTOCOL]
    argv += Tunnel.EXTRA_ARGS
    argv += ["run", "--token", Tunnel.TOKEN]
    return argv


async def _supervise(binary: Path) -> None:
    """
    Keep cloudflared alive for as long as the bot is running.

    cloudflared reconnects on its own, but it does exit on things like a revoked
    token or a failed update, and a dead tunnel means every link 502s. Restart
    with a backoff so a permanently bad token doesn't become a hot loop.
    """
    global _process
    delay = Tunnel.RESTART_DELAY

    while not _stopping:
        try:
            _process = await asyncio.create_subprocess_exec(
                *_command(binary),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            logger.error("Could not start cloudflared: %s", err)
            return

        logger.info("cloudflared started (pid %s)", _process.pid)
        started_at = asyncio.get_event_loop().time()

        if _process.stdout is not None:
            await _pump(_process.stdout)
        code = await _process.wait()
        _process = None

        if _stopping:
            logger.info("cloudflared stopped")
            return

        # A run that lasted a while was healthy; reset the backoff.
        if asyncio.get_event_loop().time() - started_at > 60:
            delay = Tunnel.RESTART_DELAY

        logger.warning("cloudflared exited (code %s), restarting in %ss", code, delay)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        delay = min(delay * 2, Tunnel.MAX_RESTART_DELAY)


# ----------------------------------------------------------------- public API

async def start_tunnel() -> bool:
    """Bring the tunnel up. Returns True if cloudflared was launched."""
    global _supervisor, _stopping

    if not Tunnel.ENABLED:
        logger.debug("Tunnel disabled or no TUNNEL_TOKEN set; skipping")
        return False

    if _supervisor and not _supervisor.done():
        return True

    binary = await ensure_binary()
    if binary is None:
        logger.error("Tunnel requested but cloudflared is unavailable; continuing without it")
        return False

    _warn_about_url()

    tunnel_id = tunnel_id_from_token(Tunnel.TOKEN)
    if tunnel_id:
        logger.info("Starting Cloudflare tunnel %s", tunnel_id)
    else:
        logger.warning(
            "TUNNEL_TOKEN does not decode as a tunnel token. "
            "Copy the token from Zero Trust > Networks > Tunnels."
        )

    _stopping = False
    _supervisor = asyncio.create_task(_supervise(binary))
    return True


async def stop_tunnel() -> None:
    """Shut the tunnel down cleanly, then forcefully if it ignores us."""
    global _stopping, _supervisor, _process

    if _supervisor is None:
        return

    _stopping = True
    proc = _process

    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("cloudflared did not exit; killing it")
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    _supervisor.cancel()
    try:
        await _supervisor
    except (asyncio.CancelledError, Exception):
        pass

    _supervisor = None
    _process = None


def is_running() -> bool:
    return _process is not None and _process.returncode is None
