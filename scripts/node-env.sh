#!/usr/bin/env bash
# Source from the repository root; use normal Node first, bundled desktop runtime second.
if ! command -v node >/dev/null 2>&1; then
  SPOTLAB_NODE_ROOT="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node"
  if [ -x "$SPOTLAB_NODE_ROOT/bin/node" ]; then
    export PATH="$SPOTLAB_NODE_ROOT/bin:$PATH"
  fi
fi
if command -v npm >/dev/null 2>&1; then
  spotlab_npm() { npm "$@"; }
elif [ -f "$PWD/.tools/npm/bin/npm-cli.js" ]; then
  SPOTLAB_NPM_CLI="$PWD/.tools/npm/bin/npm-cli.js"
  spotlab_npm() { node "$SPOTLAB_NPM_CLI" "$@"; }
else
  echo 'Node/npm bulunamadı. Önce bash scripts/setup-local.sh çalıştırın.' >&2
  return 1
fi
