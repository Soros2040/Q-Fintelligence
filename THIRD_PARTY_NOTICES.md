# Third-party components / 第三方组件

Original project code and documentation follow the root licenses. Third-party software retains its own license and attribution. Installed distributions must retain their included notices.

| Area | Version and license information |
|---|---|
| Product JavaScript | [package.json](product/package.json), [package-lock.json](product/package-lock.json), installed distribution licenses |
| Product Python | [pyproject.toml](product/pyproject.toml), [uv.lock](product/uv.lock), installed metadata |
| Algorithm | [pyproject.toml](algorithm/pyproject.toml), [requirements.lock](algorithm/requirements.lock), [notice](algorithm/THIRD_PARTY_NOTICES.md) |
| OpenHands derivative | [NOTICE](product/workers/openhands_sidecar/supply_chain/NOTICE), [source lock](product/workers/openhands_sidecar/supply_chain/source-lock.json), [patch](product/workers/openhands_sidecar/supply_chain/openhands-sdk-noobservability.patch), license embedded in vendored wheel |

The OpenHands SDK derivative uses upstream `OpenHands/software-agent-sdk`, tag `v1.39.0`, commit `54dfbc551408d10de54eb8ac5612bae6d3f99d16`. The source lock identifies the upstream archive/license, local patch and derivative wheel. Publication checks compare the wheel, patch and NOTICE hashes with this record; they do not rebuild the wheel.

Product repository catalogs describe candidate integrations and historical observations. Their labels do not replace license review of a component chosen for future integration. Quantum service access remains subject to provider terms.

原创代码与文档遵循根目录许可，第三方保留自己的许可与署名。分发依赖时保留安装包声明。上表给出产品、算法与 OpenHands 派生组件的版本和权利记录。公开验收核对 wheel、补丁与 NOTICE 哈希，不代表重建 wheel。产品候选目录与历史审计标签不能替代未来集成时的许可审查；量子平台访问仍遵循服务条款。
