#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [[ "$(uname -s)" != "Linux" ]] || ! grep -qi microsoft /proc/version; then
  echo "BLOCKED: this project must run inside WSL 2." >&2
  exit 2
fi
if [[ "$(pwd -P)" != "$EXPECTED_ROOT" ]]; then
  echo "BLOCKED: expected $EXPECTED_ROOT, got $(pwd -P)." >&2
  exit 2
fi

command -v uv >/dev/null 2>&1 || { echo "uv is required; install from https://docs.astral.sh/uv/" >&2; exit 2; }
if ! command -v nvm >/dev/null 2>&1 && [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"; fi
command -v nvm >/dev/null 2>&1 || { echo "nvm is required for Node.js setup." >&2; exit 2; }
nvm use --silent >/dev/null

[[ "$(node --version)" == v24.* ]] || { echo "BLOCKED: Node 24 is required." >&2; exit 2; }
command -v uv >/dev/null 2>&1 || { echo "BLOCKED: uv is missing." >&2; exit 2; }

if [[ -x .venv/bin/python ]]; then
  [[ "$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == "3.11" ]] || {
    echo "BLOCKED: .venv must use Python 3.11." >&2
    exit 2
  }
else
  echo "WARN: .venv is not installed; run bash infra/wsl/bootstrap.sh."
fi

npm run ports:check

echo "XH boundary (read-only observation):"
pgrep -af "[r]eserved-external-workload" || true
echo "PASS: WSL path, toolchain, remote and dedicated port boundary are valid."
