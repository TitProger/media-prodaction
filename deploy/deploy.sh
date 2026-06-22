#!/usr/bin/env bash
# deploy.sh — pull latest code, install deps, restart the service.
# Used both by GitHub Actions (over SSH) and for manual deploys on the server.
#
# Manual run on the VPS:
#   cd ~/media-prodaction && ./deploy/deploy.sh
set -euo pipefail

# project root = parent of this script's dir
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[deploy] $(date '+%F %T') — pulling latest main…"
git fetch origin main
git reset --hard origin/main      # discard nothing tracked; secrets live in storage/ (gitignored)

echo "[deploy] installing dependencies…"
if [ -x ".venv/bin/pip" ]; then
  ./.venv/bin/pip install -e . --quiet
else
  pip install -e . --quiet
fi

echo "[deploy] restarting service…"
sudo systemctl restart media-factory

echo "[deploy] done. Recent service log:"
sleep 2
sudo systemctl --no-pager --lines 8 status media-factory || true
