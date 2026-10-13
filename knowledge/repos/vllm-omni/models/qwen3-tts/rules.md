---
title: "Qwen3-TTS 规则"
created: 2026-07-20
updated: 2026-10-09
type: rule
tags: [vllm-omni, models, serving, qwen-omni]
sources: ["PR #5157", "PR #5202", "PR #5608", "PR #6001", "PR #6113", "PR #6523", "PR #6728", "PR #6861", vllm_omni/deploy/aura_omni.yaml, vllm_omni/deploy/qwen3_tts.yaml, vllm_omni/deploy/qwen3_tts_high_concurrency.yaml, vllm_omni/model_executor/models/aura_omni/pipeline.py, vllm_omni/model_executor/models/qwen3_tts/qwen3_tts_code2wav.py, vllm_omni/model_executor/models/qwen3_tts/prompt_embeds_builder.py, vllm_omni/model_executor/models/qwen3_tts/segmented_graph_wrapper.py, vllm_omni/model_executor/models/qwen3_tts/tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py, vllm_omni/model_executor/stage_input_processors/chunk_size_utils.py, vllm_omni/entrypoints/openai/serving_speech.py, vllm_omni/entrypoints/openai/serving_speech_stream.py, vllm_omni/entrypoints/openai/speech_usage.py, vllm_omni/entrypoints/openai/tts_adapters/qwen3_tts.py, vllm_omni/model_executor/stage_input_processors/qwen3_tts.py, tests/e2e/online_serving/test_qwen3_tts_base.py, tests/e2e/online_serving/test_qwen3_tts_base_expansion.py, tests/entrypoints/openai_api/test_serving_speech.py, tests/entrypoints/openai_api/test_serving_speech_stream.py, tests/entrypoints/openai_api/test_tts_adapter.py, tests/model_executor/models/qwen3_tts/test_qwen3_tts_code2wav.py, tests/model_executor/models/qwen3_tts/test_qwen3_tts_incremental_decode.py, tests/model_executor/stage_input_processors/test_qwen3_tts_async_chunk.py, "PR #5048", "PR #6639", "PR #7781", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/models/qwen3_tts/qwen3_tts_talker.py", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/stage_input_processors/qwen3_tts.py", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/models/qwen3_tts/tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/models/qwen3_tts/test_qwen3_tts_talker_ref_codes.py", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/stage_input_processors/test_qwen3_tts_async_chunk.py", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/models/qwen3_tts/test_qwen3_tts_talker_silence_ban.py", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/models/qwen3_tts/test_qwen3_tts_stateless_capture.py"]
confidence: high
---

# Qwen3-TTS 规则

只有 `Q3TTS-数字字母` 是可审计规则 ID。

## Direct 代码快速入口

| PR 描述信号 | 规则组 | 第一批源码 |
|---|---|---|
| Qwen3-TTS、`qwen3_tts` pipeline | Q3TTS-1a/1b | `config/pipeline_registry.py::OMNI_PIPELINES["qwen3_tts"]`；`model_executor/models/qwen3_tts/pipeline.py` |
| `ref_audio`、stored voice、task/checkpoint variant、x-vector、ICL、artifact-only reuse | Q3TTS-1a/1b/1c/1d | `entrypoints/openai/tts_adapters/qwen3_tts.py::{validate,_get_model_variant}` → `entrypoints/openai/serving_speech.py::_build_tts_params`、`_qwen3_tts_can_use_ref_audio_artifact_only`、`_track_ref_audio_artifact_warmup`、`_mark_ref_audio_artifact_ready_for_request` |
| talker/code2wav、native codec validity、重复 callback、implicit-position mask cache、adaptive chunk、segmented graph | Q3TTS-3a/3b/3c/3d/3e/3f/3g + Model Executor | `stage_input_processors/qwen3_tts.py::talker2code2wav_async_chunk` → `stage_input_processors/chunk_size_utils.py` → `qwen3_tts_code2wav.py::Qwen3TTSCode2Wav` → `segmented_graph_wrapper.py` |
| OpenAI speech adapter | Q3TTS-1a/1b/1d + Serving | `entrypoints/openai/tts_adapters/qwen3_tts.py::Qwen3TTSAdapter` → `serving_speech.py` |
| NPU、RoPE、BNSD/BSND、`codec_chunk_ramp` | Q3TTS-2a | `platforms/npu/models/qwen3_tts_tokenizer_v2.py::_apply_rotary_pos_emb_npu` → `platforms/npu/layers/rotary_embedding.py::npu_rotary_mul_with_bsnd_fallback` |

## Q3TTS-1a — ref-audio readiness 按 mode/capability 隔离

- 触发：同一 `ref_audio` 在 x-vector-only 与 ICL 请求之间复用 artifact。
- 强制：x-vector-only artifact 只证明存在 speaker embedding，不能满足需要 `ref_code`
  的 ICL；ICL 请求遇到这种 artifact 时必须保留原始 audio 并重新计算。
- 禁止：仅按 `artifact_key` 标 ready 后剥离 `ref_audio`；worker 此时既没有 `ref_code`
  也没有输入可补算，会把请求错误升级为 engine failure。
- 验收：同一 audio 的 x-vector → ICL 顺序重新计算且 server 存活；x-vector → x-vector
  与 ICL → ICL 仍命中 artifact-only reuse。 ^[PR #5157]

## Q3TTS-1b — ICL 是能力超集，是否反向复用必须显式取舍

- 触发：用 `(artifact_key, x_vector_only)` 等 exact-mode key 简化 readiness。
- 强制：记录 ICL artifact 同时含 `ref_code` 与 speaker embedding，因此理论上可满足
  x-vector 请求；若选择 exact-mode 隔离，应把首次 ICL → x-vector 的一次重算视为已知
  非阻塞性能代价。
- 禁止：把该重算描述为正确性要求，或在未 profiling 前增加复杂 capability 状态机。
- 验收：顺序测试证明 ICL → x-vector 只多一次重算、输出正确且后续同模式复用恢复；
  若优化为 capability predicate，保留 x-vector artifact 不能服务 ICL 的单向边界。 ^[PR #5157]

## Q3TTS-1c — ref-audio artifact 回归必须保留 ready-gate 证明

- 触发：移动、合并或重标记同一 `ref_audio` 的 x-vector → ICL artifact regression test。
- 强制：ready lane 必须仍选择 async-chunk server 上的该顺序；每步断言非空 audio，并在 ICL
  后再发一次 x-vector 请求以证明 engine 存活。full-model expansion 可以补充覆盖，不能替代
  `core_model`/`advanced_model` ready-gate case。
- 禁止：只因测试正文仍在 expansion 文件，就声称 ready 已覆盖；被 marker selector 排除的
  `full_model` case 不构成 ready regression evidence。
- 验收：ready 的实际 marker selector 收集并执行该 case；x-vector → ICL → x-vector 在同一
  audio 下通过，且在旧的 mode-agnostic artifact readiness 实现上失败。 ^[PR #5157] ^[PR #6523]

## Q3TTS-1d — effective task 必须在 dispatch 前与 checkpoint variant 对齐

- 触发：修改 Qwen3-TTS Speech API 的 `task_type` 推断、uploaded/precomputed voice、checkpoint
  variant 识别，或 Base prompt 的 speaker embedding 拼接。
- 强制：先确定 effective task：未显式 task 的 `ref_audio`/`ref_text` 选择 Base；uploaded voice 或
  `capabilities.precomputed_speakers` 中的 voice 无条件选择 Base，即使 caller 给了另一 task；
  `capabilities.supported_speakers` 的 built-in preset 仍是 CustomVoice。再以
  `hf_config.tts_model_type` field-first 解析已识别 variant；field 缺失或未识别时，才从 model path
  的 leaf 向上逐 component 查找 token-delimited marker。识别到 variant 且与 effective task 不同，
  必须在 engine dispatch 前返回 public 400，明确 task、loaded variant 和可用 alternative；无法识别
  variant 返回 `None` 并 fail-open。
- 禁止：在 validation 后才把 stored voice 改写为 Base；读取 `server.precomputed_speakers` 而不是
  adapter capability；把 `base_models`、`database` 或 parent substring 当 Base marker；让已知
  task/variant mismatch 进入 prompt builder。prompt builder 的 speaker-dimension guard 仅保留为绕过
  Speech API 时的 diagnostic，仍可能使 EngineCore 终止，不得把它宣传为 service-preserving admission。
- 验收：focused tests 覆盖 uploaded 与 precomputed stored voice（包括 caller 的 conflicting task）、
  built-in speaker、metadata priority over conflicting/uninformative path、unknown/unrecognized metadata 的
  token-delimited leaf-up fallback 与 unknown fail-open，以及三种 known task/variant mismatch 在 engine
  dispatch 前为 public 400；另验证 prompt-builder dimension mismatch diagnostic。 ^[PR #6113]

## Q3TTS-2a — NPU RoPE 按真实 shape 在 BNSD 与 BSND fused path 间选择

- 触发：Qwen3-TTS 12 Hz tokenizer decoder 的 NPU RoPE，尤其启用短
  `codec_chunk_ramp` 且提高并发时的短序列 decode。
- 强制：`unsqueeze_dim=1` 的输入合同是 rank-4 BNSD `[B,N,S,D]`。只有
  `D` 为偶数、element size 有效且整除 32、`(D/2) % (32/element_size) == 0`，并且
  `B*N <= S*8` 时，才保持 BNSD fused fast path，cos/sin 在 axis 1 扩维。
- 强制：合法 rank-4 输入不满足上述 BNSD predicate 时，把 hidden states 以
  `transpose(1,2).contiguous()` 转成 BSND `[B,S,N,D]`，cos/sin 在 axis 2 扩维后调用同一 fused
  `torch_npu.npu_rotary_mul`，再 transpose 回 `[B,N,S,D]`。q 和 k 必须使用同一 helper；
  `unsqueeze_dim != 1` 的既有调用保持直接 fused 路径。
- 禁止：让短序列/高并发 shape 继续进入会拒绝它的 BNSD tiler；也不能为规避崩溃而让
  所有 shape 都承担 BSND transpose，丢掉已支持 shape 的 BNSD fast path。共享 helper 的
  路径是模型无关的，但此 pin 只证明 Qwen3-TTS tokenizer consumer，不能外推到其他模型。
- 验收：覆盖 `B*N == S*8` 不等式边界两侧、不同 dtype/alignment、rank-4 输入合同、非默认
  `unsqueeze_dim`，并把 BNSD fast path 与 BSND fallback 的 q/k 结果同 eager RoPE 做数值
  parity。当前测试只把一个 unsupported shape 的 cos/sin dispatch 期望改为 BSND axis，
  尚无专门的 supported/unsupported 边界矩阵或 eager 数值 parity。 ^[PR #5608]

## Q3TTS-3a — async connector 只在首块传 prefix，后续只传 delta

- 触发：`async_chunk=true` 的 Talker→Code2Wav 分块、ICL reference code、
  x-vector-only、chunk ramp、KV-resumed prefill 或重复 chunk callback。
- 强制：首块包含有界 ICL ref prefix 和已完成 codec frames；后续块只传
  新完成 frames，`left_context_size=0`，依赖 Code2Wav 按 request 维护
  quantizer/conv/Transformer 状态。首个也是最后一个的短请求必须一次
  发出全长；空的 finished payload 仍携带 request ID 以便清理。
- 强制：async Code2Wav 缺 request ID 必须 fail fast；首个 ICL 状态若声明
  `ref_context_size` 却没有实际 prefix，也必须拒绝。非 async/stateless 路径
  继续传全序列，不要求 request ID。
- 强制：首次可用 reference codes 按请求只发布一次，包括 KV-resumed prefill；无 ref 的请求
  保留 batch-aligned 空 entry。consumer 保留 ref 供首次 decoder 初始化。重复 callback 不重发
  已发 frames；terminal 只 flush 未发余帧，没有余帧仍发送带 request ID 的 empty-finished sentinel。
- 禁止：用“当前是否 prefill”替代 ref 发布状态；因重复完成回调重放最后一个 chunk，或把另一
  请求的 ref 填入空 slot。
- 验收：ICL/xvec、ramp/固定 initial chunk、首块即 final、空 EOF、丢失 request ID/ref prefix；
  KV-resumed 输出连续调用两次只发布一次 ref；重复 nonterminal、整块边界 EOF、partial final、
  再次 terminal 和 mixed-ref batch 均保持帧与 ref 请求归属正确。^[PR #5202] ^[PR #7781]
  回归入口：[ref publication](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/models/qwen3_tts/test_qwen3_tts_talker_ref_codes.py#L109-L145)、
  [callback/EOF](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/stage_input_processors/test_qwen3_tts_async_chunk.py#L39-L73)。

## Q3TTS-3b — decoder state 必须以 scheduler ID 定位且在所有终止边界释放

- 强制：正常 runner 调用用 `request_ids` 中的 scheduler/internal ID 作 cache key，
  因为 `on_requests_finished()` 接收的是同一 ID；payload/external `meta.request_id`
  仅作 direct-forward 与 CUDA Graph dummy run 没有 runner ID 时的 fallback。
- 强制：正常 `finished`、resumable `is_segment_finished`、空/无效最后载荷
  和 scheduler abort 都要清理；segment 边界后不得继承上一段 KV/conv/
  quantizer 状态。dummy ID 需显式 `_is_dummy_run`；decoder dummy path 与正在
  进行的外层 CUDA Graph capture 必须走 exact decode，且 dummy replay/fallback
  不得污染运行统计。
- 边界：`_decoder_state_cache_warn_entries=512` 是 warning threshold，不是容量
  上限；为保持 active request 正确性不做 LRU eviction。因此 ID mapping 与
  abort cleanup 是防止 GPU state 无界增长的必要合同，不能用“有 512 上限”
  掩盖泄漏。
- 验收：internal/external ID 不同的 abort、finished/segment-finished、dummy direct call、
  空/非法 row 并存的 batch。目标 pin 没有专门让 Code2Wav cache 在
  `is_segment_finished` 后清空的 focused test，该边界仍需补齐。 ^[PR #5202]

## Q3TTS-3c — compacted batch 必须保持原 request slot，输出长度归 decoder

- 强制：有状态请求按 mode（ICL/xvec）、phase（first/growing/rolling）、
  target suffix shape 分组，再恢复到原 request slot；一个 right-padded
  `[B,Q,F]` 输入必须同时传真实 lengths 和对齐的 state list。跳过空/
  malformed row 后的 stateless leading-context trim 必须经 `valid_indices[row]`
  回到原 batch 位置，不得用 compacted row 直接索引 metadata。
- 强制：decoder 返回每请求精确长度的 waveform list；stateless 分支移除
  right padding/内部 overlap，Code2Wav 只再移除 scheduler 声明的 leading ref
  context。这一 stateless 合同也影响使用 `Qwen3TTSCode2Wav` 的 Aura-Omni，
  不能只测 Qwen3-TTS async 路径。
- 验收：覆盖 empty/malformed row 与有效 ICL row 混合、variable prefix
  left-padding mask、mixed-phase slot restoration，以及 stateless ref-audio leak/
  short-row tail 回归。^[PR #5202]

## Q3TTS-3d — eager 与 CUDA Graph 必须共用可达 phase 合同

- 触发：修改 segmented graph、first/growing/rolling phase、chunk ramp、capture
  shape/batch bucket 或 eager fallback。
- 强制：无 ramp 时 phase 从 `initial_codec_chunk_frames`、`codec_chunk_frames`、
  decoder `sliding_window` 派生；有 ramp 时用实际 delta。async 忽略
  `decode_cudagraph_capture_sizes`；stateless 用默认 shape 与配置额外 shape 的
  **union**。`decode_cudagraph_batch_sizes` 只限 graph bucket，`decode_batch_max_size`
  只拆 eager stateful group（0 表示不限），两者不可混同。
  旧 `decode_compile_shapes`/`decode_batch_bucket_frames` 已不再被 Qwen3 Code2Wav
  消费，不得将遗留 YAML 字段当作有效调优。
- 降级：不支持的 phase/shape 或 graph 启用失败要 fallback eager 而不得
  返空；ROCm/NPU/enforce-eager 都依赖 eager 合同，不能把 CUDA-only 测试
  外推为跨平台验证。
- 精度：增量解码与 full decode 数值接近但不保证 bit-exact，shape、
  layout 和 contiguity 均可引入小浮点差异。验收要覆盖 eager/graph、
  ICL/xvec、first/growing/rolling、partial-final trim、缺失 capture 与 graph-enable
  失败。^[PR #5202]
  最终 pin 没有 PR body 早期描述的 `_last_output_audio_length`；真实合同是
  exact-length `list[Tensor]` 加 Code2Wav 的 stateless leading-context trim。

## Q3TTS-3e — adaptive ramp 是 host-side、每段 opt-in 控制器，不是 CUDA graph 计划

- 触发：`codec_chunk_adaptive`、Talker→Code2Wav async chunk、动态 IC、buffer/underrun telemetry，或 Qwen3-TTS deploy 的 chunk key。
- 强制：默认关闭；启用后优先于 fixed `codec_chunk_ramp`。chunk 0 保持 dynamic IC（即使配置了 fixed initial IC），chunk 1+ 才逐 emit 从本段 controller 决定 target。controller 以 80 ms/frame 的 emitted audio 减去 first-emit 后 wall time 得 buffer；以 EWMA（alpha 0.3）估算 frame time。greedy target、无条件 ramp floor `last_target + max(ramp_delta_min, int(ewma/ramp_divisor))` 和 `codec_chunk_min_frames..codec_chunk_frames` clamp 合并；buffer 达到 `codec_chunk_frames * ewma + safety_margin` 后 hysteresis lock 在 max。floor 在负 buffer 下也不能关闭：即使 W5 streaming decoder state 已在基线，per-chunk transport/padding/graph overhead 与 overload death-spiral 仍使向下缩块不是本 PR 的策略；downward adaptation 明确 deferred。
- 强制：非终止也发送所有已积累的 new frames；finished flush 全部余帧，无余帧仍发 empty-finished sentinel。recorded `last_target` 必须是本次 intended target，不能用实际 overshoot emitted frames，否则 spike 会把 floor 错误 ratchet 到 max。controller state 在 segment boundary/request cleanup 清除；每段 INFO telemetry 记录 chunk trajectory、每块 gap 与 total gap。
- 配置：`codec_chunk_adaptive=false`、`codec_chunk_min_frames=2`、`codec_chunk_safety_margin_ms=50.0`、`codec_chunk_ramp_divisor=15`、`codec_chunk_ramp_delta_min=2`；min/divisor/delta-min 小于 1 clamp 为 1，最大值固定是 `codec_chunk_frames`（没有单独 adaptive max key）。两份 Qwen3-TTS deploy 只注释示例，未默认开启。
- CUDA 边界（未解决、非阻塞）：CUDA 的 Code2Wav graph capture grid 从 fixed IC/ramp 建，而 controller runtime cumulative boundaries 通常不在该 grid；adaptive ramp（含 dynamic-IC chunk 0）因此可 eager，910B NPU 不受该 CUDA graph path 影响。已匹配 source 的 nonterminal partial replay 还可能不 advance decoder cache。这两点尚无 merged fix，不得声称 adaptive 与 fixed ramp 同等 graph replay 或 cache continuity；在 CUDA 上以 graph stats/hit rate 实测。^[PR #6001]

### PR #6001 性能证据边界

- 这不是通用吞吐/RTF 改善承诺。合并前 A/B 是 Ascend 910B、vLLM 0.27.1 + vLLM-Ascend PR #14027、Qwen3-TTS voice-clone、40 requests、concurrency 8；adaptive 为 opt-in 唯一变量：mean RTF 0.746（default 0.757 / fixed 0.789）、mean underrun 0.14 s（1.18 / 0.27），continuity OK 35%（0% / 22.5%）。另一 910B c=2 观测 RTF 0.56、underrun 0.02 s；c=8 per-stream RTF>1 仍会饱和。作者听音验证质量，但未给跨硬件或 CI unit suite 证据。
- 更准确的主张是 portability：约 11 ms/frame 的 CUDA hardware 不重调 Ascend 手选 `[4,4,8,16,25]` 即自适应出 `[2,4,25,25,3]`；CUDA graph miss 可解释相对 fixed ramp 的损失。上述数字、模型、负载和软件栈以外不得外推。^[PR #6001]

## PR #5202 性能证据边界

- 作者基准是单张 RTX 3090 24 GB、vLLM 0.26.0、Omni
  `b8edbc8d`、Seed-TTS 32 prompts、concurrency 1/2/4/8/16、batch-size-1 graph
  capture；cached RTF 从 `0.222` 至 `0.718`，相对 baseline 报告吞吐改善
  `10.3%/20.2%/31.1%/40.4%/34.1%`，mean WER `0.0089`。这是特定
  配置与数据集证据，无 raw result 链接/重复次数或方差，不可当作
  通用 SLA。PR 自报 focused tests `34+22+101 passed`，同时保留一个
  未改动的 log-capture 失败，不得记为全绿。
  该 `b8edbc8d` 是 pre-merge 基准 SHA，不是目标 `4f9b6507` 的性能证明。
- 最终 reviewer 在 H20、vLLM 0.26.0 上用 current main `12a5f6fb`
  对照 head `c63c2efc`，8.08 s clone reference 的输出前缀相关系数为
  base `-0.0034/+0.0028`、head `+0.0005/-0.0038`，支持“未重现
  reference-audio leak”。该单请求 A/B 不覆盖 compacted-batch index fix，
  它的证据来自新单元测试，不得混为硬件验证。

共享 readiness/错误隔离规则见 [Serving rules](../../components/serving/rules.md)；
Qwen 家族入口见 [Qwen-Omni](../qwen-omni/_index.md)。

## Q3TTS-3f — codec frame validity 必须对应已处理的 input token

- 触发：修改 Talker preprocess、`make_omni_output`、`codec_frame_valid` 或 async frame selection。
- 强制：prefill/replay span 标无效；正常 decode 按已处理 input token 是否属于 codec vocabulary
  标有效。consumer 使用显式 validity，多行 span 与选取的最后 audio row 对齐；刚采到 EOS
  时仍保留前一 input token 的真实 frame，显式有效的全零码也必须保留。
- 禁止：依据新采样 EOS 或 output history 丢弃当前真实 frame；用 `codes.any()` 覆盖显式
  validity；把 prefill placeholder 当音频。缺字段时的旧 producer fallback 不得描述为能辨别
  有效零码。
- 验收：覆盖 prefill/replay、非零 invalid frame、全零 valid frame、刚采到 EOS 的真实尾帧，
  以及多行 validity 最后一项选择和 scalar/batch 一致性；测试要求不等于这些边界均已有通过证据。
  ^[PR #7781] 源码与回归入口：
  [validity producer](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/models/qwen3_tts/qwen3_tts_talker.py#L1056-L1090)、
  [consumer 与 frame tests](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/stage_input_processors/test_qwen3_tts_async_chunk.py#L274-L359)。

## Q3TTS-3g — sliding-mask cache 只复用于 implicit-position stateless decode

- 触发：修改 Qwen3-TTS tokenizer decoder attention-mask 缓存或 stateless CUDA capture。
- 强制：只有没有显式 attention mask、positions、cache positions、past KV 且未启用 cache
  的调用才复用该缓存；key 保留 sequence length、dtype、device、attention implementation，
  module placement/dtype 变化清空缓存。其余调用使用 live/prepared mask。
- 禁止：仅因 length 相同就把 implicit zero-based mask 用于显式或增量 positions；通过读取
  CUDA tensor 值推断可缓存性；用 legacy wrapper 测试代替 production segmented capture。
- 验收：implicit cached path 对照独立 live-mask path，explicit/incremental path 不误命中；
  dtype/device 迁移后缓存失效；真实 capture 后换输入并 replay，与 eager 对照。小型 decoder
  的有限 shape 测试不能外推为任意 position pattern、完整 checkpoint 或跨平台验证。
  ^[PR #7781] 源码与回归入口：
  [cache eligibility/invalidation](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/models/qwen3_tts/tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py#L533-L661)、
  [production capture](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/models/qwen3_tts/test_qwen3_tts_stateless_capture.py#L21-L100)。

## Q3TTS-4a — silence codec ban 必须按 checkpoint 词表和 x-vector-only mode 生效

- 触发：修改 Qwen3-TTS Base voice-clone 的 leading silence 行为、`silence_ban_frames`、talker codec mask、x-vector/ICL mode resolution 或 MRv2 sampling context。
- 强制：默认 `silence_ban_frames=0`；加载 checkpoint 后通过 `_encode_ref_audio_batch` 编码多种静音样本并只收集 codebook-0 ids，词表为空、越界或明显过大时记录 warning、清空启用值并禁用功能。decode 时只对 Base 的 x-vector-only request 在前 N 个 history steps 屏蔽派生 ids；mode resolution 必须与 prompt builder 一致，`voice_clone_prompt.icl_mode` 覆盖和未记录 mode 都要正确处理。
- 强制：MRv2 按本步请求顺序结合 live generated-token count，只在 sampling context 应用前 N 步 mask；正常与异常退出都清空临时 mask。
- 禁止：硬编码当前 checkpoint 的 12 个 token、把 mask 施加到 ICL 或非 Base 请求、对不可信派生词表只做部分 masking，或把经验值 `N=3` 当成跨 checkpoint 的固定最优参数；让 sampling mask 污染随后 prompt logprobs，或用独立模型 counter 在 resume 后重启窗口。
- 验收：覆盖默认关闭、有效派生、空/越界/ oversized 派生和 encoder failure；覆盖 step 边界、mixed x-vector/ICL batch、ICL 与非目标请求保持未修改，以及实际 stage 配置能到达 talker；MRv2 重排 mixed-mode/mixed-step batch 后 mask 仍对齐，context 外 prompt logits 和异常退出后均无遗留 mask。^[PR #5048] ^[PR #7781]
  源码与回归入口：[sampling scope](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/models/qwen3_tts/qwen3_tts_talker.py#L1252-L1267)、
  [scope/order test](https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/model_executor/models/qwen3_tts/test_qwen3_tts_talker_silence_ban.py#L82-L103)。

## Q3TTS-4b — Base codec token exhaustion 必须丢弃不完整音频

- 触发：修改 Qwen3-TTS Base 的 codec token budget、Stage-0 finish metadata、speech generation
  validation、非流式 retry 或 streaming terminal/error 语义。
- 强制：caller 未显式给 `max_new_tokens`、task 为 Base 且 text token count>0 时，自动上限为
  `min(configured_cap, max(192, 12*text_tokens))`（无 configured cap 时取 dynamic cap）；tokenizer
  不可用或计数失败时保留 configured cap，非 Base 也不套 text-scaled ceiling。无论上限来自 caller、
  config 或动态计算，只在 request metadata 记录了 effective cap、task 为 Base 且 Stage 0 实际
  `finish_reason="length"` 时判为 budget exhaustion；不能从 token 数接近上限自行猜测。
- 强制：exhausted generation 的音频不完整，non-streaming 只能在 caller 同时未指定 seed 与
  `max_new_tokens` 时，用新 request id 和 fresh seed 重试恰好一次；不得修改原 request。raw audio、
  SSE 与 WebSocket 等 streaming 路径不重试：若已经发出 delta，terminal error 必须标记
  `partial_audio=true, action=discard`，且不得发 success/done；错误发生在首个 audio 前则不伪造 partial。
- 禁止：把长度终止的 Base 音频当成功结果；用 fresh seed 覆盖显式可复现请求；在 streaming 中
  隐式重放造成重复音频；把该 guard 外推到所有 TTS adapter 或声称 dynamic cap 保证 EOS。
- 验收：覆盖 short/long text、configured cap、显式 max、token-count failure 与非 Base；分别验证
  EOS/非 length 正常通过、length+recorded-cap 拒绝、非流式一次 retry 及显式 seed/max 不 retry；
  raw/SSE/WebSocket 均携带 terminal metrics，并覆盖 partial/no-partial error。目标回归证明控制流和
  丢弃语义，不证明任意 prompt 必达 EOS、音频质量或最佳 cap 比例。^[PR #6728]

  Ready CI 的 dummy-weight Base 即使到 192 tokens 仍可无 EOS 并返回 500，因而不是该 guard 的
  Ready oracle；此事实不改变 runtime、YAML、EOS 或音质合同，也没有 real-weight merge pass log。
  ^[PR #6861] ^[issue #6855]

## Q3TTS-5a — 共享 code predictor 的 FP32 fallback 排除 NPU，且禁止外层 capture 时重放内层图

- 触发：修改 `qwen3_code_predictor` 的 FP16→FP32 stability fallback、NPUGraph/`_device_graphs` replay，或 Qwen3-TTS/Qwen3-Omni Talker 在 `enforce_eager=False` 下的外层 `talker_mtp` capture。
- 强制：`use_fp32` 仅当 `input_dtype == float16` 且 `device.type not in ("cpu", "npu")`；CUDA/XPU/MUSA 等其它 accelerator 保留既有 FP32 upcast。Ascend fused RMSNorm 要求 activation 与 weight dtype 一致，不得在 NPU 上形成 FP32 activation + FP16 gamma。
- 强制：每个 forward 采样一次 `is_npu_capturing = current_omni_platform.is_npu() and torch.npu.is_current_stream_capturing()`；存在 device graph 时，仅当**未**在外层 NPU capture 中才 `replay()`，capture 期间走普通/`compiled` forward 让外层图录制。无外层 capture 时仍使用内层 replay 快路径。
- 禁止：把“非 CPU 即 FP32”重新扩到 NPU；在外层 capture 中嵌套 replay 内层 NPUGraph；或仅靠 stage 级 `PIECEWISE` workaround 声称已修复嵌套 replay 根因。
- 验收：参数化断言 cpu/npu 不 upcast、cuda/xpu/musa upcast；`is_capturing` true/false 分别断言 skip/do replay。该合同属于共享 predictor，不证明每个 Qwen3 stage 已全图安全。^[PR #6639]
