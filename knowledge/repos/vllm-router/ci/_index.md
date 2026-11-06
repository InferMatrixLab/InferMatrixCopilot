---
title: "vLLM-Router CI"
created: 2026-11-06
updated: 2026-11-06
type: index
tags: [vllm-router, ci]
sources: [.buildkite/README.md, .buildkite/pipeline.yml, .buildkite/test-ready.yml, .buildkite/test-merge.yml, .buildkite/test-nightly.yml, .buildkite/release-pipeline.yml, .buildkite/ci_mirror_hardwares.yml, pytest.ini]
---

# vLLM-Router CI

## 什么时候查这里

- 判断某类改动该进 L1 ready、L2 merge 还是 L3 nightly，或核对某个 job 的实际选择条件。
- 发版前确认默认分支上哪条通道的结果才算证据 → 见下面目录的发布规范。

## 不放什么

- 跨仓库通用的测试选择方法 → [通用 CI](../../../general/ci/_index.md)。
- 具体 job 的历史失败复盘；需要时看对应 Buildkite build。

## 四条通道

`pipeline.yml` 只负责构建（Rust release 二进制、wheel、NVIDIA CI 镜像）并按分支和 label
上传 L1/L2/L3 三个文件；镜像发布在 `release-pipeline.yml`，两条 pipeline 都认 `NIGHTLY=1`，
但含义不同：测试 pipeline 用它选测试，release pipeline 用它发 nightly 镜像。

| 通道 | 触发 | 内容 | 证据边界 |
|---|---|---|---|
| L1 ready | 非默认分支带 `ready` label（fork 也跑） | Rust 单测/集成、Python 单测（`-m "ready and cpu"`，`--cov=vllm_router` 产出 `coverage.xml`）、一个 NVIDIA P/D completion、gRPC P/D smoke | 只证明该 PR 自身可合并，不能当默认分支健康度 |
| L2 merge | 默认分支 push（`NIGHTLY!=1`）、merge queue、或 `merge-test` label | 一模式 xGMI smoke、RDMA `SMOKE_ONLY` | 默认分支健康度与 ROCm 平台基线 |
| L3 nightly | 默认分支 `NIGHTLY=1`、或 `nightly-test` label | 全套 Rust/Python 测试（PR 同时带 `ready` 时跳过）、完整 NVIDIA GSM8K、2×H100 gRPC prefix-KV perf、xGMI read+write 矩阵、RDMA 双向 | 覆盖最广，但不阻塞 PR，过期结果不能当发版证据 |
| release | `v*` tag 或手动触发 | 只构建 wheel/二进制、smoke、发 PyPI、发镜像 | 不跑 L1–L3 测试矩阵，见发布规范 |

## 测试分级标记

- 层级与机器用 pytest marker 表达：`ready`、`L4`、`MI300`、`cpu`、`cuda`、`rocm`、`gpu`、
  `cards_{1,2,4}`，登记在 `pytest.ini`；`hardware_test(res=..., num_cards=N)` 会派生对应
  SKU/平台/卡数 marker（`tests/python/helpers/mark.py`）。
- 新增或改动测试必须带层级和机器标记，`pre-commit` 的 `check-mark`（`tools/pre_commit/check_test_marks.py`）
  会拦缺标记的 PR；Rust 侧用 Cargo feature（`cpu`、`ready`）表达同类选择。
- L3 的 Rust job 与 L1 用同一组 `--features cpu,ready`，Rust 侧没有更高层级覆盖；L3 多出来的是
  GPU/ROCm 矩阵、GSM8K 和 gRPC perf，不能从“nightly Rust 绿”推出更强的 Rust 覆盖。
- GPU job 在 `ci_mirror_hardwares.yml` 里按 preset 取机器：`l4_4`、`h100_2`、`mi300_2`、
  `mi300_rdma`；job 写 `mirror_hardwares`，由 `scripts/upload_pipeline.py` 展开成 agents 和 plugins。

## 目录内容

| 遇到什么 | 查看哪里 | 说明 |
|---|---|---|
| 发版门禁、tag/版本一致性、PyPI 与 Docker 发布顺序 | [发布规范](release-gates.md) | 硬门禁与政策门禁分栏，含常见失败模式 |
