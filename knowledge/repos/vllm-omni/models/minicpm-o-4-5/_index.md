---
title: "MiniCPM-o 4.5"
created: 2026-07-20
updated: 2026-11-26
type: index
tags: [vllm-omni, models, model-executor]
sources: ["PR #3642", "PR #5382", "PR #5524", "PR #5638", "PR #5792", "PR #5869", "PR #6056", "PR #6154", "PR #6170", "PR #6318", "PR #6404", "PR #6678", "PR #6628", tests/dfx/perf/tests/test_minicpmo_4_5.json, tests/dfx/perf/tests/test_minicpmo_4_5_duplex_seed_tts.json, tests/e2e/accuracy/minicpmo_4_5/test_minicpmo_4_5.py, tests/engine/duplex/test_duplex_deploy_config.py, tests/e2e/online_serving/helpers/minicpmo_4_5_duplex.py, tests/e2e/online_serving/test_minicpmo_4_5_duplex.py, tests/e2e/online_serving/test_minicpmo_4_5_duplex_expansion.py, vllm_omni/experimental/fullduplex/client.py, vllm_omni/model_executor/models/minicpmo_4_5/duplex/stage0.py, vllm_omni/entrypoints/duplex/realtime_input.py, vllm_omni/model_executor/models/cosyvoice3/code2wav_core/hifigan.py, vllm_omni/model_executor/models/minicpmo_4_5/, tests/model_executor/models/minicpmo_4_5/test_cuda_graph_wrapper.py, tests/model_executor/models/minicpmo_4_5/test_cfm_graph_capture_gating.py, tests/model_executor/models/minicpmo_4_5/test_talker_batching.py, vllm_omni/platforms/npu/models/minicpmo_4_5_code2wav.py, "PR #6082", "PR #6587", "PR #8007", "PR #8332", "PR #8443", "PR #8462", "PR #8222", "PR #10595"]
confidence: high
---

# MiniCPM-o 4.5

## 名称、源码与部署

- 正式名称：MiniCPM-o 4.5；仓库目录/注册 key 使用 `minicpmo_4_5`。
- 模型：`vllm_omni/model_executor/models/minicpmo_4_5/`，包含 LLM、TTS wrapper 和
  pipeline；输入处理在 `model_executor/stage_input_processors/minicpmo_4_5_omni.py`。
- 配置与入口：`config/pipeline_registry.py`、`deploy/minicpmo_4_5.yaml`、
  `minicpmo_4_5_2gpu.yaml`、`minicpmo_4_5_3gpu.yaml`、`minicpmo_4_5_8x4090.yaml`。
- 共享 owner：[Model Executor](../../components/model-executor/_index.md)、
  [Config](../../components/configuration/_index.md) 和 [Serving](../../components/serving/_index.md)。

## 版本与 stage 边界

MiniCPM-o 多个版本共享通用 `MiniCPMO` architecture 名称，4.5 不能仅靠 architecture
集合相交识别，必须结合 config/version predicate。4.5 pipeline 把 LLM/thinker 结果通过
runtime bridge 交给 TTS stage，再包装为 `OmniOutput.multimodal_outputs`；deploy 变体改变
资源拓扑，不改变数据合同。

Code2Wav 在所有平台使用树内 `MiniCPMO45Token2wav`。CUDA 可显式开启 DiT estimator +
Campplus TensorRT；这是可关闭的局部加速，不替换 encoder/HiFT，也不会自动启用
Step-Audio2 的 token2wav。engine cache/profile 与 fallback 门禁见 MCPMO-1c。

在支持的 CUDA profile，HiFT graph 捕获 pre-iSTFT 子图，按 connector chunk/cache shape 预捕并限量
lazy capture；Stage 2 outer `enforce_eager` 不控制这些 inner graph。Ascend 路径按 MCPMO-1f 验证，
不可概括为所有非 CUDA 都回 eager。
HiFT/CFM 的 shape、显存、并发、整代退休与部分-graph 性能证据边界见
[Code2Wav CUDA graph 规则](rules-cuda-graphs.md)。 新增核对：MCPMO-1k；legacy step-level 与 Whole-Euler 必须按实际路径区分。

Thinker 的 Whisper/APM audio encoder 仍构造 dense `[B,1,T,T]` mask；chunk mask 已用
broadcasted query/key index 代替逐 row Python fill，但不改变 chunk/left-context/lookahead
边界，也不消除 O(T²) storage。语义与证据门禁见 MCPMO-2b。

Talker 只收集 sampling-eligible active rows 批量执行 codec projection 与 logit transforms，
同时保留逐请求 generator 的 row-wise multinomial；request alignment、terminal 与 workspace
边界见 MCPMO-3c。

Daily-Omni、Seed-TTS simplex/realtime duplex 已进入 accuracy/perf harness；阈值、逐轮 metrics、
单卡 H100 perf 与“baseline 不是 regression gate”的证据边界见 MCPMO-5a。

Online serving CI 的 core suite 固定 async chunk，expansion 分开覆盖 sync/async，native duplex
fixture 不再覆盖 eager/KV cap 并固定 active-speech turn window；验证证据边界见 MCPMO-5b。

描述直达源码与模型专有门禁见 [rules](rules.md#direct-代码快速入口)；并发 duplex 的 ragged
Talker chunk、full-attention 容量 rollover、shipping profile 与 chat 隔离见 [native duplex 规则](rules-duplex.md) 新增核对：MCPMO-4j。
；Code2Wav
state、batch cap 与 NPU residual limits 见 [Code2Wav 并发批处理规则](rules-code2wav-batching.md)。新模型语义验证见
[model validation](../../review/guides/model-validation.md)。

Seed-TTS chat reference audio 的 request lookup、private prompt preservation、Talker buffer 与
Code2Wav per-field fallback 见 MCPMO-3e；它不覆盖 realtime。^[PR #6628]

Realtime video 不以「每 200 ms append」定义 frame cadence：首个约 1030 ms、以后每秒关闭的 Stage 0 unit 才接收对应 camera frame；双图 stacking 与外部 WAV 覆盖 soundtrack 的边界见本页已链接的 native duplex 规则 MCPMO-4f。^[PR #6404]

## 什么时候查这里

- 审查 MiniCPM-o 4.5 registry、remote-code gate、TTS dependency、Code2Wav TensorRT/HiFT/CFM graph、
  batch/stage handoff 或 native duplex session。
- 问题位于共享 bridge/batching 时转到
  [Model Executor rules](../../components/model-executor/rules.md)。

- [MiniCPM-o 4.5 Code2Wav 运行时合同](rules-code2wav-runtime.md)：`MCPMO-1f`, `MCPMO-1g`, `MCPMO-1h`, `MCPMO-1i`。

- [rules-loader](rules-loader.md) 新增核对：MCPMO-5a2。

- [Whole-Euler 与 resident graph 合同](rules-resident-graphs.md)：MCPMO-GRAPH-1a–1f，涵盖显式 backend、共享 arena、总预算、ragged fallback、ordinary TF32 与 exact continuation 预捕。
- [输入 encoder graph 合同](rules-encoder-graphs.md)：MCPMO-ENCODER-1a–1f，区分 stateless exact-shape、streaming audio startup graph 与 packed-vision lazy manager，保持 packed batch、buffer 所有权、admission、fbank exactness 与逐行 RNG。
- camera tile 与 first-append reservation 查本页已链接的 native duplex 规则 MCPMO-DUPLEX-TILE-1a。

- [MRv2 turn 与输出状态](rules-mrv2-state.md)：MCPMO-MRV2-1a–1d，区分 native V1 duplex、live row ledger、codec sampling、共享 V1 hooks 与显式 cached ISTFT。
- [native LISTEN speech unit的turn end](rules-duplex-turn-closure.md)。
