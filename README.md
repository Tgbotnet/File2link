<h1 align="center">My File 2 Link</h1>

<p align="center">
  <img src="logo.png" alt="My File 2 Link" width="180">
</p>

<p align="center">
  <b>Turn any Telegram file into a permanent direct download and streaming link.</b>
</p>

<p align="center">
  <a href="https://t.me/My_file_2_link_bot"><img src="https://img.shields.io/badge/Try%20the%20bot-@My__file__2__link__bot-2783DE?style=for-the-badge&logo=telegram&logoColor=white"></a>
  <img src="https://img.shields.io/badge/python-3.11-blue?style=for-the-badge&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge">
</p>

<p align="center">Built and maintained by <b>Mr.Aman</b></p>

---

## What it does

Send any file to the bot and get back an instant HTTP link. Nothing is copied to the
server's disk — bytes are streamed straight out of Telegram's data centres on demand.

- **Direct download links** that work in any browser, IDM or `wget`
- **In-browser streaming** for video, with a clean player page
- **Range requests** supported end to end, so seeking and resuming both work
- **Channel mode** — add the bot as an admin and it attaches a link button to every file posted
- **Your files list** — browse, re-share or revoke any link you've made with `/files`
- **Admin dashboard** at `/admin` with bandwidth stats, top users, reports, and ban tools
- **Multi-client support** — add extra bot tokens to spread the download load

---

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | Start the bot and show the main menu |
| `/files` | everyone | Browse and manage your generated links |
| `/help` | everyone | Usage instructions |
| `/about` | everyone | Bot and version info |
| `/status` | owner | Uptime, user count and bandwidth |
| `/ban` / `/unban` | owner | Block or unblock a user |
| `/broadcast` | owner | Reply to a message to send it to every user |
| `/del` | owner | Delete a stored file and revoke its link |

---

## How it works

1. A user sends a file to the bot.
2. The bot forwards it to a private log channel and stores the MTProto file ID in MongoDB.
3. The bot replies with a short link.
4. When someone opens that link, `aiohttp` streams the file back directly from Telegram,
   honouring the HTTP `Range` header.

```
Telegram user  ->  Bot  ->  Log channel + MongoDB
                              |
     Browser  <-  aiohttp  <--+   (parallel, range-aware streaming)
```

### Parallel download engine

Telegram caps `upload.GetFile` at **1 MB per call**, so the ceiling on a naive
downloader is one round trip per megabyte — latency-bound, not bandwidth-bound.
Extra bot tokens don't help here either: they spread *different users* across
clients, but a single download still crawls along one connection.

This build fixes that at the chunk level:

- **Up to 40 chunks in flight per download.** A sliding window keeps the pipe full —
  as soon as the browser consumes chunk *N*, chunk *N + 40* is requested.
- **A pool of 8 media connections per data centre.** One MTProto connection
  serialises its own request queue, so the parallel requests are round-robined
  across real separate connections.
- **Strict in-order reassembly.** Chunks come back out of order and are buffered
  into the correct sequence before being written to the response.
- **Auth key reuse.** The pool opens once and shares a single exported
  authorization, so it doesn't trip Telegram's `ExportAuthorization` rate limit.
- **Automatic fallback.** Requests under `PARALLEL_MIN_MB` (player seeks, metadata
  probes) stay single-shot, where fan-out would only add overhead.
- **Backpressure.** A process-wide semaphore caps total in-flight chunks so heavy
  concurrent traffic can't exhaust RAM.

Range requests, seeking and resume all still work exactly as before.

---

## Requirements

- Python 3.11+
- A MongoDB database (Atlas free tier is fine)
- Telegram `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org)
- A bot token from [@BotFather](https://t.me/BotFather)
- Two private channels (one for file storage, one for user logs) with the bot as admin

---

## Configuration

Create a `.env` file in the project root.

### Required

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Token from @BotFather |
| `OWNER_ID` | Your numeric Telegram user ID |
| `DATABASE_URL` | MongoDB connection string |
| `FLOG_CHANNEL` | Private channel ID for file storage |
| `ULOG_CHANNEL` | Private channel ID for user logs |

### Optional

| Variable | Default | Description |
|---|---|---|
| `FQDN` | bind address | Public domain of your deployment |
| `HAS_SSL` | `False` | Set `True` if served over HTTPS |
| `NO_PORT` | `False` | Set `True` to omit the port from links |
| `PORT` | `8080` | Web server port |
| `BOT_USERNAME` | `My_file_2_link_bot` | Bot username, no `@` |
| `OWNER_USERNAME` | `My_file_2_link_bot` | Username behind the Contact button |
| `UPDATES_CHANNEL` | `My_file_2_link_bot` | Public updates channel, no `@` |
| `FORCE_SUB` | `False` | Require joining a channel first |
| `FORCE_SUB_ID` | — | Channel to require when `FORCE_SUB` is on |
| `START_PIC` | bundled banner | Override the `/start` image |
| `FILE_PIC` | bundled banner | Override the My Files image |
| `VERIFY_PIC` | bundled banner | Override the verification image |
| `ADMIN_USERNAME` | — | `/admin` login. Blank keeps the dashboard **disabled** |
| `ADMIN_PASSWORD` | — | `/admin` password. Blank keeps the dashboard **disabled** |
| `AUTH_USERS` | — | Space-separated IDs for private mode |
| `WORKERS` | `6` | Pyrogram worker threads |
| `MULTI_TOKEN1`…`40` | — | Extra bot tokens for load balancing |

### Download speed tuning

| Variable | Default | Description |
|---|---|---|
| `MAX_PARALLEL_PARTS` | `40` | Chunks of a **single** download fetched at once (max 40) |
| `SESSIONS_PER_DC` | `12` | MTProto connections per client, per data centre (max 16) |
| `PARALLEL_MIN_MB` | `1` | Downloads below this size stay single-shot |
| `MAX_TOTAL_PARTS` | `240` | Process-wide in-flight chunk cap ≈ peak streaming RAM in MB |
| `HEDGE_AFTER_MS` | `2500` | Re-request a chunk on a second connection if it's slower than this; `0` disables |
| `MAX_INFLIGHT_PER_SESSION` | `6` | Max requests queued on any one connection |

### Why the speed used to fluctuate

Chunks must reach the client **in order**, so a single slow chunk freezes the
whole stream even when forty later parts are already downloaded and waiting in
memory. When the straggler finally lands, everything behind it flushes at once.
That is the drop-then-spike pattern — the average was fine, the floor was not.

Three things now hold the speed flat:

- **Hedged requests.** A chunk that hasn't come back within `HEDGE_AFTER_MS` is
  requested again on a different connection, and whichever reply arrives first
  wins. Costs at most one wasted megabyte; removes the stall.
- **Least-loaded connection picking.** Round-robin looks fair but isn't — a
  socket that hit a slow chunk kept receiving new work at the same rate, so its
  queue grew and everything behind it arrived late. Requests now go to whichever
  connection has the fewest outstanding.
- **Flood waits no longer block other downloads.** A throttled chunk used to
  hold one of the global in-flight slots for the entire wait.

### File naming

| Variable | Default | Description |
|---|---|---|
| `NAME_FROM_CAPTION` | `True` | Use the message caption as the file name when present |
| `CAPTION_MAX_WORDS` | `13` | Keep only the first N words of the caption |
| `CAPTION_MAX_CHARS` | `120` | Hard character cap on the generated name |

Uploads usually carry the real title in the caption while the document itself is
called something like `video_2026-07-25_13-47-11.mp4`. When a caption exists it
becomes the file name, trimmed to 13 words, with illegal characters stripped and
newlines collapsed.

> The original extension is always re-attached — captions rarely include one, and
> without it players and download managers can't tell what the file is. Files with
> no caption keep the existing naming behaviour exactly as before.

### Cloudflare tunnel

Run the bot from home with no public IP, no port forwarding and no reverse
proxy. Put a Zero Trust tunnel token in `.env` and the bot starts and supervises
`cloudflared` itself every time it boots.

| Variable | Default | Description |
|---|---|---|
| `TUNNEL_TOKEN` | — | Token from **Zero Trust → Networks → Tunnels**. Setting it enables the tunnel |
| `TUNNEL_ENABLED` | `True` | Set `False` to keep the token but leave the tunnel off |
| `TUNNEL_AUTO_INSTALL` | `True` | Download `cloudflared` automatically when it isn't installed |
| `CLOUDFLARED_PATH` | — | Path to an existing binary; blank searches `$PATH` |
| `TUNNEL_INSTALL_DIR` | `bin` | Where an auto-downloaded `cloudflared` is kept |
| `TUNNEL_PROTOCOL` | `auto` | `auto`, `quic` or `http2` — use `http2` if UDP/7844 is blocked |
| `TUNNEL_LOGLEVEL` | `info` | cloudflared log verbosity |
| `TUNNEL_EXTRA_ARGS` | — | Extra cloudflared flags, space separated |
| `TUNNEL_RESTART_DELAY` | `5` | Seconds before restarting a crashed tunnel |
| `TUNNEL_MAX_RESTART_DELAY` | `300` | Upper bound on the restart backoff |

**Setup, in order:**

1. In **Zero Trust → Networks → Tunnels**, create a tunnel and copy the token.
2. Add a **public hostname** to that tunnel, e.g. `files.example.com`, with the
   service set to `http://localhost:8080` (match your `PORT`).
3. Put the token in `.env` as `TUNNEL_TOKEN`.
4. Set `FQDN=files.example.com`, `HAS_SSL=True`, `NO_PORT=True`.
5. Start the bot. It prints `Starting Cloudflare Tunnel` and logs the tunnel ID.

> **Step 4 is the one people miss.** The tunnel and the link URLs are configured
> independently: cloudflared decides how traffic reaches the bot, while `FQDN`
> decides what the bot writes into the links it hands out. Get the tunnel right
> but leave `FQDN` unset and every link still points at `localhost:8080`. The bot
> logs a warning at startup if it spots this.

The routing between hostname and bot lives in the Cloudflare dashboard, not in
this repo — a token-based tunnel is remotely managed, so there is no local
`config.yml` to edit.

If cloudflared exits (revoked token, network loss), the bot restarts it with an
exponential backoff up to `TUNNEL_MAX_RESTART_DELAY`, and shuts it down cleanly
when the bot stops. The token is redacted from all log output.

### Link lifetime

| Variable | Default | Description |
|---|---|---|
| `LINK_EXPIRY_HOURS` | `24` | Hours a link stays alive. `0` disables expiry entirely |
| `CLEANUP_INTERVAL` | `300` | Seconds between sweeps for dead links |
| `SWEEP_BATCH` | `200` | Maximum links retired in one sweep |
| `DELETE_FROM_LOG` | `True` | Also wipe the stored copy from the log channel |
| `DELETE_USER_CHAT` | `True` | Also wipe the upload + link message from the user's chat |

### Batch links

| Variable | Default | Description |
|---|---|---|
| `BATCH_LINKS` | `True` | Bundle a burst of uploads into one copy-once message |
| `BATCH_WINDOW` | `6` | Seconds of silence after the last upload before summarising |
| `BATCH_MIN_FILES` | `2` | Don't summarise fewer files than this |
| `BATCH_MAX_FILES` | `100` | Upper bound on links per summary, and on `/links` |

> The admin dashboard has **no default credentials**. It stays switched off until you
> set both `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

---

## Link lifetime & revoking

Every link a user generates exists in **three** places at once:

1. the MongoDB record that makes the public `/watch` and `/dl` URLs resolve,
2. the copy parked in your private log channel (`FLOG_CHANNEL`),
3. the upload and the link message in the user's private chat with the bot.

Deleting only the database row leaves the file fully recoverable, so every path
that kills a link routes through a single purge that clears all three together.

**Revoking.** When a user taps *Revoke*, the link dies immediately, the stored
copy is deleted from the log channel, and the upload and link message are
removed from their chat. The same purge backs the owner's `/del` command and the
admin dashboard's takedown button.

**Expiry.** Links are valid for **24 hours** by default. A background janitor
sweeps every `CLEANUP_INTERVAL` seconds and purges anything past its deadline,
exactly as if it had been revoked by hand. Because the sweep runs on an interval,
`/watch` and `/dl` also check the deadline on every request, so an expired link
stops working the moment it lapses rather than whenever the next sweep lands.
Expired URLs return **HTTP 410 Gone**.

Only the file's owner (or the bot owner) can revoke a link. Deletions are
best-effort by design: if a message was already removed, the bot lost admin
rights in the log channel, or the user blocked the bot, the link is still killed.

Records created before this feature existed are dated by their upload time, so
they expire normally instead of living forever. Re-uploading a file you already
shared restarts its 24-hour window rather than handing back a link about to die.

Set `LINK_EXPIRY_HOURS=0` to switch expiry off and keep the old behaviour;
revoking still purges everything.

---

## Batch links (grabbing many files at once)

Send ten files and you get ten replies — fine for one file, tedious when you're
feeding a download manager. So once you stop uploading, the bot sends **one extra
summary message** containing every link from that burst in a single code block.
Tap it once and Telegram copies the lot; in **1DM+** use *+ → paste links*, and
all ten queue up at once.

The summary waits for `BATCH_WINDOW` seconds of silence, and each new file pushes
that deadline back, so it only lands when you're actually done. Your normal
per-file replies still arrive as before — they carry the stream and revoke
buttons. This is purely additive.

If the list is too long for one Telegram message, the bot sends a **`links.txt`**
instead: one bare URL per line, no comments or blank lines, which is exactly what
1DM+ and IDM expect from *import links from file*.

**`/links [n]`** re-sends your newest `n` links (default 10) in the same format,
for when you've closed the chat or want an older batch back.

> The summary is plain text and holds no file data, so it isn't deleted when a
> link is revoked or expires — the URLs inside it simply stop working.

### A note on connection counts

1DM+ opens several connections **per file**, and the server then fans each of
those out to Telegram. Ten files × 16 connections × parallel chunks multiplies
fast, which is why `MAX_TOTAL_PARTS` exists as a process-wide ceiling. If
downloads start stalling, lower 1DM+'s per-file connection count (8–16 is plenty)
rather than raising it — past a point you're just queueing against your own
server's uplink.

---

## Running it

### Locally

```bash
git clone <your-repo-url> MyFile2LinkBot
cd MyFile2LinkBot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
python3 -m FileStream
```

### Docker

```bash
docker build -t myfile2link .
docker run -d --env-file .env -p 8080:8080 --name myfile2link myfile2link
```

### Hosted platforms

`Procfile` and `app.json` are included, so Heroku, Railway, Render and Koyeb all work.
Set `FQDN` to the domain the platform gives you, and set `HAS_SSL=True` and `NO_PORT=True`.

---

## Branding

All interface art lives in the repo and can be swapped without touching code:

- `FileStream/assets/start.jpg` — the image replied to `/start`
- `FileStream/assets/files.jpg` — the My Files header
- `FileStream/assets/verify.jpg` — the verification prompt
- `FileStream/template/base.css` — shared theme for every web page
- `FileStream/template/play.html`, `dl.html`, `dmca.html`, `admin.html` — the web UI
- `logo.png`, `logo.ico` — app mark and favicon

---

## Legal

This bot only relays files its users choose to upload. Uploading pirated, adult or
otherwise illegal material is prohibited. Every public page carries a **Report** button,
and reported files can be removed from the `/admin` dashboard or with `/del`. You are
responsible for the content served by your own deployment.

---

## Credits

The chunked Telegram download routine in `FileStream/utils/custom_dl.py` is adapted from
[eyaadh/megadlbot_oss](https://github.com/eyaadh/megadlbot_oss), used under its original
licence. Everything else in this build — branding, web UI, bot interface and
configuration — is by **Mr.Aman**.

<p align="center"><b>&copy; 2026 Mr.Aman &middot; <a href="https://t.me/My_file_2_link_bot">@My_file_2_link_bot</a></b></p>
