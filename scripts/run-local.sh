#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/node-env.sh
if [ ! -x .venv/bin/uvicorn ]; then echo 'Önce setup-local.sh çalıştırın.' >&2; exit 1; fi
mkdir -p data
.venv/bin/python -m uvicorn spotlab.main:app --host 127.0.0.1 --port 8000 &
SPOTLAB_API_PID=$!
(cd frontend && spotlab_npm run dev) &
SPOTLAB_WEB_PID=$!
cleanup() { kill "$SPOTLAB_API_PID" "$SPOTLAB_WEB_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
echo 'Panel: http://127.0.0.1:5173 — Canlı emir adaptörü kapalı.'
wait "$SPOTLAB_API_PID" "$SPOTLAB_WEB_PID"
