#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
SPOTLAB_PYTHON=python3
SPOTLAB_BUNDLED="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [ -x "$SPOTLAB_BUNDLED" ]; then SPOTLAB_PYTHON="$SPOTLAB_BUNDLED"; fi
if [ ! -x .venv/bin/python ]; then "$SPOTLAB_PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev]'
if ! command -v npm >/dev/null 2>&1 && [ ! -f .tools/npm/bin/npm-cli.js ]; then
  mkdir -p .tools/npm
  curl -fsSL https://registry.npmjs.org/npm/-/npm-10.9.3.tgz -o .tools/npm.tgz
  tar -xzf .tools/npm.tgz --strip-components=1 -C .tools/npm
fi
source scripts/node-env.sh
(cd frontend && spotlab_npm ci)
git config core.hooksPath .githooks
echo 'Kurulum hazır. Başlatma: bash scripts/run-local.sh'
