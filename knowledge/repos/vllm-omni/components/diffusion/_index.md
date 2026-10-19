---
title: "Diffusion"
created: 2026-07-10
updated: 2026-10-09
type: index
tags: [vllm-omni, components, diffusion]
sources: ["PR #5543", vllm_omni/platforms/cuda/platform.py, vllm_omni/diffusion/attention/backends/flashinfer_attn.py]
---

# Diffusion

- [IPC 与客户端错误](rules-ipc-errors.md) — dead-peer send、abort no-op 与单 GPU status 保留。

- 源码入口：`vllm_omni/diffusion/` 全树，含 17 个子模块：attention、cache、diffusion_kv、distributed、executor、hooks、layers、lora、model_loader、models、offloader、postprocess、profiler、quantization、sched、utils、worker
- 源码校验：以上子模块均已在 `main @ 44d3ae10`（upstream `main` 快照）验证存在；MiniMax-H3 的 VAE eager
  ops 仍由模型 owner 维护，其他 shared/模型机制按各自规则审查
- 主要职责：多个 diffusion 模型共用的 pipeline、执行循环、scheduler 接入和运行机制

## 什么时候查这里

- 根因位于共享 diffusion 代码，可能影响多个模型。
- 调查 denoise loop、diffusion runner、scheduler 或共享 attention 执行机制。

## 不放什么

- HunyuanImage3 独有 pipeline、配置和 checkpoint 问题；这些放模型目录。
- 通用 benchmark 方法；这些放 `general/benchmark/`。

## 目录内容

| 遇到什么 | 查看哪里 |
|---|---|
| Wan exact/fused BF16预算、channels_last首帧Conv2d与cache/fallback | [Wan fastpath规则](rules-wan-fastpath.md) |
| FlashInfer FP32 per-tensor scale、TRTLLM SAGE ragged/Smooth-K 与量化环境变量 precedence | [FlashInfer quantization rules](rules-flashinfer-quantization.md) |
| TRTLLM opaque custom-op workspace/fake dtype 与实际 verified execution scope | [TRTLLM execution rules](rules-trtllm-execution.md) |
| AR-Diffusion kernel legal page、ragged sink/recent KV、scratch offset 与固定 block-table width | [AR paging geometry rules](rules-ar-paging-geometry.md) |
| Ray cluster placement、env/RPC transport、bounded teardown 与 idle failure | [Ray executor rules](rules-ray-executor.md) |
| 理解共享职责和数据流 | [architecture](architecture.md) |
| 根据 PR 描述直达 execution parity、checkpoint/artifact identity、quality evidence 或 system-runtime 异常清理规则组与第一批源码、custom-op mutable schema | [rules 与代码地图](rules.md) |
| image/diffusion timing、step normalization、missing-versus-zero 与 benchmark 解释边界 | [metrics evidence rules](rules-metrics.md) |
| vLLM/torch rebase、MoE/quant helper 漂移、kernel backend capability 与 matched accuracy | [upstream 兼容规则](rules-upstream-compat.md) |
| worker process title、拓扑局部 rank、日志前缀与offload worker-local allocator峰值 | [worker observability rules](rules-worker-observability.md) |
| 平台 IR-op priority、Inductor/eager 默认顺序、pipeline compile 准入与模型 hook 合并 | [platform runtime rules](rules-platform-runtime.md) |
| CustomOp XPU platform dispatch、PyTorch-native fallback、CUDA contract 与非同义 override 边界 | [CustomOp dispatch rules](rules-custom-op-dispatch.md) |
| runtime temporary 与 loader-scoped parameter dtype、shared RMSNorm accuracy | [tensor dtype rules](rules-tensor-dtype.md) |
| Wan VAE height/width spatial reshard、empty tail、attention extent | [Wan spatial-shard rules](rules-wan-spatial-shard.md) |
| 完整图像 batch VAE decode、native tiling、uneven/idle rank 与 gather 顺序 | [Batch VAE decode rules](rules-vae-batch.md) |
| multi-DiT、dotted `_dit_modules`、loader-to-offloader handoff 与跨 Cache-DiT/compile/LoRA/offload lifecycle、batch sampling-key 的 provided 标志 | [component lifecycle rules](rules-component-lifecycle.md)   新增核对：DIFF-2ag。 |

| PEFT 与 distilled LoRA、startup fusion、delta/key/alpha、Qwen/Wan transformer mapping、PEFT suspend/resume、零绑定与部分绑定拒绝 | [LoRA rules](rules-lora.md) — DIFF-2ag4 合并并继承 DIFF-2ag2；DIFF-2ag3。 |

| Blackwell FA4、显式/auto CUDA backend、per-role mask/SP pad；local FlashAttention deterministic opt-in、role-aware masked cross-attention、NPU packed mask-free/laser fallback、TRTLLM packed-padding 与 config propagation、equal-rank SP auto-pad fast path、等长 batch padding mask 省略 | [attention rules](rules-attention.md)   新增核对：DIFF-1ai3、DIFF-1aj2、DIFF-1ai4、DIFF-1d2。 |

| SP auto-padding、`mask_sp_padding`、dense/varlen 与 advanced UAA 边界 | [SP padding rules](rules-sp-padding.md) |
| video/audio mux、DLO DP wave、RPC reply ownership/result release、result queue、async pump、SHM ownership、async-output wait 与 shutdown、mid-stream interaction handler | [output/runtime rules](rules-output-lifecycle.md)   新增核对：DIFF-1ai、DIFF-1aj、DIFF-1ai2。 |

| distilled continuous sigma schedule、boundary/step 语义与 modality shift | [sigma schedule rules](sigma-schedules.md) |
| HunyuanImage3 Distil CFG、MeanFlow special-token / scheduler-r 合同 | [HunyuanImage3 HY3-8d](../../models/hunyuan-image3/rules.md) |
| diffusion step 与 request/continuous batching；DIFF-1af BAGEL image step wave、packed state、geometry/CFG admission 与 fallback cleanup | [step and batching](step-and-batching.md) |
| request-wave admission coalescing、stable window、deadline 与 finite config | [admission wait rules](rules-admission-wait.md) |
| paged KV/cache 预算、native/backend/platform 闭环、GQA/Ring/Ulysses layout、FlashInfer plan、能力 metadata、cache_backend None 规范化 | [paged cache 与系统运行时规则](rules-system-runtime.md)   新增核对：DIFF-4ad。 |

| Scheduler-managed diffusion KV 的请求控制面、Hunyuan layout 与未实现边界 | [paged KV control plane](paged-kv-control-plane.md) |
| Cache-DiT、TeaCache 和 prefix cache | [cache acceleration](cache-acceleration.md) |
| TP/PP/SP/CFG/VAE/HSDP 等并行策略 | [parallelism](parallelism.md) |
| 实验性 world-model session 生命周期、LRU 与内存统计边界 | [session state](session-state.md) |
| checkpoint remap、HSDP/FSDP、final-layout HWR、registered HWR mmap DLO transport、component quantization、在线量化加载与 TorchAO `.bin` index fallback | [checkpoint 与加载合同](rules-checkpoint-loading.md) |


- [Diffusion 平台 kernel 与设备合同](rules-platform-kernels.md) 新增核对：DIFF-1ai5。

- [测试替身与类型契约](rules-test-contracts.md) — streaming executor、payload 收窄与实际 CPU/type 门禁。

- [HSDP pre-sharded 权重加载](rules-hsdp-weight-loading.md) — meta-first binding、rank-local I/O 与真实双卡 parity。

- [Attention execution-path 能力](rules-attention-contracts.md) — 有效 metadata、FA4 编译边界与未迁移平台。

shared RMSNorm 的empty-input与fused精度边界沿 tensor dtype rules 的DIFF-NORM-1a核对。

- [registered video SHM 与 borrowed mapping ownership](rules-video-transport.md)。

- [SageAttention3 FP4 dispatch 合同](rules-sage-attention.md)。

- [Diffusion request batch 的 pipeline 合同](rules-request-batch.md)。
- [Diffusion JIT CUDA 构建 include 顺序规则](rules-jit-cuda-includes.md)
- [AR-Diffusion 实时模型集成规则](rules-ar-realtime-integration.md)
- [Diffusion runner peak memory 日志单位规则](rules-memory-unit-labels.md)
