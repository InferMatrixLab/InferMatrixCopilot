---
title: "vLLM Router 仓库知识入口"
created: 2026-11-10
updated: 2026-11-10
type: index
tags: [vllm-router]
sources: [pyproject.toml, .buildkite/README.md, "vllm-project/router@ddf78218cffdb6361579d3babd69a90ba6d63aaa"]
---

# vLLM Router 仓库知识入口

## 仓库和适用范围

- 上游仓库：`vllm-project/router`。PyPI 包 `vllm-router`，Python 模块 `vllm_router`，console
  script 与 Rust 二进制都叫 `vllm-router`；默认分支 `main`，tag 形如 `v0.1.17`。
- Canonical slug：`vllm-router`。别名包括 `vllm_router` 与 GitHub 仓库名 `router`。
- 适用范围：该仓库的 issue 分诊与优先级打标，以及 CI 与发布知识；发版事实按
  `main@ddf78218` 与 tag `v0.1.17` 核对。

## 什么时候查这里

- 用户给出 `vllm-project/router`、`vllm-router` 或 `router`，需要映射到本目录。
- 为该仓库的 issue 选择致命、高、中、低。
- 判断本仓库怎么发版：tag 形态、版本号要同步哪几份文件、PyPI/镜像产物哪些是硬门禁。
- 区分 L1 ready / L2 merge / L3 nightly 与 `release` 四条 Buildkite 通道的适用范围。

## 不放什么

- 换仓库仍然成立的方法放 [general](../../general/_index.md)，包括通用的发版、CI 和 DI 口径。
- 其他仓库的调度实现、模型或 CI 经验。
- 单次 issue 的调查时间线、PR 复盘和评测记录。
- 机器地址、queue 凭据、本地 checkout 路径 → Git 忽略的 `local/`。

## 目录内容

| 遇到什么 | 查看哪里 | 说明 |
|---|---|---|
| 给 `vllm-project/router` 的 issue 定级或打标 | [issue 分级规则](bug/rules.md) | 致命 / 高 / 中 / 低；先看影响，再看特性 |
| 查看本仓库的 Bug 主题入口 | [bug](bug/_index.md) | 只路由到分级规则 |
| 发版门禁、tag 与版本号同步、PyPI/Docker/GitHub Release 产物核对和常见失败 | [发布规范](ci/release-gates.md) | `release-pipeline.yml` 真实执行的门禁 + 发布前质量门槛 |
| L1/L2/L3 通道、pytest marker、GPU preset、nightly 与 release 的分工 | [CI 入口](ci/_index.md) | 只写本仓库相对通用 CI 的差异 |

## 当前边界

本切片目前只有 `bug/` 与 `ci/` 两个工作主题。`components/`、`models/` 和代码 owner 路由
（`_routes.yaml`）尚未建立；`adapters/vllm_router` 只在 issue 分诊时注入 briefing，不提供代码
owner 路由。审查本仓库代码时不能把发布页当代码 owner，必须按 live 源码和 PR changed files
自行定位。
