# Gallery Image Relay

[![CI](https://github.com/mytrashcan/gallery-image-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/mytrashcan/gallery-image-relay/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: GPL](https://img.shields.io/badge/License-GPL-blue.svg)](LICENSE)

**Gallery Image Relay** scrapes images from **DCInside** and **Arcalive** (arca.live), then relays them to **Discord**, **Telegram**, and an optional ephemeral web gallery. It is designed to run on cloud servers such as **Oracle Cloud** and **Amazon Web Services**.

> **No persistent image storage:** gallery images exist only in the web process's volatile **RAM**. Image bytes, titles, and URLs are never saved to disk or a database; they expire automatically and disappear immediately whenever the web process restarts.
>
> To prevent successful deliveries from being repeated after a crawler restart, the relay stores only minimal deduplication metadata in SQLite: namespaced post IDs, image SHA256 hashes, destination receipt IDs, and delivery timestamps. The ledger defaults to `var/delivery_archive.sqlite3` and can be moved with `ARCHIVE_PATH`; it never contains image bytes.

Two crawler types are supported:

- **DCInside** (`Module/crawler.py` + `Module/dcbot.py`) - waits behind a 20-post moderation window, prefers the original attachment, and relays one image per post to Discord and Telegram.
- **Arcalive** (`Module/arca_crawler.py` + `Module/arca_bot.py`) - waits behind a 10-image-post moderation window, accesses pages through a residential SOCKS proxy when configured, and relays up to four images per post to Discord.

Both crawlers share the same image pipeline (`ImageHandler` for compression/dedup), delivery layer (`MessageSender`), and the optional ephemeral web gallery (`web_app`).

## Features

- Scrapes images from DCInside and automatically posts to Discord and Telegram
- Arcalive (arca.live) crawler using `cloudscraper`; when hosted on a cloud VM, a home-machine SOCKS proxy handles the Cloudflare managed challenge that flags datacenter IPs (see "Arcalive: Cloudflare bypass" below)
- Multi-image extraction, capped at four images per post (Arcalive) vs single-image extraction (DCInside, spam prevention)
- Discord multi-embed delivery for Arcalive (up to 10 images per message)
- Both crawlers share the same ephemeral web gallery, with a source filter (DCInside / Arcalive / per-gallery) in the UI
- Works seamlessly on cloud environments like Oracle Cloud
- Supports multiple image formats (JPG, PNG, GIF) with automatic compression
  - Compression runs once and is reused for both platforms when their size limits match
  - Automatic re-compress & retry on Discord 413 (file too large) responses
- Fast HTML parsing via `lxml` + `SoupStrainer` (falls back to `html.parser` if lxml is unavailable)
- Config-driven gallery management via `galleries.json` - no code changes needed to add new galleries
- Optional **RAM-only ephemeral web gallery** - serves collected images in a near real-time, Pinterest-style masonry feed (titles link to the source post) with no persistent image storage
  - Installable as a PWA, ships `sitemap.xml`/`robots.txt`/`security.txt`, with an optional Cloudflare Turnstile bot gate
- Duplicate image detection via SHA256 hashing
- Stable post-ID deduplication and strict source/CDN URL validation
- Moderation safety windows before newly published posts become eligible for crawling
- Multi-process architecture for concurrent gallery crawling
- Test suite (pytest) and lint (ruff) enforced by GitHub Actions CI

## Installation

### Prerequisites

- Python 3.11+ (CI tests against 3.11 and 3.12)
- **Discord Bot Token** (for Discord bot integration)
- **Telegram Bot Token** (for Telegram bot integration) - only needed for DCInside galleries; Arcalive galleries are Discord-only
- The Arcalive crawler requires `cloudscraper` (installed via `requirements.txt`)

### Steps to Install

1. Clone the repository:
   ```bash
   git clone https://github.com/mytrashcan/gallery-image-relay.git dcinsideImageCrawler
   ```

2. Navigate to the project directory:
   ```bash
   cd dcinsideImageCrawler
   ```

3. (Optional) Set up a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/MacOS
   ```

4. Install the required dependencies:
   ```bash
   pip install -r requirements.txt -c constraints.txt
   ```

5. Create a `.env` file in the project root. Explicit environment variables take precedence; the legacy `Module/.env` is only a fallback:
   ```env
   DISCORD_TOKEN=your_discord_bot_token
   TELEGRAM_TOKEN=your_telegram_bot_token
   TELEGRAM_CHANNEL=your_telegram_chat_id
   # Optional: Discord upload limit in MB (default 10 — the free-tier limit since Sept 2024).
   # Raise to 50 (boost level 2) or 100 (boost level 3) if your server is boosted.
   DISCORD_MAX_SIZE_MB=10
   ```

## Usage

### Run all galleries
```bash
python launcher.py
```
The launcher manages multiple crawling processes using a circular queue, cycling through galleries defined in `galleries.json`.

### Run a single gallery
```bash
python run_gallery.py <gallery_name>
```
Example:
```bash
python run_gallery.py stariload
```

### Run a single arcalive gallery
```bash
# Run a single arcalive gallery (same runner - dispatched by "type" in galleries.json)
python run_gallery.py arca_bluearchive

# With web gallery
WEB_GALLERY=1 python run_gallery.py arca_genshin
```

### Adding a new gallery

Edit `galleries.json` and add a new entry:
```json
{
    "my_dc_gallery": {
        "base_url": "https://gall.dcinside.com/mgallery/board/lists/?id=my_gallery_id",
        "channel_ids": ["123456789012345678"]
    },
    "my_arca_gallery": {
        "base_url": "https://arca.live/b/myboard",
        "channel_ids": ["123456789012345678"],
        "type": "arca"
    }
}
```
Set `"type": "arca"` for arca.live galleries. `run_gallery.py` (and therefore the launcher) auto-detects this and uses the Arcalive crawler instead of the DCInside one.

No code changes required - just restart the launcher.

## Web gallery (optional)

You can serve the collected images as a live, **ephemeral** web feed - a Pinterest-style masonry grid at `http://<host>:8000/` that updates roughly in real time (the page polls every 5 seconds while visible, backs off to 60 seconds after errors, and updates existing cards). Post titles link back to their original source posts.

**Images are stored only in volatile process memory (RAM), never on disk or in a database.** The web process owns a bounded in-memory store. Images disappear when they exceed the TTL, item limit, or byte limit, and every image disappears immediately when the web process restarts.

Crawler processes publish bytes to the web process through an authenticated localhost endpoint. `WEB_INGEST_TOKEN` must be shared through `.env`; `deploy_oci.sh` creates it automatically. The web process enforces `WEB_FEED_MAX_ITEMS`, `WEB_MEMORY_MAX_MB`, and `WEB_IMAGE_MAX_MB` before accepting data.

Thumbnails are generated into `BytesIO` and count toward the same memory budget. Image responses use `Cache-Control: no-store` so browsers and CDNs are instructed not to retain them.

The systemd units disable core dumps and swap for web and crawler processes; host crash collectors and hibernation require separate operator controls. Copies sent to Discord, Telegram, browsers, or other external systems remain outside this server's control.

> ⚠️ **`LimitCORE=0` alone does not stop piped core collectors.** When `/proc/sys/kernel/core_pattern` pipes crashes to a collector (Ubuntu's `apport`, or `systemd-coredump`), the kernel ignores the `RLIMIT_CORE` size limit - so a native crash (e.g. in Pillow's decoders) could still dump the whole store's image bytes to disk under `/var/crash` or `/var/lib/systemd/coredump`. On the server, check `cat /proc/sys/kernel/core_pattern`; if it pipes to apport run `sudo systemctl mask apport.service`, and for systemd-coredump set `Storage=none` + `ProcessSizeMax=0` in `/etc/systemd/coredump.conf`.

### All galleries on one page (with the launcher)

Run the launcher with `WEB_GALLERY=1` so every crawler also writes to the shared gallery, and start one web server alongside it:

```bash
# terminal 1 - start the web server first
python run_web_server.py

# terminal 2 - crawlers (all galleries in galleries.json), each feeding the gallery
WEB_GALLERY=1 python launcher.py
```

Open `http://localhost:8000/` - images from every gallery appear in one feed. This works for both DCInside and Arcalive galleries - the gallery filter in the web UI shows `arca_*` gallery names for images coming from arcalive sources.

### A single gallery + web in one process

For quick local testing of one gallery, this runs the bot and an embedded web server together:

```bash
python run_web_gallery.py <gallery_name>
```

### Endpoints

| Path | Description |
|------|-------------|
| `/` | Masonry gallery page |
| `/feed?limit=N` | JSON feed of recent items (`limit` 1-200, default 60) |
| `/images/{id}` | In-memory image response (`Cache-Control: no-store`) |
| `/healthz` | Memory usage, freshness, item count, and ingest readiness |

> The server binds to `127.0.0.1:8000` by default. `/internal/images` requires `WEB_INGEST_TOKEN`; never expose that token to browsers or commit it.

### Terminal monitor

A live terminal dashboard shows the web feed and crawler processes at a glance:

```bash
python dashboard.py          # refreshes every 2s (Ctrl+C to quit)
python dashboard.py --once   # print a single frame
python dashboard.py -i 1     # custom refresh interval (seconds)
```

It reads `/healthz` + `/feed` from the web server (`WEB_PORT`, default 8000) and scans running `launcher.py` / `run_gallery.py` processes via `psutil` - no extra wiring needed. Panels: service status (web up/down, feed size, TTL), per-gallery crawler status (PID / memory / uptime), and the most recent images.

**Monitoring from another machine** (e.g. checking a Mac deployment from your Mac, or an OCI deployment remotely) works for the service-status and feed panels via the public API:

```bash
DASH_BASE_URL=https://dcselfie.win python dashboard.py
```

The crawler-process panel (PID/memory/uptime) can't be shown this way - `psutil` only sees processes on the machine it runs on - so it's replaced with an SSH hint. To see crawler status, run `./dcselfie.sh status` or `python dashboard.py` directly on the server (e.g. over SSH).

## Arcalive: Cloudflare bypass for cloud-hosted crawlers

`cloudscraper` alone gets Arcalive's older JS challenge, but arca.live also serves a **Cloudflare managed challenge** to IPs with a datacenter reputation (Oracle Cloud, AWS, etc.) - a real browser is required to solve it, and headless Chromium on a cloud VM tends to get flagged too (tried and reverted; see git history for `nodriver`/Xvfb attempts). A residential IP (e.g. a home Mac) is not challenged at all.

The fix: route only the Arcalive crawler's requests through a home machine via a **reverse SOCKS proxy over SSH**, so arca.live sees a residential IP while DCInside and relay deliveries run directly on the cloud VM. Both Arcalive HTML and CDN image requests use the configured proxy; each concurrent image worker reuses its own Session.

```bash
# On the home machine (Mac): open a reverse dynamic (SOCKS) forward to the server.
# No destination after the port -> ssh itself acts as the SOCKS proxy and opens
# the real outbound connection *from this machine*.
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:1080 ubuntu@<server-ip>
```

The private `com.dcselfie.arca-tunnel.plist` is not shipped in this repository. Use a local LaunchAgent or autossh supervisor for the command above; never put private hostnames, SSH keys, or proxy credentials in tracked files. See [deployment guidance](docs/deployment.md).

On the server, point the crawler at the tunnel and restart:
```bash
echo 'ARCA_SOCKS_PROXY=socks5h://localhost:1080' >> .env
sudo systemctl restart dcselfie-launcher
```
Verify the egress IP actually changed: `curl -x socks5h://localhost:1080 https://ifconfig.co` should print the home machine's IP, not the server's.

On the **server**, set `ClientAliveInterval 30` / `ClientAliveCountMax 3` in `/etc/ssh/sshd_config` (then `sudo systemctl reload ssh`). Without this, if the home machine's IP changes (Wi-Fi switch, sleep/wake, ISP DHCP renewal) before it can cleanly close the old SSH session, the server can leave the dead session's reverse-forwarded port 1080 open indefinitely - blocking the new tunnel from rebinding it. With `ClientAliveInterval` set, the server detects and drops dead sessions within ~90s on its own, so a Wi-Fi change never requires manual cleanup.

> ⚠️ **Don't combine `-D` with `-R` for this** (e.g. `-R 1080:localhost:1080` on top of `-D 1080`) - that routes the "reverse" connection back into the `-D` proxy on the *same* machine, which loops the traffic back out through the server instead of the home machine, silently defeating the whole point.
>
> ⚠️ **Never hardcode `ARCA_SOCKS_PROXY` (or any proxy credentials) directly in a git-tracked file** like a `.service` unit - this repo is public, and a proxy password committed to a tracked file stays in git history forever even after removal. Keep it in the server's `.env` only (gitignored); `launcher.py` already loads it via `python-dotenv`, no systemd `Environment=` line needed.

## Deploying the web gallery

Two common setups:

- **On your own machine (e.g. a Mac left running)** - run the crawlers + web server locally and expose the domain with a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (`cloudflared`). No port-forwarding, no public IP, automatic HTTPS. Keep the machine awake (`caffeinate -s`, or an app like Amphetamine). On macOS, `./dcselfie.sh install` registers the crawler / web server / tunnel as background **launchd** services (hidden, auto-restart, start at login); `./dcselfie.sh {start|stop|restart|status|logs|dash|uninstall}` manages them, and `./dcselfie.sh dash` opens the terminal monitor on demand.
- **On a cloud VM (e.g. Oracle Cloud free tier)** - run everything as `systemd` services and front it with a reverse proxy (Caddy gives automatic HTTPS). See the systemd example below; bind the web server to `127.0.0.1` and let the proxy serve the domain on 443. On Oracle Cloud remember to open ports 80/443 in **both** the VCN security list **and** the instance's local `iptables`.

## Running on a server (e.g. Oracle Cloud)

Run the launcher and web server as systemd services so they survive reboots and SSH disconnects. This repo ships the actual unit files used in production (`dcselfie.win`) as a reference:

- [`dcselfie-launcher.service`](dcselfie-launcher.service) - crawlers (`WEB_GALLERY=1 launcher.py`)
- [`dcselfie-web.service`](dcselfie-web.service) - web gallery, bound to `127.0.0.1:8000` (put a reverse proxy like Caddy in front for the public domain + HTTPS)

```bash
sudo cp dcselfie-launcher.service dcselfie-web.service /etc/systemd/system/
# edit WorkingDirectory/User inside if your paths differ, then:
sudo systemctl daemon-reload
sudo systemctl enable --now dcselfie-launcher dcselfie-web
```

Subsequent OCI deployments use `./deploy_oci.sh` from a clean checkout. It verifies a fast-forward update, stages a separate constrained virtual environment, checks imports/configuration, then stops workers before replacing code. Failed readiness checks restore the previous commit, venv and existing systemd units. Rollback environments are retained in `.deploy/`; see [deployment and recovery](docs/deployment.md) before first use.

Both services load the root `.env` through the same Python configuration loader. The launcher wants the web service and waits for its authenticated ingest endpoint before starting crawlers; a web restart does not stop the launcher. After changing the unit files, run `sudo systemctl daemon-reload`. For a manual restart, use `sudo systemctl restart dcselfie-web && sudo systemctl restart dcselfie-launcher`.

Secrets (`DISCORD_TOKEN`, `ARCA_SOCKS_PROXY`, etc.) go in the project's `.env`, never in the unit files - see the warning in "Arcalive: Cloudflare bypass" above.

Notes for small instances (1 GB RAM free tier):
- The launcher has independent DCInside and Arcalive budgets (up to 5 each), so it can run 10 Python processes in total. Lower `MAX_DC_CRAWLERS` and `MAX_ARCA_CRAWLERS` if memory is tight; each process loads discord.py and Pillow.
- Make sure `lxml` installed successfully (`pip show lxml`) — it noticeably reduces CPU time per crawl cycle.

## Project Structure

```
<project-root>/
├── launcher.py            # Process manager - runs multiple gallery crawlers
├── run_gallery.py         # Single gallery runner (DCInside/Arcalive, dispatched by "type")
├── run_web_gallery.py     # Single gallery runner + embedded web gallery (FastAPI)
├── run_web_server.py      # Standalone web gallery server (for the multi-gallery launcher setup)
├── dashboard.py           # Live terminal monitor (web feed + crawler processes)
├── web_app.py             # FastAPI app & bounded RAM-only ephemeral feed (TTL/eviction)
├── web_static/            # Gallery page and versioned static assets
├── galleries.json         # Gallery configuration (URLs, channel IDs)
├── requirements.txt       # Runtime dependencies
├── requirements-dev.txt   # Dev dependencies (pytest, ruff)
├── pyproject.toml         # pytest & ruff configuration
├── .github/workflows/ci.yml  # CI: lint + tests on Python 3.11/3.12
├── dcselfie-launcher.service  # systemd unit: crawlers (see "Running on a server")
├── dcselfie-web.service       # systemd unit: web gallery
├── dcselfie.sh             # macOS launchd management CLI (install/start/stop/status/dash)
├── Module/
│   ├── config.py          # Environment variables, headers, logging setup
│   ├── crawler.py         # DCInside page scraping
│   ├── dcbot.py           # Discord bot client & crawling orchestration
│   ├── arca_crawler.py    # Arcalive page scraping
│   ├── arca_bot.py        # Arcalive Discord bot client
│   ├── image_handler.py   # Image downloading, deduplication, compression
│   ├── message_sender.py  # Discord & Telegram message delivery
│   ├── embeds.py          # Shared Discord image-embed builder
│   └── lru_cache.py       # Shared bounded-cache used for dedup across crawlers
└── tests/                 # pytest test suite
```

## Key Configuration

| Setting | Location | Default | Description |
|---------|----------|---------|-------------|
| `DISCORD_MAX_SIZE_MB` | `.env` | 10 MB | Discord upload limit (raise for boosted servers) |
| `MAX_DC_CRAWLERS` | env | 5 (hard max 5) | Independent DCInside crawler concurrency budget |
| `MAX_ARCA_CRAWLERS` | env | 5 (hard max 5) | Independent Arcalive crawler concurrency budget |
| `CRAWLER_BATCH_SECONDS` | env | 3600s | Rotation interval when a platform has more galleries than its limit |
| `REQUEST_TIMEOUT` | `Module/config.py` | 15s | HTTP request timeout |
| Crawl interval | `Module/dcbot.py` | 20-40s | Random delay between crawls |
| Arca crawl interval | `Module/arca_bot.py` | 30-60s | Random delay between arcalive crawls |
| `WEB_GALLERY` | env | unset | Set to `1` so `launcher.py`/`run_gallery.py` crawlers also feed the web gallery |
| `WEB_HOST` | env | `127.0.0.1` | Web gallery bind address (set `0.0.0.0` to expose directly, without a reverse proxy) |
| `WEB_PORT` | env | 8000 | Web gallery port |
| `WEB_IMAGE_TTL_SECONDS` | env | 10800 (3h) | How long an image stays in memory |
| `WEB_FEED_MAX_ITEMS` | env | 120 | Maximum in-memory feed items |
| `WEB_MEMORY_MAX_MB` | env | 256 | Hard cap for original and thumbnail bytes |
| `WEB_IMAGE_MAX_MB` | env | 12 | Per-image RAM cap; originals within the cap stay untouched |
| `WEB_INGEST_MAX_MB` | env | 12 | Maximum localhost upload body |
| `WEB_UPLOAD_QUEUE_MAX_MB` | env | 32 | Per-crawler original-byte cap, including the in-flight web request |
| `WEB_UPLOAD_QUEUE_SIZE` | env | 20 | Per-crawler bounded background queue for web uploads |
| `WEB_FRESHNESS_SECONDS` | env | 900 | Age threshold reported by `/healthz` |
| `WEB_INGEST_TOKEN` | `.env` | required | Shared secret for crawler-to-web ingestion |
| `WEB_GALLERY_URL` | env | `http://127.0.0.1:8000` | Internal web-gallery origin |
| `WEB_STATIC_DIR` | env | `web_static` | Directory for HTML/CSS/static assets only |
| `WEB_THUMB_WIDTH` | env | 480 | In-memory card-thumbnail width (`0` disables) |
| `WEB_MAINTENANCE` | env | unset | Set to `1` to force the maintenance page (`503`). A `.maintenance` flag file next to the project (toggled by `./dcselfie.sh down` / `up`, no restart needed) has the same effect |
| `ARCA_SOCKS_PROXY` | `.env` (never commit) | unset | `socks5://...` proxy the Arcalive crawler routes through - see "Arcalive Cloudflare bypass" below |
| `ARCA_DOWNLOAD_CONCURRENCY` | env | 2 | Bounded concurrent Arcalive CDN downloads per crawler |
| `MEDIA_DOWNLOAD_MAX_MB` | env | 15 | Hard streaming limit for each source image download |
| `MEDIA_MAX_FRAMES` | env | 200 | Maximum frames accepted from an animated source image |
| `MEDIA_MAX_ANIMATION_PIXELS` | env | 60000000 | Sum of decoded canvas pixels across animation frames |
| `MEDIA_MAX_PIXELS` | env | 24000000 | Pixel-count limit checked before image processing |
| `TURNSTILE_SITEKEY` / `TURNSTILE_SECRET` | `.env` (never commit) | unset | Cloudflare Turnstile bot gate for the web gallery; unset disables it entirely |
| `WEB_ORIGIN_SECRET` | `.env` (never commit) | unset | When set, a request carrying `cf-connecting-ip` must also carry the matching `x-origin-secret` header or the header is ignored (socket peer IP used instead). Set the same value in a Cloudflare Transform Rule that injects `x-origin-secret`. For stronger protection, also enable Cloudflare Authenticated Origin Pulls (mTLS) on the zone; unset always uses the socket peer; untrusted proxy headers are ignored |

## Development

```bash
pip install -r requirements-dev.txt -c constraints.txt

# Run tests
pytest

# Lint and offline import/configuration smoke check
ruff check .
python scripts/check_install.py
```

CI (GitHub Actions) runs ruff and the test suite on every push to `main` and on pull requests.

## Delivery, privacy and operational limits

A post/hash is acknowledged only after **every configured Discord channel and (for DCInside) Telegram** succeeds. Each successful destination/media pair is committed to SQLite immediately after the transport returns; retries and crawler restarts skip those receipts. Web delivery stays best effort: its bounded RAM queue can drop images during prolonged outage and does not control post acknowledgement. Existing post/hash rows remain compatible. Destination receipts added by this release cannot reconstruct failures already acknowledged by older versions.

This is not exactly-once delivery: a remote service can accept a message just before a timeout or process crash, before SQLite can record it. That uncertain interval can produce a duplicate. Never delete the archive to fix a lock or corruption error. Keep SQLite on a local filesystem, back it up with SQLite's backup API, and retain it across deployments. `DeliveryArchive.prune(before)` is an explicit retention operation; pruning allows old content to be delivered again and is not run automatically.

Crawlers select eligible posts oldest-first **within the current first list page**. DC skips the newest 20 normal posts; Arcalive skips the newest 10 image posts. These are delay heuristics, not content moderation. There is no historical pagination/backfill guarantee: busy boards, downtime, permanently failing oldest posts, and hourly rotation when there are more than five galleries per source can cause missed posts. See the review's recommended bounded catch-up work before relying on lossless capture.

Turnstile, when configured, protects `/feed`, `/images/*` and `/like/*` regardless of proxy headers. Configure both keys. `/healthz` remains accessible and its `fresh` field is an activity indicator, not proof that every destination is healthy. The terminal dashboard's recent-feed panel cannot pass Turnstile; health/process panels still work. A matching `WEB_ORIGIN_SECRET` is required to trust Cloudflare client IPs. Use the [Cloudflare ingress example](docs/cloudflared.example.yml) to keep `/internal/*` off the public tunnel, plus edge request/connection limits.

Application image buffers never use temporary files. Root application logging redacts HTTP/SOCKS URLs, configured tokens and transport exception details; post titles and filenames have been removed from crawler logs. Public request access logs and optional honeypot diagnostics are separate metadata. Honeypot events default to a bounded RAM ring (1,000 events, 100/minute); `HONEYPOT_LOG_PATH` explicitly enables a rotating file (4 MiB plus one backup) containing bounded IP/UA/path metadata. Set OS journal/log rotation and avoid HTTP debug logging. This application cannot erase copies retained by external platforms, browsers, crash collectors, swap, hibernation or infrastructure logs.

Default web capacity counts original and thumbnail byte lengths (256 MiB), not total process RSS. Two active ingest requests and four verification requests are admitted; overload returns 503 and body reads time out after 10 seconds. The TTL sweeper runs every second and clears RAM on shutdown. Source HTML is capped at 4 MiB, media at 15 MiB, and redirects are revalidated before each fetch. Provision memory for decoded images, queues and the Python runtime as well as the store. Arcalive concurrency is validated in the range 1–4.

See [production review and change rationale](docs/production-hardening.md) and [deployment/recovery](docs/deployment.md).

## Discord Commands

| Command | Description |
|---------|-------------|
| `!쓰담쓰담` | Clear the process-local image hash cache; durable receipts remain |

## License
This project is licensed under the GPL License.
