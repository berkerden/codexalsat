#!/usr/bin/env bash
set -euo pipefail

KEEP_AWAKE=0
if [ "$#" -gt 0 ]; then
  if [ "$#" -eq 1 ] && { [ "$1" = "--help" ] || [ "$1" = "-h" ]; }; then
    echo 'Kullanım: scripts/run-local.sh [--keep-awake]'
    echo '  --keep-awake  macOS boşta sistem uykusunu başlatıcı çalışırken önlemeyi ister.'
    exit 0
  elif [ "$#" -eq 1 ] && [ "$1" = "--keep-awake" ]; then
    KEEP_AWAKE=1
  else
    echo 'Kullanım: scripts/run-local.sh [--keep-awake]' >&2
    exit 2
  fi
fi

cd "$(dirname "$0")/.."

SPOTLAB_API_PID=''
SPOTLAB_WEB_PID=''
SPOTLAB_AWAKE_PID=''
child_is_running() {
  local target_pid="$1"
  local running_pid
  if ! kill -0 "$target_pid" 2>/dev/null; then
    return 1
  fi
  for running_pid in $(jobs -pr); do
    if [ "$running_pid" = "$target_pid" ]; then
      return 0
    fi
  done
  return 1
}
cleanup() {
  trap - EXIT INT TERM HUP
  if [ -n "$SPOTLAB_API_PID" ]; then kill "$SPOTLAB_API_PID" 2>/dev/null || true; fi
  if [ -n "$SPOTLAB_WEB_PID" ]; then kill "$SPOTLAB_WEB_PID" 2>/dev/null || true; fi
  if [ -n "$SPOTLAB_AWAKE_PID" ]; then kill "$SPOTLAB_AWAKE_PID" 2>/dev/null || true; fi
  if [ -n "$SPOTLAB_API_PID" ]; then wait "$SPOTLAB_API_PID" 2>/dev/null || true; fi
  if [ -n "$SPOTLAB_WEB_PID" ]; then wait "$SPOTLAB_WEB_PID" 2>/dev/null || true; fi
  if [ -n "$SPOTLAB_AWAKE_PID" ]; then wait "$SPOTLAB_AWAKE_PID" 2>/dev/null || true; fi
}
on_signal() {
  cleanup
  exit 0
}
trap cleanup EXIT
trap on_signal INT TERM HUP

source scripts/node-env.sh

if [ ! -x .venv/bin/python ]; then
  echo 'Python ortamı bulunamadı. Önce setup-local.sh çalıştırın.' >&2
  exit 1
fi
if [ ! -x .venv/bin/uvicorn ]; then
  echo 'Uvicorn bulunamadı. Önce setup-local.sh çalıştırın.' >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo 'Node bulunamadı. Önce setup-local.sh çalıştırın.' >&2
  exit 1
fi
if [ ! -f frontend/node_modules/vite/bin/vite.js ]; then
  echo 'Vite bulunamadı. Önce setup-local.sh çalıştırın.' >&2
  exit 1
fi

if [ "$KEEP_AWAKE" -eq 1 ]; then
  if [ "$(uname -s)" != 'Darwin' ]; then
    echo '--keep-awake yalnızca macOS üzerinde desteklenir.' >&2
    exit 1
  fi
  if ! command -v caffeinate >/dev/null 2>&1; then
    echo '--keep-awake istendi ancak caffeinate bulunamadı.' >&2
    exit 1
  fi
fi

# Keep both sockets open until every bind has succeeded. This prevents either
# service from starting when a previous local panel already owns one port.
if ! .venv/bin/python -c '
import socket
import sys

sockets = []
try:
    for port in (8000, 5173):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append(sock)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
except OSError as exc:
    print("127.0.0.1:%s kullanılamıyor: %s" % (port, exc), file=sys.stderr)
    sys.exit(1)
finally:
    for sock in sockets:
        sock.close()
'; then
  echo 'Panel başlatılmadı; 8000 ve 5173 portlarının boş olduğundan emin olun.' >&2
  exit 1
fi

mkdir -p data
if [ "$KEEP_AWAKE" -eq 1 ]; then
  caffeinate -i -w "$$" &
  SPOTLAB_AWAKE_PID=$!
  sleep 0.1
  if ! child_is_running "$SPOTLAB_AWAKE_PID"; then
    wait "$SPOTLAB_AWAKE_PID" 2>/dev/null || true
    echo 'Mac boşta sistem uykusunu önleme isteği başlatılamadı; panel başlatılmadı.' >&2
    exit 1
  fi
  echo 'Mac boşta sistem uykusunu önleme isteği etkin (yalnızca bu başlatıcı çalışırken).'
fi

.venv/bin/python -m uvicorn spotlab.main:app --host 127.0.0.1 --port 8000 &
SPOTLAB_API_PID=$!
(cd frontend && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort) &
SPOTLAB_WEB_PID=$!

echo 'Panel: http://127.0.0.1:5173 — Canlı emir adaptörü kapalı.'
while :; do
  if ! child_is_running "$SPOTLAB_API_PID"; then
    echo 'Backend durdu; diğer süreçler de kapatılıyor.' >&2
    exit 1
  fi
  if ! child_is_running "$SPOTLAB_WEB_PID"; then
    echo 'Web sunucusu durdu; diğer süreçler de kapatılıyor.' >&2
    exit 1
  fi
  if [ -n "$SPOTLAB_AWAKE_PID" ] && ! child_is_running "$SPOTLAB_AWAKE_PID"; then
    echo 'Mac boşta sistem uykusunu önleme isteği beklenmedik biçimde durdu; sunucular kapatılıyor.' >&2
    exit 1
  fi
  sleep 1
done
