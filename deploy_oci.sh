#!/usr/bin/env bash
# Stage dependencies before stopping services; restore code, venv and units on failure.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# These units target this installation. Adapt both units before using another path/user.
[[ "$ROOT" == /home/ubuntu/dcinsideImageCrawler ]] || { echo "Adapt systemd paths before deploying here." >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Working tree must be clean (including untracked files)." >&2; exit 1; }
mkdir -p .deploy
exec 9>.deploy/lock
flock -n 9 || { echo "Another deployment is running." >&2; exit 1; }
old_head="$(git rev-parse HEAD)"
git fetch origin main
target="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$old_head" "$target"
stage="$ROOT/.deploy/source-$target"
new_venv="$ROOT/.deploy/venv-$target-$(date +%s)"
backup="$ROOT/.deploy/backup-$(date +%s)"
mkdir "$backup"
git worktree add --detach "$stage" "$target"
cleanup() { git worktree remove "$stage" >/dev/null 2>&1 || true; }
trap cleanup EXIT
python3 -m venv "$new_venv"
"$new_venv/bin/python" -m pip install -r "$stage/requirements.txt" -c "$stage/constraints.txt"
(cd "$stage" && "$new_venv/bin/python" -m compileall -q Module scripts *.py && "$new_venv/bin/python" scripts/check_install.py)
"$new_venv/bin/python" scripts/ensure_web_ingest_token.py .env
chmod 600 .env
for unit in dcselfie-web.service dcselfie-launcher.service; do
  if [[ -f "/etc/systemd/system/$unit" ]]; then cp "/etc/systemd/system/$unit" "$backup/$unit"; fi
done
promoted=0
rollback() {
  status=$?
  if [[ "$status" == 0 ]]; then status=1; fi
  trap - ERR INT TERM
  set +e
  echo "Deployment failed; restoring previous release." >&2
  sudo systemctl stop dcselfie-launcher dcselfie-web
  if [[ "$promoted" == 1 ]]; then
    git reset --hard "$old_head"
    if [[ -e "$backup/venv" || -L "$backup/venv" ]]; then
      if [[ -L venv ]]; then unlink venv; fi
      mv "$backup/venv" venv
    fi
  fi
  for unit in dcselfie-web.service dcselfie-launcher.service; do
    if [[ -f "$backup/$unit" ]]; then sudo install -m 0644 "$backup/$unit" "/etc/systemd/system/$unit"; fi
  done
  sudo systemctl daemon-reload
  sudo systemctl start dcselfie-web dcselfie-launcher
  echo "Inspect systemctl status and journalctl; old release: $old_head" >&2
  exit "${status:-1}"
}
trap rollback ERR INT TERM
sudo systemctl stop dcselfie-launcher dcselfie-web
# No other process/editor may modify this checkout during deployment.
promoted=1
git merge --ff-only "$target"
mv venv "$backup/venv"
ln -s "$new_venv" venv
sudo install -m 0644 dcselfie-launcher.service dcselfie-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start dcselfie-web
ready=0
for ((i=0; i<20; i++)); do
  if venv/bin/python scripts/probe_web_ingest.py .env; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]]
sudo systemctl start dcselfie-launcher
ready=0
for ((i=0; i<20; i++)); do
  pid="$(systemctl show dcselfie-launcher --property MainPID --value)"
  if systemctl is-active --quiet dcselfie-web dcselfie-launcher && [[ "$pid" =~ ^[1-9][0-9]*$ ]] && pgrep -P "$pid" -f 'run_gallery.py' >/dev/null; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]]
trap - ERR INT TERM
echo "Deployed $target; rollback venv and units retained at $backup (previous commit $old_head)."
