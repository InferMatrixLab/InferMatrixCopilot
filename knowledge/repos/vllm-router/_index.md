---
title: "vLLM-Router 入口"
created: 2026-11-06
updated: 2026-11-06
type: index
tags: [vllm-router]
sources: [pyproject.toml, .buildkite/README.md, "vllm-project/router@ea20a532485c6f4635cee0dff6dab788b87d1365"]
---

# vLLM-Router

上游：`vllm-project/router`。PyPI 包 `vllm-router`，Python 模块 `vllm_router`，console
script 与 Rust 二进制都叫 `vllm-router`。别名：`vllm-router`、`vllm_router`、`router`（本地
目录名）。默认分支 `main`；本切片的发版事实按 `main@ea20a532` 与 tag `v0.1.17` 核对。

## 什么时候查这里

- 判断本仓库怎么发版：tag 形态、版本号要同步哪几份文件、PyPI/镜像产物哪些是硬门禁。
- 区分 L1 ready / L2 merge / L3 nightly 与 `release` 四条 Buildkite 通道的适用范围。

## 不放什么

- 跨仓库通用的发版、CI 和 DI 口径 → [通用 CI](../../general/ci/_index.md)、[DI 计算说明](../../general/bug/guides/di-calculation.md)。
- vLLM-Omni 的 CUDA L4/L5 与 NPU lane 不适用于本仓库：这里只有 L1/L2/L3 和 ROCm MI300 的 MoRI 通道。
- 机器地址、queue 凭据、本地 checkout 路径 → Git 忽略的 `local/`。

## 目录内容

| 遇到什么 | 查看哪里 | 说明 |
|---|---|---|
| 发版门禁、tag 与版本号同步、PyPI/Docker 产物和常见失败 | [发布规范](ci/release-gates.md) | `release-pipeline.yml` 真实执行的门禁 + 组织级政策门禁 |
| L1/L2/L3 通道、pytest marker、GPU preset、nightly 与 release 的分工 | [CI 入口](ci/_index.md) | 只写本仓库相对通用 CI 的差异 |

## 当前边界

本切片只覆盖 CI 与发布。`components/`、`models/` 和代码 owner 路由（`_routes.yaml`）尚未建立，
也没有 adapter；审查本仓库代码时不能把发布页当代码 owner，必须按 live 源码和 PR changed
files 自行定位。
