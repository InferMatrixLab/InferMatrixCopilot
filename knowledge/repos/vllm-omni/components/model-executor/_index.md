---
title: "Model Executor"
created: 2026-07-10
updated: 2026-10-06
type: index
tags: [vllm-omni, components, model-executor]
sources: []
---

# Model Executor

- [MRv2 optional codec](rules-mrv2-optional-codec.md) — missing attribute 与 eager-MTP replay eligibility。

- 源码入口：`vllm_omni/model_executor/`（layers、model_loader、models、stage_input_processors）、
  `vllm_omni/model_extras/`（模型专有请求/prompt 转换）、`vllm_omni/worker/`（gpu_*_worker、
  gpu_*_model_runner、mixins）、`vllm_omni/inputs/`（runner 输入预处理）和设备平台层
  `vllm_omni/platforms/<cuda|musa|npu|rocm|xpu>/`
- 源码校验：以上路径均已在 `main @ 44d3ae10`（upstream `main` 快照）验证存在；stage 配置已经迁移到
  `vllm_omni/deploy/`，NPU/XPU 等平台可继续拥有自己的 worker 覆盖
- 测试入口：共享 runner 行为看 `tests/worker/`，具体模型 consumer 看 `tests/model_executor/`
- 主要职责：AR/LLM stage、stage 配置、并行与设备启动、runner 到模型的输入预处理合同和跨阶段数据桥接
- NPU trace 的 profiler metric、handler 和证据边界见 [NPU profiler rules](rules-npu-profiler.md)。

## 什么时候查这里

- 调查模型执行 stage、stage config、并行度、设备映射或 worker 启动。
- 修改 runner `preprocess`、`_omni_*` 逐行 metadata、`talker_mtp`、chunked-prefill phase 或共享输入处理合同。
- 调查 AR 到 diffusion 的共享桥接。

## 不放什么

- diffusion denoise loop 的共享实现。
- 某个模型独有的 prompt、checkpoint 或 attention 逻辑。

## 目录内容

| 遇到什么 | 查看哪里 |
|---|---|
| Breeze ROCm RNG fallback、CUDA generator registration或depth-layer Triton autotune | [Breeze平台合同](rules-breeze-rocm.md) |
| 理解共享职责和阶段边界 | [architecture](architecture.md) |
| 根据 PR 描述直达 stage config、runner preprocess、stage runtime、bridge/batch 或 loader 的规则组与第一批源码 | [rules 与代码地图](rules.md) |
| runtime info、跨 stage payload、batch 与 request RNG 合同 | [跨 stage bridge 与 batch 合同](rules-bridge-batch.md) |
| loader 的 dtype/config 获取、fused shard 与多模块 checkpoint 载入 | [loader 合同](rules-loader-contract.md) |
| shared image example envelope 与 model_extras 参数声明 | [image task envelope 合同](rules-image-task-envelope.md) |
| Omni 输出类型、字段/复制合同与V2 native routed-expert auxiliary output | [输出类型合同](rules-output-contract.md)   新增核对：EXEC-7c、EXEC-7d。 |
| 采样循环不变量、热路径缓存、AR 音频侧路、preprocess phase/one-token prefill | [运行时热路径合同](rules-runtime-hot-paths.md) |

| NPU runner、ROCm 分页注意力、NPU 模型补丁 | [平台后端合同](rules-platform-backends.md)   新增核对：EXEC-10f、EXEC-10f2。 |

- [MRv2 attention capture 边界](rules-attention-capture.md) — mixed FULL、separate decode 与当前请求 boundary。

- [release processor/dummy、profiling、state selection 与 auxiliary output：EXEC-RELEASE-1a–1d](rules-release-contracts.md)。

- [显式seed、whole-MTPgraph与NVIDIA/ROCm抽样边界](rules-request-rng.md)。
- [异步output slab、producer事件与storage所有权](rules-output-snapshots.md)。

- [MRv2 aux tree、narrow sampler、sampled embedding 与 generation输出](rules-mrv2-output-contracts.md)。

- [settled row predicate与preemption replay fallback](rules-settled-preprocess.md)。

- [AutoRound NVFP4 method dispatch 合同](rules-autoround-dispatch.md)。
- [数值精确性与平台可移植性规则](rules-numeric-exactness.md)
- [Model Executor — NPU KV cache 布局规则](rules-npu-kv-layout.md)
- [Model Executor — Dynamo/cudagraph compile guard rules](rules-compile-guard.md)
- [Model Executor — INC/AutoRound 量化配置规则](rules-inc-autoround.md)
- [Model Executor — Orchestration Loop Rules](rules-orchestration-loop.md)
- [MOSS codec 解码性能与数值规则](rules-moss-codec-perf.md)
