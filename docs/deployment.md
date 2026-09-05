# Deployment and recovery

## Installation contract

Use Python 3.11 or 3.12 and a local filesystem for the SQLite WAL archive. Install with `python -m pip install -r requirements.txt -c constraints.txt`. The constraints snapshot records versions exercised in this change; platform-only packages can still be resolved separately. Refresh constraints deliberately and rerun the matrix after dependency upgrades. Python 3.13+ has not been validated.

All runners load environment variables, then the project-root `.env`, then legacy `Module/.env` as fallback. Environment values win. Archive/static defaults are anchored to the project root; explicitly configured relative paths resolve against the process working directory. Use absolute values for custom deployments. Both systemd and launchd set the project working directory. Token values never belong in unit files, shell command lines or source control. Run `python scripts/ensure_web_ingest_token.py .env` and restrict the file to the service user (`chmod 600 .env`).

The supplied Linux units and OCI deployment guard target `/home/ubuntu/dcinsideImageCrawler`, user `ubuntu`. Adapt **both units and the deployment path guard** for another installation. Create `venv` before the first service installation. Run `python scripts/check_install.py`, install the units, `systemctl daemon-reload`, then enable/start web and launcher. Inspect `/healthz` and authenticate an empty ingest using `python scripts/probe_web_ingest.py .env` (415 means authentication passed). Never put real image data on disk for a smoke test.

## Linux lifecycle and logs

The launcher wants/starts web and checks authenticated ingest readiness before spawning workers. Web restarts do not stop the launcher; bounded queues/retries absorb brief interruptions and drop web-only images during longer outages. SIGTERM cancels bot work, joins outstanding blocking operations, closes sessions and SQLite, and then disconnects Discord. Launcher shutdown gives children a shared 90-second grace period, then kills and reaps stragglers. Units allow 110 seconds and use `KillMode=mixed` so the parent coordinates children before the cgroup kill deadline. A hard kill still loses RAM images and may leave uncertain remote deliveries.

Web has a 768 MiB cgroup ceiling; the launcher subtree has a 4 GiB ceiling. These are upper limits, not reservations. Tune crawler counts, web byte limits and cgroup limits to the host. Disable swap for both units; account for the runtime, native decode buffers, per-crawler upload queues and both Discord/Telegram buffers beyond the web-store byte counter. Disable host crash collectors and hibernation when strict RAM-only handling is required; per-process `LimitCORE=0` does not cover every host collector. Do not run multiple uvicorn workers: each would own a separate store and rate buckets.

Both units write to the system journal. Set host journald retention/size limits, and inspect `journalctl -u dcselfie-web -u dcselfie-launcher`. Existing `logs/launcher.log` from older units is not deleted automatically. Application filters remove source URLs, filenames and credential-bearing exceptions; avoid enabling transport debug/access logging outside the supplied filter paths. IP/path access metadata and external infrastructure logging are not an image-byte storage guarantee.

## Update and rollback

`deploy_oci.sh` refuses dirty/untracked worktrees and non-fast-forward main updates. A project-local `flock` serializes deployments. Before stopping anything it creates a detached source worktree and a separate venv under `.deploy/`, installs constrained dependencies, compiles/imports code and checks gallery configuration. It then stops launcher and web, fast-forwards code, keeps the old venv and unit files, selects the new venv, starts web, checks authenticated ingest, and starts/checks crawler children.

An error after stopping services triggers restoration of the previous commit, venv and backed-up units, daemon reload and service start. Read the failure output and verify the restored services manually: rollback itself can fail if disk, permissions or systemd are broken. The script never removes or rolls back `.env` or the SQLite ledger. Its schema change is additive, so the original application can still read the ledger. All RAM gallery contents disappear on restart. A deployment lock does not prevent an editor from modifying files: do not edit the checkout during deployment. The rollback reset is only intended for the clean checkout accepted at the start.

The old venv, source commit identifier (printed), and unit copies remain under `.deploy/backup-*`. Retain the last known-good set until monitoring confirms the release. Manual recovery: stop both services, select the known-good clean commit, restore the corresponding venv and units, reload systemd, then start/probe web before starting launcher. Never point `venv` at a temporary directory that will be removed. Delete only positively identified inactive `.deploy/` release environments after verifying no service uses them; cleanup is deliberately not automatic.

Local validation covers shell syntax and code/configuration behavior, not a live systemd deployment. Stage a failed ingest/readiness scenario on a test VM before first production use. Sudden power loss during the multi-step promotion is not automatically transactional and requires the manual recovery above.

## macOS launchd

`dcselfie.sh` uses `$ROOT/venv/bin/python` by default (`DC_PYTHON` may override), fails if it is missing, creates a shared ingest token, and writes private plist files using Python `plistlib`. Argument/path XML metacharacters are escaped as data. It controls its own registered labels; it no longer broadly kills processes matching `launcher.py` or hides bootstrap failures. Stop manually started conflicting processes yourself before installation. Restart uses bootout/bootstrap for orderly shutdown. Cloudflared is optional; existing tunnel plists can still be stopped even if its executable is later absent.

Configure `WEB_PORT` in `.env` consistently; status probes read that same configuration. launchd writes private files under `logs/`. Set an external log rotation policy for this directory (size and retention, with copy/truncate for inherited file descriptors) before unattended use. This script does not install a privileged log rotation daemon. macOS swap/hibernation and core collection require host-specific controls; the repository does not claim that launchd alone enforces Linux-style swap limits. Live launchctl behavior must be validated on macOS.

## Cloudflare and SOCKS

Actual Cloudflare tunnel settings and the private autossh LaunchAgent are not tracked in this repository. Copy/adapt [cloudflared.example.yml](cloudflared.example.yml), keep credentials private, and run `cloudflared tunnel ingress validate` plus `cloudflared tunnel ingress rule https://gallery.example.com/internal/images`. The internal path should select the rejecting rule. Keep the origin loopback-only and use an edge rule that overwrites `x-origin-secret` with the same private value as `WEB_ORIGIN_SECRET`; only then does the application trust `cf-connecting-ip`. Without the shared secret, clients behind the tunnel share the socket-peer rate bucket. Enable edge connection/bandwidth limits; process-local application limits are not DDoS protection. Turnstile is enforced even for direct requests and should be configured with both keys. See [Cloudflare's ingress configuration documentation](https://developers.cloudflare.com/tunnel/advanced/local-management/configuration-file/).

For reverse dynamic SOCKS, use `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:1080 ubuntu@HOST` on the home machine. Use `ARCA_SOCKS_PROXY=socks5h://localhost:1080` on the server so proxy DNS resolution follows the tunnel. Keep SSH host-key verification enabled, use a restricted key/account, and keep the listener loopback-only (`GatewayPorts no` on the server). A failed forward must terminate rather than leave an apparently healthy SSH process. An autossh or launchd supervisor can restart it. Configure server-side ClientAlive settings to remove stale sessions. See [OpenSSH options](https://man.openbsd.org/ssh_config) for forward-failure and alive behavior. Source CDNs and Cloudflare challenges change; the service uses bounded fallback/retry and cannot guarantee residential egress is always accepted.

## Triage

- 401 ingest: shared token mismatch; restart web and launcher after changing `.env`.
- 403 feed/images: Turnstile cookie missing/expired; authenticate in the browser. The terminal dashboard's feed panel cannot solve Turnstile.
- 413 ingest/image: encoded byte or decoded-pixel bound; inspect configured limits before raising them.
- 429/503: rate/concurrency limit or maintenance. Clients back off; check edge abuse and worker throughput.
- Freshness false: no recent gallery activity; inspect per-source logs and destination failures. It is not necessarily a server fault on a quiet board.
- SQLite locked: inspect competing processes/filesystem and permissions; the archive uses WAL, FULL synchronous commits and a 5-second busy timeout. Do not delete the ledger as a workaround.
- Corrupt ledger: stop workers, preserve original database/WAL files, restore a verified SQLite backup or investigate on a copy. Automatic replacement would silently forget deduplication state.
