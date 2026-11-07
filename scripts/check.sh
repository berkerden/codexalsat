#!/usr/bin/env sh
set -eu

python3 scripts/secret_scan.py --all

if [ -d backend/src ]; then
  python3 -m ruff check backend/src backend/tests scripts/macos_service.py
  python3 -m mypy backend/src scripts/macos_service.py
  python3 -m pytest
fi

if [ -f frontend/package.json ]; then
  (cd frontend && npm run lint && npm run typecheck && npm run build)
fi
