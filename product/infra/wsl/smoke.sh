#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly XH_PATTERN="[r]eserved-external-workload"

if [[ "$(pwd -P)" != "$EXPECTED_ROOT" ]]; then
  echo "Run smoke only from $EXPECTED_ROOT." >&2
  exit 2
fi

command -v uv >/dev/null 2>&1 || { echo "uv is required; install from https://docs.astral.sh/uv/" >&2; exit 2; }
if ! command -v nvm >/dev/null 2>&1 && [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"; fi
command -v nvm >/dev/null 2>&1 || { echo "nvm is required for Node.js setup." >&2; exit 2; }
nvm use --silent >/dev/null
mkdir -p .runtime

xh_before="$(mktemp)"
xh_after="$(mktemp)"
cleanup() {
  npm run stop >/dev/null 2>&1 || true
  rm -f "$xh_before" "$xh_after"
}
trap cleanup EXIT

pgrep -f "$XH_PATTERN" | sort -n >"$xh_before" || true
npm run dev >.runtime/smoke.log 2>&1 &

for _ in $(seq 1 150); do
  if curl --fail --silent --max-time 1 http://127.0.0.1:27872/api/health >/dev/null \
    && curl --fail --silent --max-time 1 http://127.0.0.1:27871/ >/dev/null; then
    break
  fi
  sleep 0.2
done

curl --fail --silent --show-error http://127.0.0.1:27872/api/health
curl --fail --silent --show-error http://127.0.0.1:27872/api/foundation
curl --fail --silent --show-error http://127.0.0.1:27871/ >/dev/null

pgrep -f "$XH_PATTERN" | sort -n >"$xh_after" || true
if ! cmp -s "$xh_before" "$xh_after"; then
  echo "BLOCKED: XH process set changed during q-fintelligence smoke." >&2
  diff -u "$xh_before" "$xh_after" || true
  exit 2
fi

npm run stop
for _ in $(seq 1 100); do
  if ! ss -ltnH | grep -Eq '127[.]0[.]0[.]1:(27871|27872)\b'; then
    break
  fi
  sleep 0.1
done

if ss -ltnH | grep -Eq '127[.]0[.]0[.]1:(27871|27872)\b'; then
  echo "BLOCKED: q-fintelligence ports remain occupied after targeted stop." >&2
  exit 2
fi

echo "PASS: API/Web smoke succeeded, targeted stop released q-fintelligence ports, XH PIDs were unchanged."
