---
title: "Model Executor — 模型侧惰性编译统计规则"
created: 2026-11-12
updated: 2026-11-12
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #8899"]
---

# Model Executor — 模型侧惰性编译统计规则

## VLLM-OMNI-PR8899-COMPILE-STATS — 模型侧编译统计必须包住首次执行而非惰性包装

- 触发：模型在 vLLM compile wrapper 之外直接调用 `torch.compile`，或修改共享 `vllm_omni/utils/torch_compile.py::log_torch_compile_stats` 与调用方的首次执行/warmup 边界。
- 强制：`torch.compile` 是惰性的，使用 `log_torch_compile_stats(logger, label)` 包住真正触发编译的首次调用块；共享 code predictor 的 `_setup_compile` 在启用 Inductor 的路径上以 `_warmup_buckets()` 为该边界。日志记录该 block 的耗时及 Inductor FX graph cache hit/miss/bypass 增量，而不是进程累计计数。边界以 [合并实现](https://github.com/vllm-project/vllm-omni/blob/0bce034b7bbe3d9937103300bc794e6b4620d0b6/vllm_omni/utils/torch_compile.py) 与 [code predictor 调用方](https://github.com/vllm-project/vllm-omni/blob/0bce034b7bbe3d9937103300bc794e6b4620d0b6/vllm_omni/model_executor/models/common/qwen3_code_predictor.py#L930-L932) 为依据。
- 禁止：只包住 `torch.compile(...)` 包装器创建就声称获得编译统计；把已有进程计数当作当前 warmup 的 cache hit/miss，或把一次 workload 的命中与启动耗时推广为所有模型的性能保证。
- 验收：沿真实调用链确认统计上下文覆盖首次执行/warmup；核对日志同时包含 label、block 耗时与三类 counter 增量，且已有 counter 值不会被计入该 block。需要性能结论时，另附固定版本、模型与 cache 状态的冷/热启动证据；源码审查不能替代运行结果。^[PR #8899]

<!-- kb:rule status=active since=v0.30.0 -->

compile key 的 per-launch 字段过滤见 [Configuration hash 规则](../configuration/rules-compile-hash.md)；环境变量读取的分类与快照同步见 [Configuration 环境变量合同 VOMNI-CFG-3a](../configuration/rules.md)。
