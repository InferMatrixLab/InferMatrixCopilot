---
title: "MiniCPM-o 4.5 规则"
created: 2026-07-20
updated: 2026-09-22
type: rule
tags: [vllm-omni, models, model-executor]
sources: ["PR #3642", "PR #5165", "PR #5382", "PR #5524", "PR #5638", "PR #5792", "PR #5869", "PR #6056", "PR #6154", "PR #6170", "PR #6318", "PR #6828", tests/dfx/perf/tests/test_minicpmo_4_5.json, tests/dfx/perf/tests/test_minicpmo_4_5_duplex_seed_tts.json, tests/e2e/accuracy/minicpmo_4_5/test_minicpmo_4_5.py, tests/e2e/online_serving/helpers/minicpmo_4_5_duplex.py, tests/e2e/online_serving/test_minicpmo_realtime_duplex_drivers.py, tests/e2e/online_serving/test_minicpmo_4_5.py, tests/e2e/online_serving/test_minicpmo_4_5_duplex.py, tests/e2e/online_serving/test_minicpmo_4_5_expansion.py, tests/e2e/online_serving/run_minicpmo_realtime_duplex_soft_interrupt.py, vllm_omni/benchmarks/data_modules/seed_tts_dataset.py, vllm_omni/benchmarks/data_modules/seed_tts_eval.py, vllm_omni/benchmarks/patch/patch.py, vllm_omni/deploy/minicpmo_4_5.yaml, vllm_omni/experimental/fullduplex/client.py, vllm_omni/entrypoints/duplex/chat_fallback.py, vllm_omni/entrypoints/duplex/realtime_input.py, vllm_omni/entrypoints/duplex/session_runner.py, vllm_omni/entrypoints/duplex/serving.py, vllm_omni/entrypoints/duplex/vad.py, vllm_omni/model_executor/models/minicpmo_4_5/duplex/adapter.py, vllm_omni/model_executor/models/cosyvoice3/code2wav_core/hifigan.py, vllm_omni/model_executor/models/minicpmo_4_5/batched_token2wav.py, vllm_omni/model_executor/models/minicpmo_4_5/cuda_graph_wrapper.py, vllm_omni/model_executor/models/minicpmo_4_5/minicpmo_4_5_code2wav.py, vllm_omni/model_executor/models/minicpmo_4_5/minicpmo_4_5_omni_llm.py, vllm_omni/model_executor/models/minicpmo_4_5/minicpmo_4_5_omni_tts.py, vllm_omni/model_executor/stage_input_processors/minicpmo_4_5_omni.py, tests/model_executor/models/minicpmo_4_5/test_audio_chunk_mask.py, tests/model_executor/models/minicpmo_4_5/test_cfm_graph_capture_gating.py, tests/model_executor/models/minicpmo_4_5/test_code2wav_batching.py, tests/model_executor/models/minicpmo_4_5/test_cuda_graph_wrapper.py, tests/model_executor/models/minicpmo_4_5/test_pipeline.py, tests/model_executor/models/minicpmo_4_5/test_talker_batching.py, tests/model_executor/models/minicpmo_4_5/test_vision_flash_attention.py, "PR #6082", "PR #5604", "PR #6274", "PR #6346", "PR #6397", "PR #6406", "PR #6458", "PR #6587", "PR #6619", "PR #6757", "PR #6529", "PR #6772", vllm_omni/entrypoints/duplex/protocol.py, "PR #7071", "PR #5730", "PR #7344", "PR #7517"]
confidence: high
---

# MiniCPM-o 4.5 规则

只有 `MCPMO-数字字母` 是可审计规则 ID。

相关专题：[MiniCPM-o 4.5 Code2Wav 运行时合同](rules-code2wav-runtime.md)（`MCPMO-1f`, `MCPMO-1g`, `MCPMO-1h`, `MCPMO-1i`）。

## Direct 代码快速入口

| PR 描述信号 | 规则入口 |
|---|---|
| version/registry、processor、remote code | `MCPMO-1a`、`MCPMO-2a` |
| Whisper/APM 或 SigLIP attention | `MCPMO-2b`、`MCPMO-2c` |
| audio_feature_lens ragged slices、encoder batch TensorShape、`audio_embeds` 注册与 placeholder | `MCPMO-2e`、`MCPMO-2f` |

| TTS/Code2Wav/TRT、CUDA Graph | `MCPMO-1b`–`MCPMO-1e`；[CUDA graphs](rules-cuda-graphs.md) |
| batch、runtime info、reference-audio handoff | `MCPMO-3a`–`MCPMO-3e`；[Code2Wav batching](rules-code2wav-batching.md) |
| native duplex、sampling budget、shipping profile、playback ACK、Talker full-attention rollover | `MCPMO-4a`–`MCPMO-4i`；[duplex](rules-duplex.md) |

| accuracy/performance/online-serving evidence | `MCPMO-5a`、`MCPMO-5b` |

## 完整代码路由

| PR 描述信号 | 规则组 | 第一批源码 |
|---|---|---|
| MiniCPM-o 4.5、`minicpmo_4_5`、版本识别 | MCPMO-2a | `config/pipeline_registry.py::OMNI_PIPELINES["minicpmo_4_5"]` → `model_executor/models/minicpmo_4_5/pipeline.py`；`model_executor/models/registry.py::_OMNI_MODELS` |
| Whisper/APM、chunk attention mask、left context/lookahead | MCPMO-2b | `minicpmo_4_5_omni_llm.py::{get_audio_hidden_states,subsequent_chunk_mask}` → audio encoder；row-by-row oracle test |
| SigLIP、FlashAttention、padding/unpad metadata、varlen vision | MCPMO-2c | `SiglipEncoder.forward` → `SiglipEncoderLayer` → `SiglipFlashAttention2._upad_input` |
| tokenizer/processor、`trust_remote_code` | MCPMO-1a | pipeline/config factory → `model_executor/models/minicpmo_4_5/` loader |
| TTS extra、backend 初始化、空音频 | MCPMO-1b | `minicpmo_4_5_omni_tts.py::MiniCPMO45OmniTTSForConditionalGeneration`、`minicpmo_4_5_token2wav.py::MiniCPMO45Token2wav` |
| Code2Wav TensorRT、DiT/Campplus、engine cache/profile | MCPMO-1c | `minicpmo_4_5_code2wav.py::MiniCPMO45Code2Wav` → `batched_token2wav.py::BatchedToken2Wav._estimator_step`；共享实现 `step_audio2/step_audio2_dit_trt.py` |
| HiFT CUDA Graph、chunk bucket、lazy capture、iSTFT | [`MCPMO-1d`](rules-cuda-graphs.md#mcpmo-1d-hift-graph-只捕获稳定-pre-istft-子图并限界-shape-cache) | `cuda_graph_wrapper.py::HiFTGraphWrapper` → `BatchedToken2Wav._hift_inference` → shared `HiFTGenerator` |
| CFM DiT CUDA Graph、shape cache、generation retirement、eager fallback | [`MCPMO-1e`](rules-cuda-graphs.md#mcpmo-1e-legacy-step-level-cfm-dit-graph-必须按-shape-整代退休并保持-eager-parity) | `cuda_graph_wrapper.py::CFMGraphWrapper` → `BatchedToken2Wav._estimator_step` |
| batch、`runtime_info`、stage handoff | MCPMO-3a/3b | `stage_input_processors/minicpmo_4_5_omni.py` → `minicpmo_4_5_omni.py::MiniCPMO45OmniForConditionalGeneration` → TTS/code2wav |
| Seed-TTS、chat `ref_audio`、runtime-ref、Code2Wav handoff | [MCPMO-3e](rules-code2wav-batching.md#mcpmo-3e-minicpm-o-45-reference-audio-必须经过私有-prompt-到-code2wav-handoff) | serving chat MediaConnector → original prompt private key → `llm2tts` buffer → Code2Wav |
| Talker codec sampling、repetition penalty、request RNG/compaction | MCPMO-3c | `minicpmo_4_5_omni_tts.py::{make_omni_output,_sample_audio_codes,_apply_batched_repetition_penalty}` → `test_talker_batching.py` |
| native duplex、Stage0 resume、LISTEN/SPEAK、server VAD | MCPMO-4a | `experimental/fullduplex/{minicpmo45,openai}/` → stage input processor |
| instructions/native-mode update、first-append reservation、context lock | [`MCPMO-4b`](rules-duplex.md#mcpmo-4b-native-duplex-session-update-必须如实拒绝不可重建的上下文) | `experimental/fullduplex/minicpmo45/{adapter,session}.py` → runtime adapter/session runner |
| duplex Talker chunk、runtime `min_tokens`、chat sampling isolation | [`MCPMO-4c`](rules-duplex.md#mcpmo-4c-native-duplex-talker-必须按-generate_chunk-预算终止) | `experimental/fullduplex/minicpmo45/adapter.py` → stage 1 sampling → Talker `generate_chunk` |
| shipping YAML、duplex/chat 共服、session capacity、async scheduler | [`MCPMO-4e`](rules-duplex.md#mcpmo-4e-shipping-profile-必须共服-chat-与-native-duplex) | `deploy/minicpmo_4_5*.yaml` → config merge → realtime/chat entrypoints |
| playback ACK、response-owned assistant history、下一 input 后的 late ACK | [`MCPMO-4h`](rules-duplex.md#mcpmo-4h-responseplayback-history-必须由回答它的-input-commit-所有) | `experimental/fullduplex/openai/{protocol,serving}.py` → response snapshot / conversation item lifecycle |
## MCPMO-1a — trust_remote_code 服从用户选择

- 触发：API/CLI server 为 MiniCPM-o 加载 tokenizer、processor、config 或模型代码。
- 强制：只使用 CLI/配置解析后的 `trust_remote_code` 值，并沿所有入口传递。
- 禁止：模型特例无条件设置 `True`，绕过用户的安全选择。
- 验收：false 路径不执行 remote code，true 路径按显式授权加载；offline/online 语义一致。 ^[PR #3642]

## MCPMO-1b — TTS 依赖显式声明，初始化失败不得伪装为空音频

- 触发：可选 TTS extra、音频 backend 或模型初始化异常。
- 强制：缺包使用明确 extra/import 错误；只有确认为 ImportError 的可选能力才可按合同
  降级，其他初始化失败立即向上抛出。
- 禁止：catch-all 后返回空 waveform，让请求看似成功。
- 验收：缺 extra、坏配置和 backend 初始化失败分别得到可诊断错误；正常路径返回非空
  且格式正确的音频。 ^[PR #3642]

## MCPMO-1c — Code2Wav TensorRT 是 CUDA-only 的显式可逆替换

- 触发：`token2wav_trt` / `MINICPMO_TOKEN2WAV_TRT=1`，或修改 Code2Wav 的 DiT、
  Campplus、ONNX/TRT engine cache 与 optimization profile。
- 强制：各平台先构造树内 `MiniCPMO45Token2wav`；仅 CUDA 开关同时启用 DiT estimator 与
  Campplus TRT，关闭/非 CUDA 保留 torch。DiT 只替换 `_estimator_step`，encoder/HiFT 仍为
  torch 且 engine 用调用者 CUDA stream。dtype ONNX export 深拷贝再 cast、不改 live estimator；
  plan key 含 depth、dtype、max-batch、SM、TRT version，profile 覆盖 CFG `2B`、chunk 3000、
  cache 1000，越界报可操作错误；Campplus 是本地资产 `[T,80]` → `[1,192]` fp32。
- 强制：共享 `_publish_atomically` 用同目录 `<dest>.tmp.<pid>.<uuidhex>` 独占 `xb`；writer
  写入、flush/close 后 `os.replace`，并发成功完整且 last-writer-wins。collision 在 writer 前
  失败且不删 foreign tmp；任何失败保留 destination、只删 owned tmp、重抛原异常，cleanup 仅
  日志。ONNX path writer 在 umask 移除 owner-write 时记录 mode、补写权限并在发布前恢复；plan
  stream 不作 `fstat`/`fchmod`。无 fsync/目录持久化、锁、内容校验或 ONNX/plan 成对事务。
- 禁止：把 shared helper 解释成 Step-Audio2 TRT；当前显式 consumer 只有 MiniCPM-o Code2Wav，
  也不得把讨论中的通用 vocoder backend 当作已实现。
- 验收：覆盖开关/非 CUDA、torch/TRT parity、cache/SM/profile 边界；运行
  `python -m pytest -q tests/model_executor/models/step_audio2/test_step_audio2_dit_trt.py` 覆盖
  CPU 并发、collision、writer/fstat/replace/cleanup failure 与 restrictive umask。MiniCPM
  wiring 的自动化验证仍须补证。 ^[PR #5638] ^[PR #6957]

## MCPMO-2a — registry 使用 4.5 config/version predicate

- 触发：pipeline auto-detection 看到通用 `MiniCPMO` architecture。
- 强制：结合 4.5 专有 architecture/config 字段判定；旧 1.0/2.6 不得落入 4.5 pipeline。
- 禁止：仅用 architecture 集合相交。
- 验收：4.5、旧版和无版本字段 fixture 分别命中 4.5、旧 pipeline 或明确失败。 ^[PR #3642]

## MCPMO-2b — audio chunk mask 向量化必须保持逐行边界语义

- 触发：修改 Whisper/APM `subsequent_chunk_mask`、chunk size、left-context 或 lookahead。
- 强制：对每个 query row，end 保持 `min((row // chunk_size + 1) * chunk_size +
  num_lookhead, size)`；`num_left_chunks < 0` 从 key 0 开始，否则 start 保持
  `max((row // chunk_size - num_left_chunks) * chunk_size, 0)`。可用 query/key index
  broadcast 一次构造 dense bool `[T,T]` mask，但不得改变 caller 与 padding mask 的 OR/取反语义。
- 禁止：把移除 O(T) Python row loop 描述成移除 O(T²) mask storage/attention；把 reviewer
  建议的 `@staticmethod` 当成已合并（目标仍保留 instance-method interface）；或声称所有 TTFT
  case 都改善。PR 自己记录短输入/较大 batch 的 scheduler noise 会淹没局部节省。
- 验收：row-by-row oracle 覆盖 size 0、chunk 边界两侧、无限/有限/零 left context 与 lookahead，
  并在真实 caller 组合 padding mask。目标提交的 CPU 参数化测试覆盖 216 组纯 mask parity，
  但不覆盖 CUDA kernel 数、完整 audio embedding 或 serving。PR 的 CUDA timing/bitwise embedding
  证据来自 `ee33954dff27da317be597449a6c1b5a5df4052b`，不是 merge target `9235b0ae` 的复测，
  因而只能作为外部局部性能证据。^[PR #5382]

## MCPMO-2c — vision unpadding metadata 只在同一次 encoder forward 内复用

- 触发：修改 MiniCPM-o 4.5 SigLIP FlashAttention mask、`_get_unpad_data`、Q/K/V layout 或
  encoder layer 调用链。
- 强制：只有 FlashAttention2 且 `attention_mask is not None` 时，`SiglipEncoder.forward` 才从
  当前 mask 计算一次 `(indices, cu_seqlens, max_seqlen)`，并把同一 tuple identity 传给该次
  forward 的所有 encoder layers。下一次 forward 必须从新 mask 重算；不得做 global、实例级或
  cross-request cache。直接调用 `SiglipFlashAttention2` 且未传 metadata 时，`_upad_input` 保留
  per-call fallback；dense mask=None path 不计算 metadata。显式传入的 metadata 不会校验是否与
  `attention_mask` 匹配，因此 custom/direct caller 必须成对持有；安全 owner 是 encoder 当前
  forward 的 mask，禁止外部或 stale cache。
- 强制：metadata 复用必须保持 padded batch 输出与逐 sample unpadded、以及逐 layer 重算 path
  数值一致。Q/K/V projection 的 view→transpose→transpose 原本就形成共享 projection storage 的
  contiguous BSHD；本 PR 没改 layout，新 storage/stride test 只是冻结该既有 zero-copy 合同，
  不能把它计入 metadata 优化收益。
- 禁止：把 mask shape 相同当成内容相同而跨 forward 复用；将该局部优化泛化成 dense image、
  equal-length video、端到端 serving 或 throughput 提升。review 中短暂加入的 profiler scripts/raw
  JSON 已在 final commit history 删除，不是仓库内可复现 benchmark fixture。
- 验收：CPU spy 覆盖 padded 一次、dense 零次、不同 mask 重算与 tuple identity；L4/FA2 BF16
  覆盖 encoder reuse 对 per-layer recompute bitwise parity、unpadded 对 eager、两种 padding 程度
  对逐 sample unpadded（容差 2e-2）。PR 的 2×RTX PRO 6000 Blackwell、27 layers、5 warmups、
  30 paired samples 中 varlen 约 21.697→13.790 ms / 22.162→14.113 ms，只绑定测试 commit
  `895cb303`、该环境与 vision-encoder latency；作者说明 rebase 后 source-file hash 未变，但它
  仍不是 merge target 的 end-to-end 复测。dense/equal-length video 不计算 metadata且近零变化。
  当前测试未覆盖 `gradient_checkpointing and training`：新 tuple（两 tensor + Python int）会作为
  位置参数穿过 `_gradient_checkpointing_func`，补该 path 前不能假设训练/checkpoint 兼容。
  ^[PR #5165]

## MCPMO-2d — Whisper 流式音频缓存必须兼容 current 与 legacy Transformers API

- 触发：MiniCPM-o 4.5 Whisper/APM 音频编码器需要在 Transformers 版本变化后复用流式 chunk 的 attention cache。
- 强制：根据 `self_attn.forward` 签名选择 `past_key_values` 或旧版 `past_key_value`；读取缓存长度时优先支持 `self_attention_cache.get_seq_length()`，兼容需要 layer 参数的实现，并回退到 legacy tuple 的 key shape；当当前 attention 返回值没有 cache 项时保留传入 cache。
- 禁止：固定传入已被新 API 吞入 `**kwargs` 的旧参数名；调用已移除的 `get_usable_length()`；依赖 experimental 模块的全局 monkey patch 才能读取缓存；把当前 API 返回的两项结果误解为应清空已有 cache。
- 验收：覆盖 legacy/current attention 参数名、`EncoderDecoderCache`/`DynamicCache`、legacy tuple 和两种 `get_seq_length` 签名；连续编码两个 chunk 时缓存长度正确增长，输出与无缓存编码有可观察差异，并通过 MiniCPM-o 4.5 streaming/duplex 回归验证首响应和中断行为。 ^[PR #6274]

## MCPMO-3a — TTS stage 的 batch 能力与 runtime_info 消费一致

- 触发：talker/TTS stage 的 `max_num_seqs`、batching 或 runtime bridge。
- 强制：若实现只安全消费一条 runtime info，将上限设为 1；开放 batch 前逐请求处理并
  证明输出不串线。
- 禁止：读取 `runtime_info[0]` 后把请求 0 的音频广播到全批。
- 验收：两个不同文本/voice 的同批测试分别产生对应音频；未支持前配置拒绝 batch>1。 ^[PR #3642]

## MCPMO-3b — wrapper 接收 bridge 并显式包装 OmniOutput

- 触发：LLM/thinker → TTS stage handoff 或 TTS class 返回 waveform。
- 强制：stage wrapper 读取 runner 实际写入的 bridge 字段，模型输出包装到明确的
  `OmniOutput`/multimodal key。
- 禁止：直接 TTS class 不读 runtime bridge；返回 tuple 让 runner 猜它是 hidden state。
- 验收：真实 wrapper handoff 测试断言字段名、逐请求 shape、最终公开 payload。 ^[PR #3642]

## MCPMO-3c — Talker 批量 logit 处理必须保留逐请求采样与输出索引

- 触发：修改 Talker `make_omni_output`、codec projection、repetition penalty、top-k/top-p、
  multinomial、terminal 或 compaction。
- 强制：只把有效 span、未 finished 且 sampling-eligible 的 row 收集为 pending samples；一次批量
  执行 head projection、frequency-aware repetition penalty、top-k/top-p 与 softmax。每个 pending
  item 必须携带原 `output_index`、request ID、history、step 与 state，采样结果再按该原索引写回
  codec delta、terminal 和 stop row。无效 info/span 与 incomplete prefill 保持 continue、空 delta，
  且不推进 request RNG/state；此前或本次 terminal 的请求才 stop。
- RNG：`torch.multinomial` 每次只接收一个 generator，因此必须保留逐 row 调用各自 request-local
  generator；禁止为了完全向量化而改成共享 generator。batch reorder、其他请求结束和 compaction
  均不得改变单请求序列。所有 row 采完后再把 concatenated tensor 一次搬到 CPU `.tolist()`，
  避免逐请求 `.item()` 同步。
- workspace：repetition penalty 可保留 full-batch logits/result，但 encoded history 与
  `bincount` 必须最多按 16 rows 分块；token 编码为 `token + local_row*vocab`，count minlength
  只到 `chunk_rows*vocab`，使 int64 count 临时空间不随总并发一次放大。penalty=1 与空 batch
  保持 fast path，logits 必须为 2D且 histories/request IDs/steps 与 batch row 数一致。
- 验收：逐 row oracle 对比正负 logits 与空 history，跨 16/16/1 chunk 固定 bincount workspace；
  reorder、compaction 与 standalone 对照 request RNG；mixed eligible/ineligible/finished rows 断言
  原索引 delta、stop、EOS/min/max token、duplex metadata 和只调用一次 batched sampler。CPU 单测
  证明这些离散合同，但没有硬件上的 batched GEMM/logit 与旧 serial GEMV 数值/音频 parity；PR
  也没有 NPU concurrency 1/4 的 latency、吞吐或内存数据，因此不得宣称性能或质量不变/提升。
  ^[PR #5792]

## MCPMO-3d — Talker codec 采样必须由 Stage 1 Sampler 与 codec 词表闭环

- 触发：修改 MiniCPM-o 4.5 Talker 的 codec 采样、词表、EOS、prompt penalty 或 Stage 1 sampling 参数。
- 强制：`ConditionalChatTTSConfig` 暴露 `vocab_size=num_audio_tokens`、`eos_token_id=num_audio_tokens-1`；完整采样项由 Stage 1 `default_sampling_params` 提供，缺项初始化失败；`head_code`、`emb_code` 与 `codes.audio` 按采样 id 闭环，Sampler 前将 scheduler prompt 全置为 `vocab_size`。
- 禁止：从 `tts_config` 回退或硬编码 codec knobs；把 Thinker token/embed 或 scheduler 占位符当作 codec history；丢失 sampled id 或让 EOS 不可达。
- 验收：配置、pipeline EOS、prompt 不变、codec delta/empty EOS 单测通过，并用长文本真实 text+audio/ASR 检查完整输出。 ^[PR #6346]

## MCPMO-4a — native duplex 保留可恢复 Stage0，VAD 只作显式策略

- 触发：MiniCPM-o native duplex 的连续音频、LISTEN/SPEAK handoff 或 server VAD interruption。
- 强制：模型说话时仍把音频单元追加到同一可恢复 Stage0，保留 KV/runtime state 和上一
  terminator；仅确认的 empty/control-only handoff 可在没有 speech conditioning 时跳过，其他
  缺 latent/hidden-state 的 speech handoff 仍报错。plain LISTEN 是成功且不需要 Talker hidden state；含 turn_eos 的 speech-closure
  LISTEN 仍须转交 Talker，边界见 [turn closure](rules-duplex-turn-closure.md)。默认模型拥有的模式固定为 `listen_only`，不会创建/调用 Silero；只有
  `turn_detection.type=server_vad` 且 `interrupt_response=true` 才选择 `barge_in_on_speech`，
  并拒绝 `interrupt_response=false` 的第三模式。server VAD hard cancellation 必须显式 opt-in，
  阈值不得落到使 silence branch 不可达的边界。
- 禁止：用 server VAD cancellation 冒充 native interruption；每轮重建 Stage0；让 LISTEN 因
  无 Talker payload 失败；将 Qwen3-Omni 的 turn-based server-VAD endpointing/auto-commit
  合同混同为本 MiniCPM native-barge-in 行为。
- 验收：native case 完成响应≥2、cancel/truncate=0、listen≥1；opt-in hard interrupt 恰好一次
  terminal、epoch fence 后无 stale delta，且新 epoch 保留 interrupt PCM/confirming pre-roll、
  后续响应消费该语句并成功。Realtime `session.update` 先暂存 VAD candidate，只有 matching
  runtime ACK 才 commit，任何 rejected/failed append/config path 都必须 correlated error 后
  discard，不能让 unrelated outbound error 提交它或让 reader 卡住。response-required fixture 若要求
  每轮 SPEAK，应重放已验证的完整 active-speech window；任意较短 mid-utterance slice 合法返回
  LISTEN，不能把它当模型失败或修改生产决策阈值。`threshold=0.15` 的 validator boundary assertion
  在 merge 时缺失，是后续测试补强，不是本提交已实现的覆盖；#6821 的 playback ACK/history 修复
  也在本精确提交之外。 ^[PR #6056] ^[PR #6154] ^[PR #6170]

## MCPMO-4d — async chunk 快照替换必须清理旧音频

- 触发：MiniCPM-o 4.5 native duplex 遇到重复 `turn_end` 或新的 Code2Wav async segment。
- 强制：`tts2code2wav_async_chunk` 对重复终端边界发送 ready empty replacement；Code2Wav payload 声明替换 marker；当前 `codes.ref` 等 sibling 可继续使用，上一段 `codes.audio` 必须丢弃。
- 禁止：重复边界返回未就绪的 control-only 更新；让旧 terminal audio 与新 snapshot merge/replay；把该模型的替换语义外推给未 opt-in 模型。
- 验收：覆盖 terminal→empty duplicate、audio+ref、scheduled-new/cached runner、prompt replacement 和 `OmniNPUModelRunner` 继承路径；断言旧 audio 消失且边界被标记为 ready。 ^[PR #6406]

## MCPMO-5a — accuracy 与 duplex perf 证据必须绑定协议、硬件和实际 gate

- 触发：修改 MiniCPM-o 4.5 Daily-Omni、Seed-TTS、realtime duplex 数据集/客户端、benchmark
  配置或 CI lane。Daily-Omni 合同是 `minicpm-interleave`、temperature 0、文本输出、512 tokens，
  使用 TTS template 与 1 FPS/最多 128 帧 media 配置，准确率下限 0.78；simplex Seed-TTS 同时请求
  text+audio，WER 上限 0.05。duplex Seed-TTS 走 `openai-realtime-tts`，每 session 四轮、50 prompts，
  并要求每轮恰好一个音频 response。
- 强制：多轮数据只把连续有效 row 按 `turns_per_session` 分组，沿用首行 reference audio/text；
  多轮必须使用 realtime backend，在同一 session 依次发送 item/response create 并做 playback ACK。
  客户端按轮从共同 measurement origin 单调记录首 text TTFT、首 audio TTFP、逐轮请求起点到末音频的 RTF；
  PCM 与这些 metrics 必须展开回逐轮 WER 样本。`native_duplex=False` 必须显式走 chat fallback，且在
  构造 `ChatCompletionRequest` 前移除 `minicpmo45_native_duplex`。
- 证据边界：accuracy case 标为 H100 或 A3、每 case 一卡，但 nightly 可为第二张卡上的 WER
  evaluator 分配两卡资源；资源 allocation 不等于模型 stage 用卡数。最终 simplex/duplex perf 是
  单卡 H100，baseline 只有 H100 bucket，runner 只检查完成数，duplex 再检查四轮 audio count；没有
  性能阈值 consumer，故 baseline、TTFT/TTFP/RTF 只构成测量 artifact，不能证明回归或 A3 性能。
  ready 的 trimmed simplex config 仅保留 concurrency 1/4 与 prompts 10/40；CUDA `h100_1`、NPU
  `a3_npu_2` 都运行同一 JSON，但 JSON 的 case cardinality 是一张卡且只含 H100 baseline。该 config
  与 runner 没有 threshold consumer；因此 “ready gate” 只表示请求完成 gate 已接线，不是性能
  regression threshold，也不能单凭配置存在声称 H100/A3 实测通过。
- 禁止：把结果 artifact 中存在 baseline 描述成 active performance gate；把 PR 的“双卡”文字覆盖到
  最终单卡 lane/YAML；或从 H100 bucket 外推 A3 的性能与阈值。
- 验收：固定上述 collection、阈值、逐轮 cardinality 与 fallback；分别故障注入缺轮、重复音频、
  非单调时间和错误 backend。PR 报告的 Daily-Omni 78.53%、Seed-TTS WER 0.0332/0.0255 只能绑定其
  日志环境：数据/模型 revision 未固定，Daily 首次受损坏 decord 失败后重跑，kernel warmup 另有
  未提交 patch，证据脚本还吞掉 step failure，且把版本号写作 commit。因而不能从这些数字声称
  merge target 的可复现质量/性能通过。^[PR #5524] ^[PR #6079]

## MCPMO-5b — online serving CI 必须显式区分 chunk 模式与 duplex fixture 语义

- 触发：修改 MiniCPM-o 4.5 online serving、expansion、native duplex 测试或其 deploy helper。
- 强制：core online suite 显式启动 `--async-chunk`，并把 bounded sampling 保持在 fixture-local
  派生配置；expansion suite 以两个独立 parametrization 覆盖 `--no-async-chunk` 与
  `--async-chunk`。不得用 deploy 默认值或参数 ID 猜实际模式。audio→text+audio case 若要稳定
  验证 TTS，必须显式请求 text/audio modalities、`use_tts_template=True`、
  `enable_thinking=False` 并断言目标关键词。
- 对 `test_image_to_text_audio_001` 的 content-consistency oracle，test-local `_CI_DEPLOY` 只把 Stage-1
  Talker `temperature` 设为 `0.0`；shipping deploy/runtime 保持不变。该设置不证明 deterministic audio、
  所有 flake 已消除或生产 sampling 修复；已放弃的 production sampling-mask 工作不属于本合同。^[PR #6828] ^[issue #6815]
- duplex fixture 保留 Talker `max_model_len=4096`，但不覆盖三个 stage 的 KV sizing，不另造 eager-only
  server；protocol smoke 与 response tests 使用同一 server config。response-required 多轮测试每轮
  重放同一 active-speech window，避免较短的 mid-utterance slice 合法地产生 LISTEN。reference audio
  先接受本地 checkpoint 目录（包括绝对 `MODEL` 路径），不存在时才从本地 HF cache 解析。
- 禁止：把删除测试专用 KV cap 解释成生产 deploy 资源变更；把移除 assertion 的 JSON failure
  message 解释成行为放宽；或在输入与 oracle 已改变后声称复现并修复原始 mixed-input
  audio-similarity 故障。
- 验收：至少收集 core async、expansion sync/async、duplex protocol 与 advanced session cases，
  并记录实际执行结果及硬件；local checkpoint 与 cache fallback 各一例。changed tests、marker
  或 parametrization 只能证明 collection 意图；没有实际 runtime 结果时，不能声称 CUDA/NPU、
  音频质量或 flake 已闭环。^[PR #6056]
  `duplex_camera_frames()` 必须解包 `_video_frames_from_file()` 的 `(base_frames, stacked_frames)`，只返回
  flat base-frame list；这是 CPU fixture input-shape contract，不改变 runtime camera stacking、H100 E2E
  或模型行为。^[PR #6757]

- soft-interrupt 的 `response-required` artifact oracle 把“commit 前 response 后的 LISTEN”锚定为
  `input_audio_buffer.committed` **严格之前**最后一个 `response.done`，并要求该 LISTEN 严格落在
  两者之间；不能用全局最后一个 done，因为 follow-up 可在 commit 后继续 drain residual model unit。
  `listen_after_last_done` 仍独立锚定全局最后一个 done，故 residual fixture 还必须在该 late done 后有
  LISTEN。`model-policy` 模式仅要求 commit 前已创建的完成 audio response，不采用该完整 sandwich。
  验收用事件序列 `r1.done → LISTEN → r2.created → … → commit → r2.done → LISTEN`，断言
  `ok` 与两个 listen predicates；这只是 summarizer/test oracle，未改变 ACK、runtime interrupt、barge-in
  detection 或生产响应时序。A100 的已知失败仍是 `enough_responses=false`（barge-in 漏检），不是本 PR
  修复范围；H100-class 测试计划也不是跨硬件实时性证明。^[PR #6772] ^[Issue #6716]

共享 bridge/batch 规则见 [Model Executor rules](../../components/model-executor/rules.md)；
公开入口完整性见 [model adaptation guardrails](../../review/guides/model-adaptation-guardrails.md)。

## MCPMO-2e — audio_feature_lens 的切片维必须允许跨样本 ragged

- 触发：修改 `MiniCPMOAudioFeatureInputs.audio_feature_lens` 的 `TensorShape`、encoder batch 合批，或 `get_audio_hidden_states`/`get_audio_embedding_streaming` 对 per-audio lens 的消费。
- 强制：`audio_feature_lens` 声明 `TensorShape("bn", "s", dynamic_dims={"s"})`，允许同一 encoder batch 内不同音频有不同 slice 数（如 >30s 与 ≤30s）；消费侧继续 `hstack` + 按 `len(audio_feature_lens_raw[i])` 逐音频处理。仅放松 `"s"`；`"bn"` 与其他非动态维仍严格校验。
- 禁止：因 list 内 lens 长度不一致在 schema 层抛 `inconsistent shapes` 并拖垮 Stage-0 engine；把 ragged 误当成非法输入。
- 验收：直接构造混合 slice 数（如 3 vs 2）与同 slice 数两例，断言均可构造且不改变既有同长路径。^[PR #7071]

## MCPMO-2f — `audio_embeds` 必须注册为 audio，并以嵌入条数定 placeholder

- 触发：修改 MiniCPM-o 4.5 的 `MiniCPMOAudioEmbeddingItems`、`_get_prompt_updates`，或 OpenAI `audio_embeds` 内容部分。
- 强制：embedding items 的 `modality` 必须是 `"audio"`（不得从 image 类 copy 成 `"image"`）。单条 `audio_embeds` 张量为 `(s, h)` 时，placeholder 长度取 `len(single_audio_embeds)` / 前导维 `s`，再经 `get_audio_len_by_num_chunks`；不得 `sum(map(len, …))` 把 hidden size 累进 token 数。
- 禁止：以 `modality="image"` 注册 audio embedding；让 `(10, 4096)` 请求约 40960 个 placeholder 并撞 `max_model_len`。
- 验收：modality 断言为 audio；经真实 `_get_prompt_updates` 覆盖 1s/3s/30s 与不同 `h`，placeholder 只随 `s` 变化。^[PR #5730]

## MCPMO-2g — MiniCPM-o structured content 必须无分隔符拼接，对齐 native omni chat

- 触发：修改 MiniCPM-o 4.5 chat template、online serving `--chat-template`、image/audio/video 多模态 content parts，或 `minicpmo45_native.jinja` 打包。
- 强制：structured content 按 native `chat(omni_mode=True)` 直接拼接 text/media parts，不插入额外换行/分隔符；显式 text 空白保留。模板须作为 package data 发布，online/recipe 显式启用并设 `chat-template-content-format=openai`；image→speech 一致性用例保持 image-first。
- 禁止：依赖通用 string-content 渲染在 media 与 text 间加换行；只改测试不改 shipping template/package data。
- 验收：CPU 模板回归覆盖 structured parts；online image-to-text-audio 一致性在目标硬件通过。^[PR #7344]

## MCPMO-3f — Thinker forward 必须返回裸 hidden_states tensor

- 触发：修改 `MiniCPMO45OmniLLMForConditionalGeneration.forward`、Omni wrapper 的 shape 归一化，或 AR/`GPUModelRunner` 对 thinker 输出的消费。
- 强制：thinker `forward` 只返回裸 `hidden_states` `torch.Tensor`；`inputs_embeds is None` 时只构建一次 embeddings。wrapper 仅当 `ndim==3 and shape[0]==1` 时 squeeze batch 维，得到 `(seq_len, H)`，并保留 2D `(1, H)` decode 行。
- 禁止：返回 `(text_inputs_embeds, hidden_states)` tuple；用 `added_batch_dim` 误 squeeze 单 token 的 `(1, H)`；让 runner 用 `[0]` 把 embeddings 当最终 hidden 算 logits。
- 验收：断言返回类型/值、单次 embedding 构建，以及 `(1,H)/(N,H)/(1,1,H)/(1,N,H)` 归一化。^[PR #7517]
