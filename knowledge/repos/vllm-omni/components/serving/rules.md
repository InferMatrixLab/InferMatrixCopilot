---
title: "Serving 规则"
created: 2026-07-20
updated: 2026-10-09
type: rule
tags: [vllm-omni, components, serving]
sources: ["Issue #5369", "PR #3576", "PR #4583", "PR #4718", "PR #4834", "PR #4905", "PR #4912", "PR #5085", "PR #5157", "PR #5374", "PR #5670", "PR #5682", "PR #5713", "PR #5732", "PR #5746", "PR #5752", "PR #5843", "PR #5957", "PR #6008", "PR #6138", "PR #6202", "Issue #5811", "PR #6150", "claude-workflow-starter-private@09dca46", "zuiho-kai/claude-workflow-starter@c217fc6", .pre-commit-config.yaml, vllm_omni/entrypoints/async_omni.py, vllm_omni/entrypoints/omni_base.py, vllm_omni/entrypoints/openai/api_server.py, vllm_omni/entrypoints/openai/diffusion_request_utils.py, vllm_omni/entrypoints/openai/serving_chat.py, vllm_omni/entrypoints/openai/serving_speech.py, vllm_omni/entrypoints/openai/serving_video.py, vllm_omni/entrypoints/openai/video_api_utils.py, vllm_omni/entrypoints/openai/tts_adapters/, vllm_omni/engine/async_omni_engine.py, vllm_omni/engine/orchestrator.py, vllm_omni/engine/stage_pool.py, vllm_omni/engine/cfg_companion_tracker.py, vllm_omni/metrics/prometheus.py, tests/dfx/reliability/test_reliability_qwen3_omni.py, tests/engine/test_orchestrator_error_handling.py, tests/entrypoints/test_async_omni.py, tests/entrypoints/test_omni_entrypoints.py, tests/entrypoints/openai_api/test_api_server_guards.py, tests/entrypoints/openai_api/test_audex_serving_guards.py, tests/entrypoints/openai_api/test_omni_sleep_wakeup.py, tests/entrypoints/openai_api/test_serving_speech.py, tests/entrypoints/openai_api/test_tts_detection.py, tests/entrypoints/openai_api/test_video_api_utils.py, tests/entrypoints/openai_api/test_video_server.py, tests/tools/test_check_tts_adapter.py, tools/pre_commit/check_tts_adapter.py, "PR #4795", "PR #4755", "PR #3805", "PR #5878", "PR #6070", "PR #6122", "PR #4499", "PR #6050", "PR #6329", "PR #5999", "PR #5445", "PR #6288", "PR #6622", "PR #6499", "PR #6529", "PR #6776", recipes/MiniMaxAI/MiniMax-H3.md, "PR #7781"]
confidence: high
---

# Serving 规则

只有 `SERV-数字字母` 是可审计规则 ID。

## Direct 代码快速入口

- **SERV-0a — PR 描述先选代码地图。** Direct review 先按 title/body 声明的协议、请求字段或服务能力命中下表，再用 pinned changed files 校验真实 dispatcher 和范围；描述不能作为 finding 证据。
- **SERV-0b — 复用同一份 serving 证据。** 第一次 Codex review 打开命中函数后，把 request 对象、caller 搜索、测试和 findings 追加到同一份证据包；不得让专项重新扫描整个 serving 树。

| PR 描述在做什么 | 精确规则组 | 第一批 live 源码 |
|---|---|---|
| `extra_body`、flattened/nested/canonical/legacy 输入、alias、`negative_prompt`、diffusion request extras | `request-contract`：`SERV-4a`–`4h` | `vllm_omni/entrypoints/openai/diffusion_request_utils.py::{normalize_diffusion_request_args,apply_normalized_diffusion_request_extra_args}` → `serving_chat.py::{OmniOpenAIServingChat._preprocess_chat,OmniOpenAIServingChat.generate_diffusion_images}` |
| batched chat、fan-out/fan-in、sub-request ID、choice collapse、whole-batch error | `batch-chat-contract`：`SERV-4i`–`4k` | `api_server.py::create_batch_chat_completion` → `batch_serving.py::OmniOpenAIServingChatBatch` → ordinary chat completion children |
| `chat_template_kwargs`、raw HTTP/SDK `extra_body`、text/audio modalities、choices、空音频 | `chat-multimodal-contract`：`SERV-4c` + 命中模型规则 | upstream `ChatCompletionRequest` → `serving_chat.py::{OmniOpenAIServingChat._preprocess_chat,OmniOpenAIServingChat.chat_completion_full_generator,OmniOpenAIServingChat._create_text_choice,OmniOpenAIServingChat._create_audio_choice}` |
| endpoint restriction、route/app-state guard、capability、公开 400 | `endpoint-capability`：`SERV-4c`, `SERV-4d`, `SERV-5d`, [SERV-5s](rules-app-assembly.md#serv-5s-覆盖-upstream-http-route-前必须先移除同-methodpath-的旧-route) | endpoint policy → `api_server.py::build_app` assembled app → public handler |
| pause/resume、sleep/wake、partial stage/tag、ACK、generation admission、abort cleanup acknowledgment 或 shutdown-time abort race | `engine-lifecycle`：`SERV-5a`；[sleep/wake ACK 与 admission](rules-sleep-control.md)：`SERV-5b`, `SERV-5m`；[SERV-5t](rules-abort-lifecycle.md#serv-5t-acknowledged-abort-在-shutdown-边界只忽略已识别的-transport-closure) | `entrypoints/async_omni.py::{AsyncOmni.pause_generation,AsyncOmni.resume_generation,AsyncOmni.sleep,AsyncOmni.wake_up,AsyncOmni._abort}` → `engine/{async_engine_utils,stage_pool,orchestrator,async_omni_engine}.py` → AR EngineCore helper / diffusion worker RPC |
| stage/replica death、remote membership register/watch/detach、`EngineDeadError`、`StageUnavailableError`、health/readiness、eviction | `engine-lifecycle`：[SERV-5f](rules-fault-isolation.md#serv-5f-stage-death-按-replica-隔离request-fatal-不等于-process-fatal)、[SERV-5n](rules-engine-lifecycle.md#serv-5n-remote-replica-attachdetach-必须按本次-membership-generation-判定) | `engine/membership_controller.py` register/watcher generation → `engine/stage_pool.py` live set → `engine/orchestrator.py` poll/dispatch/cleanup → `entrypoints/{omni_base,async_omni}.py` error transport |
| serving class/factory 重构、optional adapter、diffusion/no-TTS 实例、warmup | `engine-lifecycle`：`SERV-5c` | `entrypoints/openai/serving_speech.py` 的所有 factory/`__new__` 路径 → `warmup`、voice upload/list、speech request caller |
| vLLM rebase、chat parser/renderer API moved or removed | `engine-lifecycle` | target-release `vllm.parser.*` / renderer API → `serving_chat.py` / `api_server.py`; remove a compatibility fallback only after the pinned base image is the target release ^[PR #6606] |
| TTS model detection、`stage_keys`/`model_archs`、adapter priority/topology、legacy migration | `engine-lifecycle`：`SERV-5e` | `tts_adapters/__init__.py::{iter_tts_detectors,detect_tts_model_type,all_tts_stage_keys,tts_entry_stage_archs}` → `serving_speech.py::_find_tts_stage` |
| request-level LoRA、AR-only/multistage、streaming input update、stage cardinality | `request-contract`：`SERV-4l` | `serving_chat.py` LoRA resolve → `AsyncOmni.generate` → `AsyncOmniEngine._build_add_request_message` → stage-0 input processor |
| Stage-0 prompt transform、downstream original prompt、临时 artifact ownership/cleanup | `request-contract`：[SERV-4q](rules-request-input.md#serv-4q-stage-0-prompt-transform-必须保留-downstream-原始视图并闭合临时-artifact-生命周期) | `AsyncOmniEngine._build_add_request_message` → `StageSubmissionMessage.request_artifact_dirs` → `OrchestratorRequestState` terminal cleanup |
| caller sampling params、pipeline `sampling_constraints`、stage config runtime extraction | `request-contract`：`SERV-4o` | `OmniBase.resolve_sampling_params_list` → `_get_sampling_constraints_list` → `_apply_sampling_constraints` |
| OpenAI integer bounds、msgpack overflow、request-local cleanup | `request-contract`：`SERV-4p` | protocol request models → orchestrator stage dispatch → `_fail_request_client_error` |
| SSE/streaming speech、audio format、PCM/WAV、speed、首 chunk 前校验 | `streaming-format`：`SERV-1a`, `SERV-1b` | `vllm_omni/entrypoints/openai/protocol/audio.py::{OpenAICreateSpeechRequest.validate_streaming_constraints,StreamingSpeechSessionConfig.validate_streaming_constraints}` → `serving_speech.py::{OmniOpenAIServingSpeech._validate_speech_streaming_request,OmniOpenAIServingSpeech.create_speech}` |
| speech `sample_rate`、adapter capability、resample 与 stream header | [SERV-9b](rules-speech-output.md#serv-9b-speech-sample_rate-必须由-adapter-capability-限定) | protocol → TTS adapter capability → `serving_speech.py` / `audio_utils_mixin.py` |
| video reference 解码、mixed media、frame conversion/mux、bounded memory | `media-ingress`：`SERV-1c`–`1e`, `1k` | `entrypoints/openai/video_api_utils.py` decode/coerce/encode helpers → video server callers |
| `ref_audio`、x-vector/ICL、content identity、artifact cache/readiness、waveform 存储与 array/list 隔离 | `artifact-readiness`：`SERV-3a`–`3c`；[SERV-3e/3f](rules-multimodal-cache.md#serv-3e-resolved-waveform-必须拥有独立且紧凑的缓存存储) | `serving_speech.py` reference resolve/decode/cache → adapter speaker cache → prefix salt |
| Prometheus、waiting/running gauge、replica stats、throttle、collector lifecycle、pipeline request gauge 或 image/diffusion metric emission | [metrics-lifecycle rules](rules-metrics.md)：`SERV-2a`, `SERV-2b`, `SERV-2e`, `SERV-2f` | `vllm_omni/entrypoints/omni_base.py::{OmniBase._log_summary_and_cleanup,OmniBase._process_stage_metrics_message,_publish_request_gauges}` → `vllm_omni/metrics/prometheus.py::{OmniPrometheusMetrics.__init__,set_running,set_waiting}` |

| 审查组 | 什么时候触发 | 规则 ID |
|---|---|---|
| `core` | 每次 serving 审查 | `SERV-4c`，见 [请求输入合同](rules-request-input.md) |
| `streaming-format` | SSE、audio streaming、format/default/capability | `SERV-1a`, `SERV-1b` |
| `media-ingress` | video reference、decoder registry/backend、mixed capability、bounded upload/conversion | `SERV-1c`–`1e` |
| `metrics-lifecycle` | metrics、gauge、replica、collector、pipeline request gauge 或 image/diffusion measurement boundary | [SERV-2a–2f](rules-metrics.md) |
| `artifact-readiness` | artifact/content cache、capability、ready/mark/discard、waveform 所有权 | `SERV-3a`, `SERV-3b`, `SERV-3c`；[SERV-3e/3f](rules-multimodal-cache.md#serv-3e-resolved-waveform-必须拥有独立且紧凑的缓存存储) |
| `chat-multimodal-contract` | chat template kwargs、SDK flatten、text/audio response shape | `SERV-4c`（见 [请求输入合同](rules-request-input.md)）+ 命中模型规则 |
| `endpoint-capability` | endpoint restriction、route/app-state guard、公开 400 | `SERV-4c`, `SERV-4d` 见 [请求输入合同](rules-request-input.md)；`SERV-5d` 见 engine lifecycle；`SERV-5s` 见 [app assembly](rules-app-assembly.md) |
| `engine-lifecycle` | pause/resume、sleep/wake、partial stage/tag、ACK、generation admission、abort cleanup/shutdown race、streaming raw terminal、event-driven orchestration、factory 状态矩阵、TTS adapter detection、replica membership/fault isolation | `SERV-5a`、`SERV-5c`–`SERV-5e`、`SERV-5g`–`SERV-5r`（`SERV-5m` 见 [sleep control](rules-sleep-control.md)，其余见 [engine 生命周期规则](rules-engine-lifecycle.md)）、[SERV-5t](rules-abort-lifecycle.md#serv-5t-acknowledged-abort-在-shutdown-边界只忽略已识别的-transport-closure)，`SERV-5f` |
| `full-duplex` | duplex opt-in、stage prewarm/fence、async-chunk、CFG companion、stage-keyed segment lifecycle，adapter-owned deferred cognition/session cleanup，或 startup warmup/realtime admission | `SERV-6a`–`SERV-6f` 见 [engine 生命周期规则](rules-engine-lifecycle.md)；`SERV-6g` 见 [duplex 编排规则](rules-duplex-orchestration.md)；`SERV-6h`、`SERV-6i` 见 [session lifecycle](rules-session-lifecycle.md) |
| `request-contract` | 请求字段、来源、冲突、dispatcher、consumer view、stage sampling constraints、serialization bounds、transform artifact ownership | `SERV-4a`–`4h`, `SERV-4l`–`4q`，全部见 [请求输入合同](rules-request-input.md) |
| `batch-chat-contract` | frontend fan-out、identity、choice cardinality、error/cancellation | `SERV-4i`–`4k`，见 [batch chat rules](rules-batch-chat.md) |
| `author-routing` | 只供 Direct reviewer 导航，不作为 finding 规则 | `SERV-0a`, `SERV-0b` |

## SERV-1a — 所有可预判错误在第一个 streaming chunk 前返回

- 触发：SSE/streaming 接口新增 format、audio、output modality 或 extra-body 参数。
- 强制：在 streaming/non-streaming 分叉以及发送首个 chunk 前完成合同校验，非法请求
  返回结构化 4xx。
- 禁止：先发送文本成功 chunk，再在 audio/encoder 分支发现参数非法并静默结束；客户
  端会看到 HTTP 成功但缺失后半段输出。
- 验收：坏格式在两种模式都得到相同 4xx；测试证明坏请求没有产生任何 SSE chunk，
  合法请求仍按协议完成。 ^[PR #4718]

## SERV-1b — 支持格式、默认值和 backend capability 只有一个来源

- 触发：protocol enum、validator、encoder/backend 分别维护 format/default 列表。
- 强制：protocol、校验器和执行层引用同一 canonical 常量，并以实际 backend capability
  为上限。
- 禁止：把 backend 不支持的格式列为合法值，再靠中途 fallback；不同文件各复制列表。
- 验收：每个公开格式都有 encoder smoke；移除格式只改 canonical owner，协议和校验测试
  同步反映。 ^[PR #4718]

## SERV-1c — media decoder 参数只在真实入口可达时对外声明

- 触发：替换 video/image reference decoder、接入 loader registry、新增 backend/fps/
  frame-selection 参数或改变坏媒体错误。
- 强制：只有请求/配置入口能把值传到 loader 时才声明参数可配；否则在最近
  loader 调用处固定实际值。frame sampling 只消费真正传入的 target metadata；decoder
  返回零帧必须继续映射为 `InvalidInputReferenceError`，不能因共享 loader 只记 warning
  就返回空 `VideoFrames`。
- 禁止：不得留下没有 caller 传入的 backend/fps 形参或死分支；不得因 registry 存在
  多 backend 就宣称 vLLM-Omni 已支持运行时选择；不得丢失替换前的非空帧 fail-safe。
- 验收：同一合成视频分别断言 first-N 与 last-N 顺序、数量和 fps；覆盖非法
  `keep`、非正 `max_frames`、无效字节与 loader 零帧，且真实 Qwen multimodal/video
  请求仍通过公开入口。 ^[PR #5085]

## SERV-1d — mixed media 必须 capability-gated，request 错误在解码前限界

- 触发：共享 video serving 接受 mixed image/video/audio、typed URL/data URL 或 multipart upload。
- 强制：unknown model 默认无 mixed capability；只有 model metadata 明示支持才允许 image+video。
  multipart 大小/格式等可在字节边界判定的约束必须 bounded-read 并在解码前拒绝；pipeline 的
  request-facing validation 使用可跨 worker 保留 client metadata 的错误类型，统一映射为 400。
- 禁止：删除旧互斥 guard 后让所有 diffusion 模型继承 mixed input；用 unbounded `read()` 后
  再检查；让 plain `ValueError` 穿过 stage worker 变成 500。
- 验收：支持模型的 mixed request 成功，unknown/不支持模型在 engine 前返回 400；multipart
  oversize/bad format 与 pipeline count/shape 错误保持同一 client-error 合同。typed URL/data URL
  是否同样 bounded 必须单独证明；本 pin 的 helper 仍完整读取。模型专有 allowlist、temp-file
  与 input matrix 见 [MiniMax H3 rules](../../models/minimax-h3/rules.md)。 ^[PR #5752]

## SERV-1e — 长视频转换只保留必要的全视频 buffer，并逐帧保持旧语义

- 触发：修改 video output 的 normalize、float→uint8、RGBA strip、mux 或 fragmented encode。
- 强制：预分配最终 contiguous uint8 video，逐帧 clip/scale/round 写入，禁止先 `np.stack`
  全量 float frames 再产生全量转换临时量；这只消除额外副本，原 normalized float frames、最终
  uint8 buffer 及上游 device→host 分配仍存在，不能称为 O(1) memory。
- 强制：所有 frame shape 相同；只有 rank-3 HWC 且 C=4 才去 alpha，二维 width=4 灰度不能
  截断。mixed float dtype 先用 `np.result_type` 得到 common dtype，再逐帧计算，以保持 legacy
  stack 的 promotion/rounding/checksum；不得原地改输入。仅当输入是 `np.ndarray`、`uint8`、rank-4
  FHWC、last dim=3 且 C-contiguous 时，fast branch identity-return；其他输入一律回既有 conversion。^[PR #6824]
- 验收：回归测试禁止 float path 调用 `np.stack`，并覆盖 input immutability、RGBA、width-4
  grayscale、mixed float16/float32 与 exact uint8 output。性能证据必须分开报告 conversion 和
  未改动的 MP4 encode：PR #5732 的可复现实验仅绑定 209×1344×768 float32 RGB、24 FPS、
  ultrafast、fresh process、3 次 median/RSS 10 ms；conversion 1201.15→529.47 ms、conversion+MP4
  1808.96→1235.26 ms、peak RSS 8.874→4.141 GiB、above-resident 5.393→0.660 GiB，两个 hashes
  相同。PR body 的 362-frame 与 4×MI300X E2E 是另一组观察，不能混合或泛化。另须断言 exact
  eligible ndarray 返回同一对象，并以 dtype/rank/channel/contiguity controls 证明其他输入仍转换。^[PR #5732] ^[PR #6824]

## SERV-1f — TTS word timestamps 必须按 transport capability 显式门禁和传输

- 触发：公开 `word_timestamps`、forced-aligner CLI/config，或修改 HTTP non-stream、SSE 和 WebSocket speech transport。
- 强制：服务启动时统一记录 aligner capability；无 aligner 时 `word_timestamps=true` 明确返回 400。HTTP non-stream 将短 JSON 时间戳放入 `X-Word-Timestamps`，超过 4096 bytes 用可检测的 `X-Word-Timestamps-Omitted`；WebSocket 仅在 `stream_audio=true` 且 PCM 合同满足时，在每句音频结束后发送 trailing timestamps frame。
- 禁止：无 aligner 时静默返回 200 音频；让 `stream=true` 的 HTTP 路径伪装支持该字段；把长 header 静默丢弃；以 in-process sidecar 或离线示例替代 pipeline stage 的真实输出。
- 验收：覆盖 non-stream、HTTP streaming、WebSocket、无/有 aligner、短/超限 alignment 和多句音频；分别断言 400、header/frame 内容、超限标记，以及音频在时间戳缺失或超限时仍按合同返回。
^[PR #4795]

## SERV-1g — TTS voice 输入、占位 default 与可用 speaker 必须闭环

- 触发：修改 OpenAI speech 的 `voice` 输入校验、VoiceID 归一化、可用 voice 列表、上传 speaker 或无 speaker 模型的默认 voice 语义。
- 强制：协议层同时接受字符串和包含 `id` 的 VoiceID 对象，并在 serving 校验前归一化为小写名称；可用 voice 列表必须统一包含内置 speaker、上传 speaker 和 `default` 占位项。无实际 `default` speaker 时，`default` 请求必须被接受并转换为后端忽略的无 voice 请求；存在同名真实 speaker 时必须优先使用真实 speaker；其他非法名称返回结构化 400。
- 禁止：要求没有可用 speaker 的模型提供一个实际存在的 voice 名称；让 voice 列表遗漏 `default` 或上传 speaker；把 `default` 占位值传入后端作为真实 speaker；仅支持字符串而拒绝合法 VoiceID 对象。
- 验收：覆盖无上传 speaker 时的字符串和 `{\"id\": ...}` 请求、未知 voice 的 400、上传前后 `default` 与新增 speaker 的列表和语音请求，以及注册并删除同名 `default` 后占位行为恢复；另需验证同名上传 voice 的实际使用，而不只断言请求成功。 ^[PR #5878]

## SERV-1h — I2V resize ownership 必须按 checkpoint version 解析

- 触发：在线视频 API 处理 I2V reference image，且 pipeline 的预处理可能随 checkpoint version 改变。
- 强制：serving 从 active diffusion `od_config` 取得 `model_class_name`、`model` 和 `revision`，通过 model_extras resolver 判断 resize ownership；只有 LTX-2.5 保留 source geometry 交给 pipeline，LTX-2/2.3 继续在 serving 侧 resize，随后由 pipeline 执行其版本化 conditioning。
- 禁止：以共享 `LTX2Pipeline` class 全局开启 preserve-image-size；按请求目标尺寸或路径名猜版本；跳过 revision；未经版本化回归就让 online 与 legacy offline I2V framing 政策改变。
- 验收：用 2、2.3、2.5 checkpoint metadata、Full/Distilled class、pinned revision 和 off-aspect image 覆盖 HTTP I2V，断言 legacy resize、2.5 source geometry、CRF 委托及最终尺寸各自符合合同。^[PR #6070]

## SERV-1i — image reference 重定向必须遵循媒体 URL 策略

- 触发：`decode_image_url` 处理 HTTP(S) `image_reference.image_url`，或修改媒体 URL 重定向策略与下载错误映射。
- 强制：读取 vLLM 的 `envs.VLLM_MEDIA_URL_ALLOW_REDIRECTS` 并传给 HTTPX 的 `follow_redirects`；策略关闭时在收到 3xx 后拒绝请求且不得请求重定向目标；最终非成功 HTTP 状态与网络/请求失败必须分别映射；data URL 继续走本地解码路径。
- 禁止：硬编码或绕过媒体重定向策略；策略关闭时仍跟随可能跨主机的重定向；把 HTTP 状态错误与连接失败混为同一诊断；为 data URL 创建 HTTP client。
- 验收：用确定性的 HTTPX transport 覆盖允许重定向并请求原路径和目标路径、禁用重定向且目标未被请求并在错误中指出 `VLLM_MEDIA_URL_ALLOW_REDIRECTS`、最终 404 与连接失败的错误区分，以及 data URL 不创建 HTTP client；再通过公开 `/v1/videos` image reference 回归验证解码图像进入 engine。该验证不覆盖未修改的 video/audio reference 或 image-edit fetcher。 ^[PR #6122]

## SERV-1j — 非流式 speech raw-audio 响应通过 headers 暴露 usage

- 触发：修改 `/v1/audio/speech` 非流式 raw-audio 响应、speech usage 计算或 usage headers，或需要对齐 streaming/batch 的 token 统计语义。
- 强制：非流式 raw-audio 响应必须复用 `build_speech_usage` 生成的 `SpeechTokenUsage`，在 usage 可用且服务非 diffusion mode 时发送 `X-VLLM-OMNI-INPUT-TOKENS`、`X-VLLM-OMNI-OUTPUT-TOKENS`、`X-VLLM-OMNI-TOTAL-TOKENS`、`X-VLLM-OMNI-INPUT-TEXT-TOKENS` 和 `X-VLLM-OMNI-INPUT-AUDIO-TOKENS`；input 使用 text 与 ICL audio tokens，output 使用生成的 codec/audio tokens，raw audio body 保持不变。
- 禁止：把 usage JSON 塞入 raw-audio body；用原始 stage prefill 长度替代共享 usage 语义；在 streaming headers 已发送后补发尚未确定的最终 totals；宣称 diffusion-mode 路径必然提供这些 headers。
- 验收：测试非流式请求返回 `200`、raw audio body 未改变且五个 headers 的值与 `SpeechTokenUsage` 精确一致；覆盖 usage 不可用时不生成 headers，并回归 streaming 与 batch 的 usage 语义及 diffusion-mode 的无 headers 行为。^[PR #4499]

## SERV-1k — 非流式 MP4 编码必须按运行时能力自动选择并保留回退语义

- 触发：修改非流式 MP4 视频响应的 normalize、frame layout/dtype 判断、mux、raw/base64 输出或 fallback 行为。
- 强制：公共 dispatcher 先完成一次帧准备和形状校验。共同 direct 条件是所有帧具有相同的正 HWC 形状、3/4 通道和支持的 common dtype（`uint8`、bool 或浮点）。C-contiguous RGB channel view 可直接写入 PyAV GBR planar 帧；strided RGB planes 仅可由 server-owned、`max_workers > 1` 的 converter 接受，并必须复制到其 owned scratch/PyAV planes，保持相同量化语义。没有 converter 或 `max_workers == 1` 时，strided planes 必须在打开 PyAV container 前以稳定 reason `non_contiguous_rgb_planes` 选择 `legacy_fallback`。raw MP4 和 base64 共用该 dispatcher，不提供 CLI、模型声明或请求级编码策略；流式 fMP4 保持原路径。
- 强制：converter 的 scratch 是每个 worker thread 的实例本地存储；`iter_frames()` 按 FIFO 产生帧，并且一次调用最多保留 `2 * min(frame_count, max_workers)` 个 pending futures。无论 mux 成功还是抛错，调用方都必须关闭 iterator 以 cancel remaining futures；server `finally` 关闭 video handler，handler shutdown 必须幂等并以 `wait=True, cancel_futures=True` 关闭 executor。日志报告 effective frame-conversion workers（legacy 为 0）。
- 禁止：为能力探测再次 normalize/copy 视频；在 direct mux 开始后因异常重试 legacy；把非法形状或不支持 dtype 静默送入 planar path；不得把 async route 仍同步执行 mux/base64 或单请求 pending 上限说成全请求 nonblocking、全局并发上限、普遍模型/process/performance 改进。共享 pool 只在一个 API process 内限制 conversion threads；多 process 仍各自拥有 pool。
- 验收：覆盖无 converter、1 worker、8 workers 的 interleaved routing，及 channel-first-backed direct、非法 shape/dtype fallback、RGB contiguity、GBR plane 与 padded stride、FIFO、future bound、iterator close 后 cancel、reused executor 幂等 shutdown、direct failure 不重试；音频存在时 serving resolver 才以 24 kHz 为缺省。固定输入下有无音频的 parallel-interleaved/legacy MP4 必须 byte-identical 并通过 ffprobe/完整 FFmpeg decode，raw/base64 传同一 persistent converter，streaming 行为不变。^[PR #6288] ^[PR #6499] ^[PR #6776]

## SERV-2e — 指标发射必须保持测量边界、缺失语义与请求唯一性

- 触发：新增 image/diffusion Prometheus family，或修改 `OmniBase` terminal result、
  `OrchestratorAggregator`、scheduler snapshot、失败清理或 stage/replica membership。
- 强制：只在同一 finished result 的首次消费中发射 stage workload；request-finalize
  family 由 e2e guard 发射一次。保留已测得的零 queue duration，但没有来源的 optional
  profiler/KV/memory measurement 不发射。failure counter 在 abort、disconnect、stage error
  和 cleanup 之间去重，并将 reason 归一到有界 taxonomy。waiting gauge 只求 live replicas
  的最新快照，且 abort/error、death 和正常 unregister 都必须刷新或移除旧快照。
- 禁止：把缺失值补成零、重放 terminal message 后重复加 Counter/Histogram、把已移除
  replica 的历史 waiting 值留在总和，或把 `stage_gen_time` 当纯 DiT forward 时间。
- 验收：覆盖 finished-result replay、present-zero 与 missing、错误/abort cleanup、正常及
  dead replica unregister、一次请求的多个 failure path，以及 image-only workload guard。
  profiler-off 与 profiler-on 均须断言各自可观测合同。^[Issue #5811] ^[PR #6150]

## SERV-2d — 图像生成与编辑响应必须透传 diffusion 指标

- 触发：修改 `/v1/images/generations` 或 `/v1/images/edits` 的 diffusion generation result 解包、指标字段或 OpenAI 响应构造。
- 强制：单 stage 从 result 属性、multi-stage 从返回 tuple 捕获 `stage_durations` 与 `peak_memory_mb`，并在两个图像端点的响应 `metrics` 中透传；指标不可用时保留 `null`，内存值按稳定的数值类型输出。
- 禁止：让 `edit_images()` 丢弃已生成的阶段耗时或峰值内存；只修复 generations 路径，或以 HTTP 成功代替指标字段的传播证明。
- 验收：对 generations 和 edits 分别覆盖 single-stage 与 multi-stage stub 结果，断言响应状态为 200，且 `metrics.stage_durations`、`metrics.peak_memory_mb` 与 generation result 精确一致；同时覆盖指标缺失时的 `null` 响应。^[PR #5999]

## 缓存 readiness 与失败隔离

### SERV-3a — readiness 必须表达 artifact 能力而非仅表达 identity

- 触发：serving 层按 URI/key 缓存预处理 artifact，并在后续请求剥离原始输入。
- 强制：readiness key 或 capability predicate 必须包含 consumer 真正需要的 mode/字段；
  只有 artifact 已具备全部必需字段时，才能进入 artifact-only 路径。
- 禁止：因为 `artifact_key` 相同就跨模式复用；原始输入已剥离后再让 worker 尝试补算
  缺失字段。
- 验收：覆盖“能力不足 artifact → 更强请求”与“能力超集 artifact → 较弱请求”，前者
  保留原始输入并重新计算，后者是否复用必须作为明确性能取舍。Qwen3-TTS 的方向性
  合同见 [Qwen3-TTS 规则](../../models/qwen3-tts/rules.md)。 ^[PR #5157]

### SERV-3b — readiness 状态迁移与错误存活性一起验证

- 触发：修改 artifact ready/track/mark/discard、失败清理或 eviction。
- 强制：所有状态入口使用同一 key/capability 合同；请求级 prompt/build 错误不得杀死
  engine，后续健康请求仍应成功。resolve entry 替换/淘汰后，仅最后一条引用消失时才清理对应 artifact readiness。
- 禁止：只改 ready 查询而遗漏 mark/discard；只测同模式 cache hit，不测跨模式顺序和
  counterfactual failure；共用 artifact 的别名 entry 仍在时提前使 readiness 失效。
- 验收：单测枚举状态迁移；E2E 复现原始坏顺序、证明修复后存活，并在回退修复代码时
  重新出现目标错误，避免测试空跑。两个 locator 共用 artifact 时，替换/逐出一条保留 readiness，最后移除才清理；核对替换与 entry/byte eviction 的字节计数。 ^[PR #5157] ^[PR #7781]

### SERV-3c — 内容身份必须原子贯穿全部缓存层

- 触发：本地/inline reference audio 或其他 locator 同时经过 decode、speaker/code 与 prefix cache。
- 强制：先通过 allowed-local-media gate，再规范解析 URI/stat；本地身份至少含 size 与
  `st_mtime_ns`。decoded payload 与 identity 一次计算、一起返回，并把同一身份贯穿每层 cache key
  和 prefix salt；named voice 加 inline ref 按真实分支区分。
- 禁止：raw locator 充当内容 salt；授权前 stat；两次独立 decode/stat；用 client-controlled
  locator 建无界共享 side table；把完整 base64/路径写日志。
- 验收：同长度覆盖、并发改写、named+inline、speaker cache 和 prefix hit 均不复用旧内容；
  非法路径不 stat，日志只含截断值且命中真实 logger hierarchy。 ^[PR #5670]

### SERV-3d — pure-diffusion speech 必须在 factory 固化 media policy

- 触发：pure-diffusion speech 绕过普通 `OpenAIServing` 初始化，或修改 `ref_audio` 的
  `MediaConnector`、local-media allowlist 或 factory 参数。
- 强制：app bootstrap 将 `allowed_local_media_path` 和 `allowed_media_domains` 原样传给
  speech factory；diffusion instance 在 factory 中创建并持有按该配置构造的 connector，且其
  local cache-key stat 使用同一 allowed path。普通 multi-stage instance 仅在首次需要时从
  `model_config` 构造 connector，不能要求 diffusion instance 拥有 `model_config`。
- 禁止：diffusion 分支以无配置 connector 加载本地 `file:` URI；只把 allowlist 传到
  `MediaConnector` 而遗漏 cache-key metadata gate；每次解析重新构造 connector 并漂移策略。
- 验收：pure-diffusion bootstrap 断言两个 media 参数到达 speech factory；在 allowlisted
  本地 ref-audio 上断言 connector 只按该配置创建一次，文件 size/mtime 改变后 cache miss 并
  refetch；普通 multi-stage 路径仍从 `model_config` 延迟构造。 ^[PR #6622]

### SERV-11a — 用户输入日志必须默认降级并受限输出

- 触发：修改 TTS、audio generation 或 diffusion chat serving 请求日志，以及 `request_logger` 或 `max_log_len` 的行为。
- 强制：INFO 级别只记录 request ID、模型类型、voice clone、reference image 和参数等元数据；用户输入仅在 `self.request_logger` 存在时通过 `logger.debug` 输出，并按 `max_log_len` 预留日志前缀长度进行截断；未配置上限时使用 200 字符默认上限。
- 禁止：在 INFO 日志中写入原始 `text` 或 `prompt`；无条件输出 DEBUG 用户内容；使用固定预览长度替代请求日志开关；在未配置 `max_log_len` 时输出无界用户输入。
- 验收：覆盖 TTS、diffusion TTS、audio generation 和 diffusion chat，断言 INFO 不含用户内容；启用 `--enable-log-requests` 时 DEBUG 才输出内容，且配置与未配置 `max_log_len` 时均遵守截断上限。 ^[PR #6329]
