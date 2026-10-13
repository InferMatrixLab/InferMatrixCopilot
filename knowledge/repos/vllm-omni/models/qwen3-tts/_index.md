---
title: "Qwen3-TTS"
created: 2026-07-20
updated: 2026-10-09
type: index
tags: [vllm-omni, models, serving, qwen-omni]
sources: ["PR #5157", "PR #5202", "PR #5608", "PR #6001", "PR #6113", "PR #6553", vllm_omni/entrypoints/openai/serving_speech.py, vllm_omni/entrypoints/openai/tts_adapters/qwen3_tts.py, vllm_omni/model_executor/models/qwen3_tts/prompt_embeds_builder.py, vllm_omni/model_executor/models/qwen3_tts/qwen3_tts_code2wav.py, vllm_omni/model_executor/models/qwen3_tts/segmented_graph_wrapper.py, vllm_omni/model_executor/stage_input_processors/qwen3_tts.py, vllm_omni/model_executor/stage_input_processors/chunk_size_utils.py, vllm_omni/platforms/npu/models/qwen3_tts_tokenizer_v2.py, vllm_omni/platforms/npu/layers/rotary_embedding.py, tests/entrypoints/openai_api/test_serving_speech.py]
confidence: high
---

# Qwen3-TTS

- Public `sample_rate` 只接受 adapter 声明的 24 kHz native 或 8 kHz server-resampled output；
  streaming 状态/header/flush 边界见 [SERV-9b](../../components/serving/rules-speech-output.md#serv-9b-speech-sample_rate-必须由-adapter-capability-限定)。^[PR #6553]

## 名称与范围

- 正式 owner：Qwen3-TTS serving、ref audio、artifact cache、async Code2Wav
  增量解码/状态生命周期、adaptive chunk ramp 与模型专有 NPU patch。事实在 `main @ 4ad455e6`
  复核。
- serving 入口：`vllm_omni/entrypoints/openai/serving_speech.py`。
- NPU tokenizer patch：`vllm_omni/platforms/npu/models/qwen3_tts_tokenizer_v2.py`；共享
  RoPE layout helper 在 `vllm_omni/platforms/npu/layers/rotary_embedding.py`，此 pin 上只
  证明 Qwen3-TTS consumer。
- 共享请求/cache 合同见 [Serving rules](../../components/serving/rules.md)。

## 什么时候查这里

- 审查 `x_vector_only_mode`、ICL、`ref_audio` artifact-only reuse、incremental
  Code2Wav、segmented CUDA Graph、engine 存活性，或 NPU
  tokenizer RoPE 的 BNSD/BSND shape fallback。
- 审查 Qwen3-TTS 的 effective `task_type`、uploaded/precomputed stored voice、
  `tts_model_type`/model-path checkpoint variant 与 Base speaker-embedding mismatch
  admission 时，查 [Qwen3-TTS rules](rules.md) 的 Q3TTS-1d；同页 Q3TTS-3a/3f/3g 与 Q3TTS-4a
  还覆盖 ref publication/重复 callback、processed-token codec validity、stateless mask cache
  和 MRv2 sampling scope。built-in `supported_speakers` 不属于 stored voice。
- 其他源码入口与具体不变量见 Qwen3-TTS rules；模型家族结构见
  [Qwen-Omni](../qwen-omni/_index.md)。

- [当前step prompt projection、requestID cache 与speaker embedding](rules-prompt-preprocess.md)。

- [Qwen3-TTS codec tokens 输出合同](rules-codec-output.md)。
