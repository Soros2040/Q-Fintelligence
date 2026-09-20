# OpenHands sidecar supply chain

This directory is the rebuild boundary for the q-fintelligence-maintained
`openhands-sdk==1.39.0+qf.noobservability.1` derivative.

## Fresh-checkout installation

From the project checkout in WSL, run:

```bash
workers/openhands_sidecar/bootstrap-frozen.sh
```

The bootstrap fixes `uv==0.11.29`, uv-managed CPython `3.12.13`, the committed
`uv.lock`, and the vendored wheel SHA-256. It refuses a different wheel, lock,
Python, uv binary, user, or observability dependency. `--verify-only` checks all
committed inputs without creating `.venv`; `--offline` installs only from the
existing uv cache.

## Exact wheel rebuild

The source lock pins upstream tag `v1.39.0`, commit and Git tree, the GitHub
codeload archive SHA-256, SDK source-tree hashes, the QF metadata patch, NOTICE,
toolchain, canonical ZIP metadata, and the approved output SHA-256.

With an existing verified upstream checkout:

```bash
workers/openhands_sidecar/supply_chain/rebuild-wheel.sh \
  --source-dir /path/to/software-agent-sdk \
  --output /tmp/openhands_sdk-1.39.0+qf.noobservability.1-py3-none-any.whl
```

Without `--source-dir` or `--archive`, the script downloads only the pinned
codeload archive over HTTPS and verifies its SHA-256 before extraction. The
recipe applies `openhands-sdk-noobservability.patch`, leaves every Python source
byte unchanged, adds the upstream MIT license and QF NOTICE, omits only the
direct `lmnr` dependency, and creates the wheel with Python's standard library.
The output is published only if it exactly matches the approved wheel SHA-256.

The recipe never overwrites `vendor/` automatically. Upgrades require a new
upstream review, patch review, vulnerability audit, lock refresh, source lock,
and explicit QF approval.
