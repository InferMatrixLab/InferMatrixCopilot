---
title: "MOSS codec 解码性能与数值规则"
created: 2026-10-19
updated: 2026-10-19
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #8002"]
---

# MOSS codec 解码性能与数值规则

## VLLM-OMNI-PR8002-MOSS-CODEC-PERF — MOSS codec 解码优化必须绑定缓存生命周期与逐设备数值证据

- 触发：修改 `vllm_omni/model_executor/models/moss_tts/audio_tokenizer_v2.py` 中 transformer 层的 attention bias（`attn_bias`）构造与跨层共享、`_cached_attn_bias` 缓存，或 `_sa_block`/`_ff_block` 的 residual+layer_scale 融合（如 `torch.addcmul`）。
- 强制：跨层共享的 attention bias 由 block 内第一层计算并显式经 `attn_bias` 参数传给后续层；`_cached_attn_bias` 必须在 forward 结束时清空（含异常中断路径），不得跨 step 持有 tensor。融合算子必须以实际模块类型守卫（`isinstance(..., MossAudioTokenizerLayerScale)`），对 `nn.Identity()`（`layer_scale=None`）回退原 mul+add 路径。数值主张逐设备/精度分别取证、按实测覆盖面表述：bit-identical 主张须在真实 checkpoint 上以 `torch.equal` 验证（本例 mask 共享覆盖 CPU fp32、NPU fp32/bf16）；改变累加顺序的融合（如 addcmul，非 bit-identical）须在真实 checkpoint 上逐设备/精度报告 SNR 或 bit-identity（本例 CPU fp32 SNR 107.1 dB、NPU fp32 bit-identical、NPU bf16 SNR 31.5 dB）。“误差低于该 dtype 自身量化噪声”是更强的额外主张：只须在作出该主张的设备/精度上实测论证，且不得推广为每个设备/精度的统一门槛（本例仅 NPU bf16：融合 vs 手写差值 SNR 31.5 dB，高于 bf16-vs-fp32 的 26.6/27.3 dB 基线；CPU fp32 与 NPU fp32 未做、也不要求做该比较）。
- 禁止：让缓存 bias 在 forward 异常、streaming 每步或 streaming 退出后残留；不设回退地对所有配置启用融合；把改变累加顺序的 `addcmul` 声称为 bit-identical；仅用 CI 小 codec fixture 的数值结果代表真实模型；把“低于量化噪声”的比较当作所有设备/精度的必备证据，或将单一 dtype 上验证过的噪声底结论外推到未测设备/精度。
- 验收：CPU 测试（`tests/model_executor/models/moss_tts/test_audio_tokenizer_v2.py`，`core_model and cpu`）断言非流式、流式、execution-context 驱动、层内抛异常与多步 streaming 后 `_cached_attn_bias is None`；数值等价的 NPU 变体在 `test_audio_tokenizer_v2_npu.py`（`core_model and npu`）覆盖；真实 checkpoint 的逐设备测量结果（本例 CPU fp32、NPU fp32/bf16）写入 PR，噪声底一类结论只限实测过的设备/精度。^[PR #8002]

<!-- kb:rule status=active since=v0.30.0 -->
