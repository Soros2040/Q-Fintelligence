# 复现与验证

[English](reproduction.md) · [首页](../README_zh.md) · [验收状态](status_zh.md)

本仓库包含源码、精选历史证据及可独立执行的教学检查。请先选择验证层次，它们的环境要求和证据范围不同。

## 1. 公开离线检查

使用 Python 3.11 或以上，在仓库根目录运行：

```bash
python examples/bridge_identity.py
python tools/verify_public_evidence.py
```

第一个检查枚举合成六资产问题全部 64 个状态，比较直接目标、上三角 QUBO 与 Ising 能量，并检查 20 个可行组合。第二个重新计算公开表格均值、样本数、部分配对效应与端点运算。两者仅使用标准库；`PASS` 表示这些具体检查通过。

[三份 Notebook](../notebooks/)使用相同离线材料和双语解释。可在本地 Jupyter 打开，从仓库或 Notebook 目录执行。文件已清空输出与执行元数据，发布验收也会执行代码单元，但不把输出写回文件。

## 2. 产品工作台

基准环境是 Linux 或 WSL2、Node.js **24.x**、Python **3.11** 与 `uv`。部分脚本使用 Unix Shell 语法，`doctor` 面向 WSL。

```bash
cd product
npm ci
uv sync --frozen
cp .env.example .env
npm run typecheck
npm test
npm run build
uv run pytest tests/python
npm run dev
```

mock 路径保持服务密钥为空。源码包含依赖锁和测试；发布检查不等同于全新安装和完整产品测试，见[状态表](status_zh.md)。

可选 OpenHands Sidecar 使用独立 Python **3.12.13** / `uv` **0.11.29** 环境，携带派生 wheel，并依赖相应执行路径的 `bwrap` / Docker。启用 `QF_AGENT_MODE=openhands` 前阅读[配置](../product/workers/openhands_sidecar/pyproject.toml)与[供应链说明](../product/workers/openhands_sidecar/supply_chain/README.md)。基础 mock 成功不代表此服务已验证。

## 3. 算法软件

包声明 Python **3.11**（`>=3.11,<3.12`），使用新环境：

```bash
cd algorithm
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
qf-algorithm --version
```

依赖版本与哈希来自源码快照，本次发布尚未认证全新解析与安装。

本地合成方法链使用实现包之外的新输出位置：

```bash
qf-algorithm run --epochs 1 --qaoa-mode fixed --shots 128 --output-dir ../outputs --run-id synthetic-review
```

这是建议的开发检查，会执行本地拟合与模拟，本次发布未运行。合成结果不复现 E01–E07。使用自有输入前阅读 [CLI](../algorithm/qf_algorithm/cli.py)、[配置](../algorithm/qf_algorithm/config.py)与 [Schema](../algorithm/qf_algorithm/schemas/)。

历史 `statistics`、兼容 `verify` / `analyze` / `recompute` 和 `--frozen` 路径需要获授权的源归档。本版不分发市场面板、完整执行记录或完整归档。现有历史紧凑归档也有缺失 canonical 记录，恢复别名无法生成缺失源文件，完整历史复现仍需资料保全。

## 登记结果

按[贡献模板](contributions_zh.md#贡献记录模板)记录提交、系统、运行时、命令、模式、输入来源、输出哈希及结果。明确属于算术复核、源码安装、实验重跑或设备执行。清理秘密后保留失败步骤与日志，可复现失败同样有价值。
