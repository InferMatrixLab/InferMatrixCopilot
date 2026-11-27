---
title: "Diffusion 并行策略总览"
created: 2026-07-16
updated: 2026-11-27
type: guide
tags: [vllm-omni, components, diffusion]
sources: [docs/design/feature/tensor_parallel.md, docs/design/feature/cfg_parallel.md, docs/design/feature/vae_parallel.md, "PR #6340", "PR #6786", "PR #11109", vllm_omni/diffusion/distributed/a2a_permute.py, vllm_omni/diffusion/distributed/cfg_parallel.py, vllm_omni/diffusion/attention/parallel/ulysses.py]
---

# Diffusion 并行策略总览

每种策略一篇官方 spec（`docs/design/feature/`，均在 `main @ 5c390096` 验证存在）：
`tensor_parallel.md`、`pipeline_parallel.md`、`sequence_parallel.md`、
`expert_parallel.md`、`cfg_parallel.md`（CFG 正负分支并行——与
[architecture 的 CFG companion 流](../serving/architecture.md)相关）、
`hsdp.md`、`vae_parallel.md`。本页只做路由：读具体策略以对应 spec 为准。

- 源码：`vllm_omni/diffusion/distributed/`（distributed_vae、sp_plan、序列并行
  hooks）；策略在 `diffusion/registry.py::initialize_model` 初始化链中注入
  （sequence parallelism、patch-parallel、VAE slicing/tiling）。
- **配置入口是 config 组件**：`composable_parallel` 的声明式 per-stage 轴栈
  （tp/dp/pp/ep/stage_replica 已接线；sp/cfg/vae_pp/hsdp 为保留位）见
  [Configuration](../configuration/architecture.md)；stage 级 `tensor_parallel_size` 等
  字段见 deploy schema。
- strict Ulysses transport 以目标版本为准：`v0.34.0` tag 用旧布尔开关且默认关闭；[#11109](https://github.com/vllm-project/vllm-omni/pull/11109) 合入后，`ulysses_a2a_backend=auto` 仅在合资格的 CUDA strict pure Ulysses 路径自动选 CE；不满足选择条件就不启用 CE。显式 `copy_engine` 与 `nccl` 可用于固定实验的 transport；先确认模型、设备与执行模式支持，不以参数值推断实际 kernel。
  选择约束和 workspace 生命周期见 [DIFF-4x](rules-system-runtime.md#diff-4x-symmmem-ulysses-a2a-transport-与-workspace-生命周期)，
  配置投影见 [VOMNI-CFG-1q](../configuration/rules-diffusion-parallel-transport.md#vomni-cfg-1q-symmmem-ulysses-transport-必须按版本完整投影)。
- CFG parallel state 的未初始化 serial-1 fallback、已初始化错误传播以及 two/N-branch rank
  分派，见 [DIFF-4y](rules-system-runtime.md#diff-4y-cfg-parallel-state-未初始化时串行初始化错误必须可见)。
- 并行度 × 设备容量的启动验收硬规则在
  [Model Executor 规则](../model-executor/rules.md)。
