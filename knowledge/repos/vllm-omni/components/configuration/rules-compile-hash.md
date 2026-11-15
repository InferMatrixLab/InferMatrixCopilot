---
title: "Torch Compile Hash 稳定性规则"
created: 2026-10-26
updated: 2026-11-12
type: rule
tags: [vllm-omni, components, config]
sources: ["PR #8899"]
---

# Torch Compile Hash 稳定性规则

## VLLM-OMNI-PR8899-COMPILE-HASH — torch compile hash 必须排除 per-launch UUID

- 触发：修改会进入 vLLM `ModelConfig` hash factors 的 Omni 字段（`stage_connector_config`、`omni_kv_config` 里的 `connector_config`、含 `wakeup_scope` 等 per-launch `extra` key 的 transfer config）。
- 强制：`OmniModelConfig.compute_hash()` 必须在对象浅拷贝上先用 `connector_hash_factors()`（`vllm_omni/distributed/omni_connectors/utils/config.py`，当前排除集合 `PER_LAUNCH_EXTRA_KEYS = {"wakeup_scope"}`）过滤 `stage_connector_config` 与 `omni_kv_config` 中的 `connector_config`（浅拷贝共享内层 dict，替换前先复制该 dict），再委托 upstream `compute_hash()`；模型实际读取的 connector 设置（如 `extra.codec_chunk_frames`）必须继续改变 hash。把环境变量纳入 inventory（本例 `TORCHINDUCTOR_CACHE_DIR` 归入 PLATFORM_EXTERNAL）必须同步 `environment_variable_inventory.py` 分类与数量快照。
- 禁止：让 per-launch UUID 留在 compile cache key 中，使重复启动本应命中的编译与后续依赖 `TORCHINDUCTOR_CACHE_DIR` 的启动期编译（如 code predictor）出现不必要的 cache miss；为求 hash 稳定直接 mutate `self` 或过滤掉模型实际读取的 connector 设置；纳入/调整 inventory 环境变量而不更新分类与快照计数。
- 验收：用真实 deploy YAML（qwen3_tts）对 stage 0/1 各构建两次 `OmniEngineArgs.create_engine_config().compute_hash()`，断言同一配置两次 hash 相同；再断言改变 `codec_chunk_frames` 等 connector extra 仍产生不同 hash（`tests/engine/test_arg_utils.py`）；env inventory 分类与数量快照同步通过（PLATFORM_EXTERNAL 30→31）。^[PR #8899]

<!-- kb:rule status=active since=v0.30.0 -->

模型侧直接 `torch.compile` 的首次执行日志由 [Model Executor 编译统计规则](../model-executor/rules-compile-stats.md) 负责。
