"""Register QF OpenHands extensions before Agent Server loads persisted state."""

from __future__ import annotations

import runpy
import sys

RUNTIME_EXTENSION_ROOT = "/opt/qf/runtime"


def main() -> None:
    if RUNTIME_EXTENSION_ROOT not in sys.path:
        sys.path.insert(0, RUNTIME_EXTENSION_ROOT)

    # Importing the module registers its discriminated-union kind with the
    # OpenHands SDK before ConversationService scans persisted base_state.json.
    import qf_openhands.security  # noqa: F401, PLC0415

    runpy.run_module("openhands.agent_server", run_name="__main__")


if __name__ == "__main__":
    main()
