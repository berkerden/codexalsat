#!/usr/bin/env sh
set -eu

python3 scripts/secret_scan.py --all

if [ -d backend/src ]; then
  python3 -m ruff check backend/src backend/tests
  python3 -m mypy backend/src
  python3 -m pytest
fi

if [ -f frontend/package.json ]; then
  (cd frontend && npm run lint && npm run typecheck && npm run build)
fi
