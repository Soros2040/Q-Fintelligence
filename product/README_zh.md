# 量融智枢研究工作台

[English](README.md) · [首页](../README_zh.md) · [架构](../docs/architecture_zh.md) · [安装](../docs/reproduction_zh.md#2-产品工作台)

本快照包含量融智枢的 Web、控制面、Worker、合同、数据库迁移和测试。通用 Agent harness 提供编排、工具调用与状态管理基础；研究工作台在其上组织金融与量子流程。

使用 Linux/WSL2、Node.js 24.x、Python 3.11 和 `uv`，按指南使用锁文件安装并验证 mock。可选 OpenHands 使用独立 Python 3.12.13。`.env.example` 保持凭据为空。[验收状态](../docs/status_zh.md)区分源码检查、安装和流程验证。

| 目录 | 用途 |
|---|---|
| [apps](apps/) | Web 与控制面 |
| [packages](packages/) | 合同与 Schema |
| [workers](workers/) | 金融、量子和隔离 Agent |
| [infra](infra/) | SQLite、WSL 与 Sidecar 支持 |
| [scripts](scripts/) | 开发、实验编排与进程管理 |
| [tests](tests/) | TypeScript 与 Python 测试 |

实验编排和硬件脚本面向进阶操作者，执行前检查输入、模式和服务要求。入门从本地 mock 开始。
