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
# Always use the venv's python — never the system pip (PEP 668 blocks it on
# modern Ubuntu). uv-created venvs ship without pip, so bootstrap it if missing.
# Non-fatal: a dep hiccup must not stop the service restart for code-only changes.
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true
  .venv/bin/python -m pip install -e . --quiet || echo "[deploy] WARN: dep install skipped/failed — continuing to restart"
else
  echo "[deploy] WARN: .venv/bin/python not found — skipping deps"
fi

echo "[deploy] restarting service…"
sudo systemctl restart media-factory

echo "[deploy] done. Recent service log:"
sleep 2
sudo systemctl --no-pager --lines 8 status media-factory || true
