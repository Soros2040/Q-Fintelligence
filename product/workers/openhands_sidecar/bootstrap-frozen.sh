#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly SIDECAR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
readonly UV_BIN="${UV_BIN:-$(command -v uv || true)}"
readonly EXPECTED_UV_VERSION="uv 0.11.29 (x86_64-unknown-linux-gnu)"
readonly PYTHON_REQUEST="3.12.13"
readonly VERIFY_SCRIPT="${SIDECAR_DIR}/supply_chain/verify_supply_chain.py"

verify_only=false
offline=false
for argument in "$@"; do
  case "${argument}" in
    --verify-only) verify_only=true ;;
    --offline) offline=true ;;
    *)
      echo "usage: $0 [--verify-only] [--offline]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "${UV_BIN}" ]]; then
  echo "approved uv executable is missing: ${UV_BIN}" >&2
  exit 2
fi
if [[ "$("${UV_BIN}" --version)" != "${EXPECTED_UV_VERSION}" ]]; then
  echo "uv version does not match the sidecar supply-chain lock" >&2
  exit 2
fi

/usr/bin/python3 "${VERIFY_SCRIPT}" --sidecar-dir "${SIDECAR_DIR}"
if [[ "${verify_only}" == true ]]; then
  echo "OpenHands sidecar fresh-checkout inputs: PASS"
  exit 0
fi

uv_network_args=()
if [[ "${offline}" == true ]]; then
  uv_network_args+=(--offline)
else
  "${UV_BIN}" python install --managed-python --no-progress "${PYTHON_REQUEST}"
fi

python_bin="$(
  "${UV_BIN}" python find \
    --no-project \
    --managed-python \
    --resolve-links \
    "${uv_network_args[@]}" \
    "${PYTHON_REQUEST}"
)"

"${UV_BIN}" sync \
  --project "${SIDECAR_DIR}" \
  --frozen \
  --no-dev \
  --python "${python_bin}" \
  --no-progress \
  "${uv_network_args[@]}"

venv_python="${SIDECAR_DIR}/.venv/bin/python"
if [[ ! -x "${venv_python}" ]]; then
  echo "sidecar virtual environment was not created" >&2
  exit 2
fi
resolved_python="$(readlink -f "${venv_python}")"
if [[ "${resolved_python}" != "${python_bin}" ]]; then
  echo "sidecar virtual environment is not based on the approved Python" >&2
  exit 2
fi

"${venv_python}" -I -c '
from importlib.metadata import distributions, version
import sys

if sys.version.split()[0] != "3.12.13":
    raise SystemExit("unexpected Python version")
expected_openhands = {
    "openhands-sdk": "1.39.0+qf.noobservability.1",
    "openhands-tools": "1.39.0",
    "openhands-workspace": "1.39.0",
    "openhands-agent-server": "1.39.0",
}
for package, expected in expected_openhands.items():
    if version(package) != expected:
        raise SystemExit(f"unexpected {package} version")
installed = {
    item.metadata["Name"].lower().replace("_", "-")
    for item in distributions()
    if item.metadata["Name"]
}
banned = {
    "lmnr",
    "lmnr-claude-code-proxy",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp",
}
banned_installed = sorted(
    name
    for name in installed
    if name in banned
    or name.startswith("opentelemetry-exporter-")
    or name.startswith("opentelemetry-instrumentation-")
)
if banned_installed:
    raise SystemExit(
        f"banned observability distributions are installed: {banned_installed}"
    )
'

echo "OpenHands sidecar frozen bootstrap: PASS"
