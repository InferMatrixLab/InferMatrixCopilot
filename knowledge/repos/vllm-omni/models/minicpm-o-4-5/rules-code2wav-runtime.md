---
title: "MiniCPM-o 4.5 Code2Wav 运行时合同"
created: 2026-07-20
updated: 2026-11-26
type: rule
tags: [vllm-omni, models, model-executor]
sources: ["PR #3642", "PR #5165", "PR #5382", "PR #5524", "PR #5638", "PR #5792", "PR #5869", "PR #6056", "PR #6154", "PR #6170", "PR #6318", "PR #6828", tests/dfx/perf/tests/test_minicpmo_4_5.json, tests/dfx/perf/tests/test_minicpmo_4_5_duplex_seed_tts.json, tests/e2e/accuracy/minicpmo_4_5/test_minicpmo_4_5.py, tests/e2e/online_serving/helpers/minicpmo_4_5_duplex.py, tests/e2e/online_serving/test_minicpmo_realtime_duplex_drivers.py, tests/e2e/online_serving/test_minicpmo_4_5.py, tests/e2e/online_serving/test_minicpmo_4_5_duplex.py, tests/e2e/online_serving/test_minicpmo_4_5_expansion.py, tests/e2e/online_serving/run_minicpmo_realtime_duplex_soft_interrupt.py, vllm_omni/benchmarks/data_modules/seed_tts_dataset.py, vllm_omni/benchmarks/data_modules/seed_tts_eval.py, vllm_omni/benchmarks/patch/patch.py, vllm_omni/deploy/minicpmo_4_5.yaml, vllm_omni/experimental/fullduplex/client.py, vllm_omni/entrypoints/duplex/chat_fallback.py, vllm_omni/entrypoints/duplex/realtime_input.py, vllm_omni/entrypoints/duplex/session_runner.py, vllm_omni/entrypoints/duplex/serving.py, vllm_omni/entrypoints/duplex/vad.py, vllm_omni/model_executor/models/minicpmo_4_5/duplex/adapter.py, vllm_omni/model_executor/models/cosyvoice3/code2wav_core/hifigan.py, vllm_omni/model_executor/models/minicpmo_4_5/batched_token2wav.py, vllm_omni/model_executor/models/minicpmo_4_5/cuda_graph_wrapper.py, vllm_omni/model_executor/models/minicpmo_4_5/minicpmo_4_5_code2wav.py, vllm_omni/model_executor/models/minicpmo_4_5/minicpmo_4_5_omni_llm.py, vllm_omni/model_executor/models/minicpmo_4_5/minicpmo_4_5_omni_tts.py, vllm_omni/model_executor/stage_input_processors/minicpmo_4_5_omni.py, tests/model_executor/models/minicpmo_4_5/test_audio_chunk_mask.py, tests/model_executor/models/minicpmo_4_5/test_cfm_graph_capture_gating.py, tests/model_executor/models/minicpmo_4_5/test_code2wav_batching.py, tests/model_executor/models/minicpmo_4_5/test_cuda_graph_wrapper.py, tests/model_executor/models/minicpmo_4_5/test_pipeline.py, tests/model_executor/models/minicpmo_4_5/test_talker_batching.py, tests/model_executor/models/minicpmo_4_5/test_vision_flash_attention.py, "PR #6082", "PR #5604", "PR #6274", "PR #6346", "PR #6397", "PR #6406", "PR #6458", "PR #6587", "PR #6619", "PR #6757", "PR #6529", "PR #6772", vllm_omni/entrypoints/duplex/protocol.py, "PR #10595"]
confidence: high
---

# MiniCPM-o 4.5 Code2Wav 运行时合同

入口与其他规则见 [共享规则](rules.md)。

## MCPMO-1f Ascend Code2Wav 保持 outer eager 按实际 inner graph 路径验收

- 触发：MiniCPM-o 4.5 在 Ascend 上修改 Code2Wav、CFM estimator、NPUGraph 开关、图缓存或 bundled deploy profile。
- 强制：Stage 2 的 runner 保持 `enforce_eager: true`；这不关闭模型内部的设备 graph。早期 #5604 的 platform exact-step 路径只捕获 `blocks_forward_chunk`，cached/uncached shape 分桶；含 #10595 的 runtime 可在 NPU 上使用共享 CFM Whole-Euler/step wrapper，也可分别启用 Flow encoder 和 HiFT 子图。按当前 dispatch 区分这些路径：支持时在模型内部捕获有界子图，请求解析、request-owned state 提交和未捕获部分留在外层。带 capture 的 NPU runtime 仍需在 Token2Wav 加载前配置 `allow_internal_format=False`、`jit_compile=False` 并拒绝 `ASCEND_LAUNCH_BLOCKING=1`；SDPA 限制按实际路径验证；启用且设备为 NPU 时，缺少所需 graph API 可使加载报错，不是未命中 bucket 时的 eager 回退。
- 禁止：把 #5604 的 step-only、encoder/HiFT 全 eager 或默认 graph cap 当成后续版本的通用合同；把关闭 CFM graph 当成同时关闭独立的 encoder/HiFT 图；为已达到 cap、未命中 bucket 或未通过资格校验的请求强制 replay；捕获整个 Stage 2，或把部件 parity/速度外推为端到端结果。
- 验收：先按 [CONF-5e/CONF-5g](../../components/configuration/rules-topology-profiles.md) 检查最终配置；再分别断言 CFM/encoder/HiFT 的开关、有效输出及下一 chunk 使用的 cache，覆盖 graph 关闭、边界 shape、cached/uncached、超预算、未被接纳及各路径的 capture 失败策略。对支持的 Ascend profile 比较 graph 与 eager；只有同硬件、同 profile、同 checkpoint 和 workload 的端到端测量才支持服务性能结论。区分 Ngram/BF16 与可选 DSpark/W8A8，不把组件资格测试或单轮历史数据作当前版本的端到端证明。 ^[PR #5604] ^[PR #10595]
- 版本证据：[合入树的 CFM 初始化与 dispatch](https://github.com/vllm-project/vllm-omni/blob/94de758f0678bb8d5e770349c463f1f267a91f20/vllm_omni/model_executor/models/minicpmo_4_5/code2wav/token2wav.py#L638-L758)、[平台开关与加载条件](https://github.com/vllm-project/vllm-omni/blob/94de758f0678bb8d5e770349c463f1f267a91f20/vllm_omni/platforms/npu/models/minicpmo_4_5_code2wav.py#L370-L433)。

## MCPMO-1g — Token2Wav 的 eager TorchDynamo 包装必须在流式热路径中移除

- 触发：MiniCPM-o 4.5 的 Token2Wav/CosyVoice 流式路径使用 `torch.compile(backend="eager")` 包装 `forward_chunk`，并在新 chunk shape 上出现首响应停顿。
- 强制：在 `BatchedToken2Wav` 初始化时，仅当方法存在 `_torchdynamo_orig_callable` 或 `__wrapped__` 时恢复原始 bound method；保留 estimator、encoder、HiFT 的其他路径及其运行时语义。
- 禁止：让 eager backend 的 tracing/guard 构造进入每个未见 chunk shape 的实时响应；把该解包扩大为所有后端、模型或 CUDA Graph 路径的通用优化。
- 验收：检查存在包装时初始化后 `forward_chunk` 已指向原始实现、无包装时保持不变；用 cold 与 warm 的多 chunk duplex 回归确认首个响应不再长时间停顿、后续响应持续产出音频，且 steady-state 行为未被改变。 ^[PR #6274]

## MCPMO-1h — Code2Wav 编码窗口必须受 RelPos 与缓存预算约束

- 触发：修改 MiniCPM-o Code2Wav 的长 codec prefill、`forward_chunk`、RelPos PE、upsample 或 conformer cache。
- 强制：按 PE `max_pos`、upsample stride、cache offset 和 lookahead 计算安全 token 窗口，限制单片上限并切分超长输入；非最终片保留 lookahead，调用 `forward_chunk` 前确保 PE 足够，按 batch 行合并音频并延续 cache。
- 禁止：把数千 codec token 一次送入固定 RelPos PE；忽略 cache offset 或把非最终短片静默截断；用空音频掩盖窗口溢出。
- 验收：单测覆盖预算计算与切片计划，fake Code2Wav 测试断言每片大小、`last_chunk` 和非空音频；不得从该修复推断通用性能提升。 ^[PR #6346]

## MCPMO-1i — Code2Wav dynamo 解包必须兼容无 encoder 的 Flow

- 触发：MiniCPM-o 4.5 `BatchedToken2Wav` 初始化需要对 `flow.encoder.forward_chunk` 执行 TorchDynamo 解包，但某些 decoder/estimator-only Flow 没有 `encoder`。
- 强制：先以 `getattr(self.flow, "encoder", None)` 检查 encoder；仅在 encoder 存在时解包 `forward_chunk`，真实 CosyVoice flow 仍必须保留原有解包行为。
- 禁止：无条件访问 `self.flow.encoder`，或让 encoder-less 的 NPU Code2Wav 图测试与其他合法 Flow 在构造阶段因 `AttributeError` 失败。
- 验收：decoder/estimator-only `_Flow` 能成功构造并完成 NPU graph/eager 回归；带 encoder 的 CUDA MiniCPM-o 4.5 路径仍确认 `forward_chunk` 的 Dynamo 包装被正确移除。 ^[PR #6397]

