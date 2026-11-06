---
title: "vLLM-Router CI"
created: 2026-11-06
updated: 2026-11-09
type: index
tags: [vllm-router, ci]
sources: [.buildkite/README.md, .buildkite/pipeline.yml, .buildkite/test-ready.yml, .buildkite/test-merge.yml, .buildkite/test-nightly.yml, .buildkite/release-pipeline.yml, .buildkite/ci_mirror_hardwares.yml, .buildkite/scripts/build-ci-image.sh, pytest.ini, tests/python/helpers/mark.py, "vllm-project/router@ddf78218cffdb6361579d3babd69a90ba6d63aaa"]
---

# vLLM-Router CI

## 什么时候查这里

- 判断某类改动该进 L1 ready、L2 merge 还是 L3 nightly，或核对某个 job 的实际触发条件。
- 发版前确认默认分支上哪条通道的结果才算证据 → 见下面目录里的发布规范。

## 不放什么

- 跨仓库通用的测试选择方法 → [通用 CI](../../../general/ci/_index.md)。
- 具体 job 的历史失败复盘；需要时看对应 Buildkite build。

## 上传条件

`pipeline.yml` 先构建 Rust release 二进制、wheel 和 NVIDIA CI 镜像，再按分支和 label 上传三个
测试文件。上传只依赖 `build` 组，彼此不依赖；GPU job 还要等 `image-build`。CI 镜像由
`docker/Dockerfile.ci` 推到共享 ECR，tag 里带 `router-<commit>`，不会顶掉同 commit 的其他项目镜像。

| 通道 | 触发 | 内容 | 证据边界 |
|---|---|---|---|
| L1 ready | 非默认分支带 `ready` label（fork 也跑） | Rust 单测/集成（`--features cpu,ready`）、Python 单测（`-m "ready and cpu"`，`--ignore=tests/python/e2e`，产出 `coverage.xml`）、P/D smoke（4×L4，`SKIP_LM_EVAL`，1 条 sanity）、kv_aware e2e（2×L4）、gRPC P/D smoke（4×L4，`RUN_LM_EVAL=0`） | 只证明该 PR 自身可合并，不能当默认分支健康度 |
| L2 merge | 默认分支 push（`NIGHTLY != 1`）、merge queue（base 为默认分支）、或 `merge-test` label | xGMI smoke（`read_mode=true`、GSM8K `LM_EVAL_LIMIT=20`、基线 0.38 允许 −0.03）、RDMA `SMOKE_ONLY`（两节点） | 默认分支健康度与 ROCm 平台基线 |
| L3 nightly | 默认分支 `NIGHTLY=1`，或 `nightly-test` label | 单测（PR 同时带 `ready` 时整组跳过）、P/D 全量 GSM8K（HTTP 与 gRPC）、gRPC prefix-KV perf（`rust_grpc` + `python_http`，`c16-r32`、`hit99-c4`、`hit99-c64`，2×H100）、L1 encode perf（Qwen3-0.6B，1×L4，`hit0.30`/`hit0.99999` × `c4`/`c16`，L1 on/off）、ROCm xGMI 与 RDMA 的 read true/false 矩阵 | 覆盖最广，但不阻塞 PR，过期结果不能当发版证据 |
| release | `v*` tag 或手动触发 | 只构建 wheel/二进制、smoke、发 PyPI、发镜像 | 不跑 L1–L3，见发布规范 |

几处窄 gate 容易漏：L1 与 L3 的 NVIDIA GPU job 在 `ROCM_ONLY=1` 时跳过；L3 的 xGMI 还接受
UI/API 加 `RUN_ROCM_XGMI=1`，RDMA 只接受 schedule 加 `NIGHTLY=1`、`nightly-test` label，或
UI/API 加 `RUN_ROCM_RDMA=1`。

## 测试分级标记

- 层级与机器用 pytest marker 表达：`ready`、`L4`、`MI300`、`cpu`、`cuda`、`rocm`、`gpu`、
  `cards_{1,2,4}`，登记在 `pytest.ini`；`hardware_test(res=..., num_cards=N)` 会派生对应
  SKU/平台/卡数 mark（`tests/python/helpers/mark.py`）。
- 新增或改动测试必须带层级和机器标记，`pre-commit` 的 `check-mark`
  （`tools/pre_commit/check_test_marks.py`）会拦缺标记的 PR；Rust 侧用 Cargo feature（`cpu`、`ready`）。
- L3 的 Rust 与 Python 单测和 L1 用同一组 `--features cpu,ready` / `-m "ready and cpu"`，单测覆盖
  不随层级变大；L3 多出来的是 GPU/ROCm 矩阵、GSM8K 和两个 perf job。
- GPU job 在 `ci_mirror_hardwares.yml` 里按 preset 取机器：`l4_1`（L1 encode perf）、
  `l4_2`（kv_aware）、`l4_4`（P/D 与 gRPC）、`h100_2`（prefix-KV perf）、`mi300_2`（xGMI）、
  `mi300_rdma`（两节点 RDMA）。job 写 `mirror_hardwares`，由 `scripts/upload_pipeline.py` 展开成
  agents 和 plugins。

## 目录内容

| 遇到什么 | 查看哪里 | 说明 |
|---|---|---|
| 发版门禁、tag/版本一致性、PyPI 与 Docker 发布顺序、发布产物核对 | [发布规范](release-gates.md) | 硬门禁与质量门槛分栏，含常见失败模式 |
