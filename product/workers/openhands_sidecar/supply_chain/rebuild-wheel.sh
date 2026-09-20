#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly SUPPLY_CHAIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
readonly UV_BIN="${UV_BIN:-$(command -v uv || true)}"
readonly EXPECTED_UV_VERSION="uv 0.11.29 (x86_64-unknown-linux-gnu)"
readonly PYTHON_REQUEST="3.12.13"

if [[ ! -x "${UV_BIN}" ]]; then
  echo "approved uv executable is missing: ${UV_BIN}" >&2
  exit 2
fi
if [[ "$("${UV_BIN}" --version)" != "${EXPECTED_UV_VERSION}" ]]; then
  echo "uv version does not match the sidecar supply-chain lock" >&2
  exit 2
fi

"${UV_BIN}" python install --managed-python --no-progress "${PYTHON_REQUEST}"
python_bin="$(
  "${UV_BIN}" python find \
    --no-project \
    --managed-python \
    --resolve-links \
    "${PYTHON_REQUEST}"
)"

exec "${python_bin}" "${SUPPLY_CHAIN_DIR}/rebuild_wheel.py" "$@"
