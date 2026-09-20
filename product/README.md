# Q-Fintelligence research workbench

[中文](README_zh.md) · [Home](../README.md) · [Architecture](../docs/architecture.md) · [Setup](../docs/reproduction.md#2-product-workbench)

This snapshot contains Q-Fintelligence's Web interface, control plane, workers, contracts, migrations and tests. The generic-agent harness supplies orchestration, tool invocation and state management; the research workbench adds financial and quantum workflows.

Use Linux/WSL2, Node.js 24.x, Python 3.11 and `uv`. Follow the setup guide for lock-based installation and mock checks. Optional OpenHands uses separate Python 3.12.13. `.env.example` leaves credentials empty. [Validation status](../docs/status.md) distinguishes source inspection, installation and workflow verification.

| Directory | Purpose |
|---|---|
| [apps](apps/) | Web UI and control plane |
| [packages](packages/) | Contracts and schemas |
| [workers](workers/) | Finance, quantum and isolated agent capabilities |
| [infra](infra/) | SQLite, WSL and sidecar support |
| [scripts](scripts/) | Development, campaigns and process management |
| [tests](tests/) | TypeScript and Python tests |

Campaign/hardware scripts are advanced operator entry points. Review inputs, mode and provider requirements before running them. Start with the local mock workflow.
