#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

if [[ "$(pwd -P)" != "$EXPECTED_ROOT" ]]; then
  echo "Run bootstrap only from $EXPECTED_ROOT." >&2
  exit 2
fi

command -v uv >/dev/null 2>&1 || { echo "uv is required; install from https://docs.astral.sh/uv/" >&2; exit 2; }
if ! command -v nvm >/dev/null 2>&1 && [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"; fi
command -v nvm >/dev/null 2>&1 || { echo "nvm is required for Node.js setup." >&2; exit 2; }
nvm install
nvm use

uv python install 3.11
uv sync --frozen --all-packages --group dev
npm ci

npm run check
uv run pytest
uv run ruff check .
npm run doctor
