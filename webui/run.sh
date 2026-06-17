#!/usr/bin/env bash
# Convenience launcher for the SDR Console local UI.
#
#   ./webui/run.sh dev    -> Python API (:8787) + Vite dev server (:5173, hot reload)
#   ./webui/run.sh        -> build the frontend, then serve UI + API from one
#                            Python process at http://localhost:8787  (no Node at runtime)
#
# Run from the project root.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

MODE="${1:-prod}"

if [ "$MODE" = "dev" ]; then
  echo "[run] starting Python API on :8787 ..."
  python3 webui/server/app.py --port 8787 &
  API_PID=$!
  trap 'kill $API_PID 2>/dev/null || true' EXIT
  echo "[run] starting Vite dev server on :5173 (proxies /api -> :8787) ..."
  npm --prefix webui/frontend run dev
else
  if [ ! -d webui/frontend/node_modules ]; then
    echo "[run] installing frontend deps ..."
    npm --prefix webui/frontend install
  fi
  echo "[run] building frontend ..."
  npm --prefix webui/frontend run build
  echo "[run] serving UI + API at http://localhost:8787"
  python3 webui/server/app.py --port 8787
fi
