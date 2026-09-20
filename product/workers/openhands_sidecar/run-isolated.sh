#!/usr/bin/env bash
set -euo pipefail
umask 077

SIDECAR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "${SIDECAR_DIR}/../.." && pwd -P)"
STATE_ROOT="${1:-}"
EXPECTED_STATE_PARENT="${PROJECT_ROOT}/.local/openhands-sessions"
EXPECTED_WORKSPACE_PARENT="${PROJECT_ROOT}/.local/openhands-workspaces"
EXPECTED_SOCKET_ROOT="${PROJECT_ROOT}/.local/ohs"

if [[ -z "${STATE_ROOT}" ]]; then
  echo "OpenHands sidecar state directory is required" >&2
  exit 2
fi

mkdir -p "${EXPECTED_STATE_PARENT}" "${EXPECTED_WORKSPACE_PARENT}" "${EXPECTED_SOCKET_ROOT}" "${STATE_ROOT}"
chmod 700 "${EXPECTED_SOCKET_ROOT}" "${STATE_ROOT}"
EXPECTED_STATE_PARENT="$(realpath -e "${EXPECTED_STATE_PARENT}")"
EXPECTED_WORKSPACE_PARENT="$(realpath -e "${EXPECTED_WORKSPACE_PARENT}")"
EXPECTED_SOCKET_ROOT="$(realpath -e "${EXPECTED_SOCKET_ROOT}")"
STATE_ROOT="$(realpath -e "${STATE_ROOT}")"
case "${STATE_ROOT}" in
  "${EXPECTED_STATE_PARENT}"/*) ;;
  *)
    echo "OpenHands sidecar state directory escaped the QF state root" >&2
    exit 2
    ;;
esac
WORKSPACE_ROOT="${EXPECTED_WORKSPACE_PARENT}/$(basename "${STATE_ROOT}")"
mkdir -p "${WORKSPACE_ROOT}"
chmod 700 "${WORKSPACE_ROOT}"
WORKSPACE_ROOT="$(realpath -e "${WORKSPACE_ROOT}")"
case "${WORKSPACE_ROOT}" in
  "${EXPECTED_WORKSPACE_PARENT}"/*) ;;
  *)
    echo "OpenHands workspace directory escaped the QF workspace root" >&2
    exit 2
    ;;
esac

exec 9>"${STATE_ROOT}/.sidecar.lock"
if ! flock -n 9; then
  echo "OpenHands sidecar state directory is already in use" >&2
  exit 2
fi

VENV_ROOT="${SIDECAR_DIR}/.venv"
MAIN_PATH="${SIDECAR_DIR}/main.py"
if [[ ! -x "${VENV_ROOT}/bin/python" || ! -f "${MAIN_PATH}" ]]; then
  echo "OpenHands sidecar environment is not synchronized" >&2
  exit 2
fi
if ! command -v bwrap >/dev/null 2>&1; then
  echo "bubblewrap is required for the OpenHands sidecar" >&2
  exit 2
fi
if [[ ! -S /run/docker.sock ]] || ! command -v docker >/dev/null 2>&1; then
  echo "Docker Agent Server backend is unavailable" >&2
  exit 2
fi
PYTHON_BASE="$(cd "$(dirname "$(readlink -f "${VENV_ROOT}/bin/python")")/.." && pwd -P)"
case "${PYTHON_BASE}" in
  *) ;;
  *)
    echo "OpenHands sidecar Python escaped the approved uv-managed 3.12 root" >&2
    exit 2
    ;;
esac

ulimit -c 0
ulimit -n 256
# RLIMIT_NPROC counts threads for the real UID on Linux. WSL desktop processes
# already exceed 256 threads before QF starts, so a 256 controller limit makes
# namespace creation fail before isolation is established. The code workspace
# remains independently capped by Docker's --pids-limit=256.
ulimit -u 1024
ulimit -v 2097152

exec bwrap \
  --die-with-parent \
  --new-session \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  --unshare-cgroup \
  --hostname qf-openhands-sidecar \
  --cap-drop ALL \
  --ro-bind / / \
  --proc /proc \
  --dev /dev \
  --tmpfs /mnt \
  --tmpfs /root \
  --tmpfs /tmp \
  --bind /run/docker.sock /run/docker.sock \
  --ro-bind "${SIDECAR_DIR}" /opt \
  --bind "${STATE_ROOT}" "${STATE_ROOT}" \
  --bind "${WORKSPACE_ROOT}" "${WORKSPACE_ROOT}" \
  --bind "${EXPECTED_SOCKET_ROOT}" "${EXPECTED_SOCKET_ROOT}" \
  --clearenv \
  --setenv LANG C.UTF-8 \
  --setenv PATH /opt/.venv/bin:/usr/bin:/bin \
  --setenv USER sandbox \
  --setenv LOGNAME sandbox \
  --setenv HOME /home/sandbox \
  --setenv DOCKER_CONFIG /tmp/qf-docker-config \
  --setenv QF_PROCESS_NAMESPACE qfintelligence \
  --setenv QF_OPENHANDS_STATE_ROOT "${STATE_ROOT}" \
  --setenv QF_OPENHANDS_WORKSPACE_ROOT "${WORKSPACE_ROOT}" \
  --setenv QF_OPENHANDS_PROJECT_SOURCE "${PROJECT_ROOT}" \
  --setenv QF_OPENHANDS_AGENT_SERVER_IMAGE qfintelligence/openhands-agent-server:1.39.0-qf.1 \
  --setenv LITELLM_LOCAL_MODEL_COST_MAP true \
  --setenv OPENHANDS_SUPPRESS_BANNER 1 \
  --setenv PYTHONHASHSEED 0 \
  --chdir "${STATE_ROOT}" \
  /opt/.venv/bin/python -I /opt/main.py
