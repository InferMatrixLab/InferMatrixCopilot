---
title: "Diffusion parallel transport 配置规则"
created: 2026-09-05
updated: 2026-11-27
type: rule
tags: [vllm-omni, components, config, diffusion]
sources: ["PR #6340", "PR #11109", vllm_omni/config/stage_config.py, vllm_omni/config/omni_config.py, vllm_omni/engine/async_omni_engine.py, vllm_omni/engine/arg_utils.py, vllm_omni/entrypoints/cli/serve.py, tests/config/test_omni_config.py, tests/entrypoints/test_async_omni_diffusion_config.py, tests/entrypoints/test_serve.py]
confidence: high
---

# Diffusion parallel transport 配置规则

## VOMNI-CFG-1q SymmMem Ulysses transport 必须按版本完整投影

- 触发：新增/修改 diffusion 并行 transport、`StageDeployConfig`、`OmniStageDiffusionParallelConfig`、default diffusion stage 或 `serve` CLI。
- `v0.34.0` tag 仍使用 `--ulysses-a2a-permute` / `ulysses_a2a_permute` 布尔字段，省略为 false。旧 true 迁移为 `copy_engine`，要保留旧 false 的 transport 行为则显式选 `nccl`，不能直接省略。[tag CLI](https://github.com/vllm-project/vllm-omni/blob/06a28821a7f72bdb436c3df656b10f67fded0846/vllm_omni/entrypoints/cli/serve.py#L813) · [tag config](https://github.com/vllm-project/vllm-omni/blob/06a28821a7f72bdb436c3df656b10f67fded0846/vllm_omni/diffusion/data.py#L300)
- 从 [PR #11109](https://github.com/vllm-project/vllm-omni/pull/11109) 的 main 合入点起，CLI 参数换成 `--ulysses-a2a-backend {auto,nccl,copy_engine}`，deploy/structured/legacy default-stage 配置使用 `ulysses_a2a_backend`。CLI 未提供时保留 unset，让 deploy 的选择生效；运行时确实省略时才默认 `auto`。不要把旧布尔字段当兼容别名，也不要把内部策略仍使用的布尔变量当公开参数。[新 CLI](https://github.com/vllm-project/vllm-omni/blob/6c30b6eb7f5e69407b5353be4f806e0bfc7a96fb/vllm_omni/entrypoints/cli/serve.py#L822) · [新 config](https://github.com/vllm-project/vllm-omni/blob/6c30b6eb7f5e69407b5353be4f806e0bfc7a96fb/vllm_omni/diffusion/data.py#L325)
- 验收：分别验证相应版本的 CLI、deploy、structured/default-stage 和最终 `parallel_config`；新版本区分显式 `nccl`、显式 `copy_engine` 与 unset，确保未指定的 CLI 不覆盖 deploy。transport 解析、硬件拓扑和生命周期见 [DIFF-4x](../diffusion/rules-system-runtime.md#diff-4x-symmmem-ulysses-a2a-transport-与-workspace-生命周期)。
