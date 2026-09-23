#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/node-env.sh
if [ ! -x .venv/bin/uvicorn ]; then echo 'Önce setup-local.sh çalıştırın.' >&2; exit 1; fi
mkdir -p data
.venv/bin/python -m uvicorn spotlab.main:app --host 127.0.0.1 --port 8000 &
SPOTLAB_API_PID=$!
(cd frontend && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort) &
SPOTLAB_WEB_PID=$!
cleanup() {
  kill "$SPOTLAB_API_PID" "$SPOTLAB_WEB_PID" 2>/dev/null || true
  wait "$SPOTLAB_API_PID" "$SPOTLAB_WEB_PID" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
echo 'Panel: http://127.0.0.1:5173 — Canlı emir adaptörü kapalı.'
while kill -0 "$SPOTLAB_API_PID" 2>/dev/null && kill -0 "$SPOTLAB_WEB_PID" 2>/dev/null; do
  sleep 1
done
echo 'Sunuculardan biri durdu; diğer süreç de kapatılıyor.' >&2
exit 1
