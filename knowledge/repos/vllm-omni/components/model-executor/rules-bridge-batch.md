---
title: "跨 stage bridge 与 batch 合同"
created: 2026-09-04
updated: 2026-10-09
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #3422", "PR #3642", "PR #4795", "PR #5073", "PR #5074", "PR #5310", "PR #5792", "PR #5842", "PR #5957", "PR #5976", "PR #6422", vllm_omni/worker/gpu_ar_model_runner.py, vllm_omni/worker/gpu_model_runner.py, vllm_omni/model_executor/models/higgs_audio_v3/higgs_audio_v3_talker.py, vllm_omni/core/sched/output.py, vllm_omni/utils/mm_outputs.py, tests/model_executor/models/higgs_audio_v3/test_higgs_audio_v3.py, "PR #4765", "PR #5666", "PR #5491", "PR #6186", "PR #5452", "vllm_omni/worker/output/payload_build.py", "PR #6406", "PR #7781", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/omni_ar_model_runner.py#L531-L578", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/worker_v2/test_omni_ar_model_runner.py#L237-L253"]
confidence: high
---

# 跨 stage bridge 与 batch 合同

`bridge-batch` 审查组的 `EXEC-1a`–`EXEC-1i`：runtime info、跨 stage payload、batch
与 request RNG。触发条件和其余审查组见 [model-executor 共享规则](rules.md) 的
Direct 代码快速入口；loader 与 checkpoint 合同留在该页的 `EXEC-2x`。

## EXEC-1a — 从 producer 字段追到下一 stage consumer 和最终输出包装

- 触发：新模型、多阶段 pipeline、stage wrapper、runtime info 或 multimodal payload。
- 强制：逐段记录 runner 写入字段、传输后的字段名/shape、下一 stage 读取位置，以及最终
  `OmniOutput`/multimodal payload 的包装。loader 或模型 class 单独可调用不能代替真实
  stage handoff。
- 禁止：让 tuple waveform/hidden state 依赖 runner 的隐式猜测；bridge key 不一致时用
  fallback 掩盖。
- 验收：测试从真实 stage wrapper 输入开始，断言下一 stage 收到逐请求字段并得到公开
  输出类型。MiniCPM-o 的具体合同见
  [MiniCPM-o 4.5 规则](../../models/minicpm-o-4-5/rules.md)。 ^[PR #3642]

## EXEC-1b — stage 声明 batch 能力就必须逐请求消费 runtime info

- 触发：`max_num_seqs > 1`、batch handoff 或 wrapper 接收 `runtime_info` 列表。
- 强制：输出按请求索引与输入一一对应；无法安全逐请求处理时把并发上限显式收紧为 1。
- 禁止：只消费 `runtime_info[0]`，或把单元素 waveform/metadata 广播给整个 batch。
- 验收：至少两个不同输入的同批测试，分别断言 bridge、输出和错误归属；不能重复相同
  prompt 让串线不可见。partial downstream subset 必须保留原 `req_id_to_index`：跳过
  中间请求后，后续请求的 tensor slice 和 list-valued payload 仍取原 batch index，不能
  压缩到 downstream position。 ^[PR #3642] ^[PR #5310]

## EXEC-1c — 请求随机状态跨 batching 和 yield 保持请求所有权

- 触发：AR/talker adapter 接收 seed/sampling knob，或修改 batch compaction/reorder、逐 token loop。
- 强制：请求值在 adapter/model 构造前到达真实 sampling consumer；使用 request-local generator。
  若依赖必须临时改 global RNG，只能在无 yield 的窄上下文 save/restore。
- 禁止：deploy 默认覆盖请求 seed；共享 generator；每步复制完整历史或创建无界
  `batch*vocab` 临时量；global RNG 状态跨 yield 泄漏到兄弟请求。
- 验收：同 seed 同输出、异 seed 不同输出，batch reorder/compaction 后逐请求结果稳定；全局 RNG
  前后相同，计数器证明目标分支实际消费请求参数。 ^[PR #3422] ^[PR #5074] ^[PR #5792]
  Higgs V3 将每请求的八个 codebook rows 对齐扩展至 `SamplingMetadata.generators`，并把 GPU decode
  delay state 以 request ID（非临时 batch row）保存；finished/replacement 同 ID 时缺失 pool entry 必须
  触发重同步。^[PR #6422]

## EXEC-1d — cross-stage embedding buffer 必须按 ingress width 分配

- 触发：stage 的输入 embedding 在 model forward 或 preprocess 内投影，或修改 shared AR runner
  的 `inputs_embeds` buffer。
- 强制：buffer second dimension 来自 `model_config.get_inputs_embeds_size()`；它表示进入 stage
  时的 pre-projection width，与内部 `hf_text_config.hidden_size` 分开。没有 stage override 时
  helper 必须回退到内部 hidden size，保持其他 AR stage 的既有行为。
- 禁止：把内部 hidden size 当跨 stage wire shape；反过来用某模型的外部宽度扩大所有 stage；
  只测 config helper 而不覆盖发生宽度差异的正向 runner/model path。
- 验收：至少一个 ingress≠internal 的正向 case 与一个无 override control，断言真实 buffer shape
  和 projection consumer。Qwen2.5 Talker 当前接收 3584-wide Thinker state，并在 forward 内投影
  到 896；Qwen3 在 preprocessing 先投影，因此走 hidden-size fallback。PR #5073 的最终单测只有
  Qwen3 control，没有直接钉住 Qwen2.5 正向 buffer，后者仍是回归缺口。^[PR #5073]

## EXEC-1e — upstream registry 重名时 Omni override 与 plain-vLLM forward 必须同时成立

- 触发：上游开始注册 Omni 同名 architecture，或 model runner/forward/capture/dummy-run 接口变化。
- 强制：全局 registry 无条件重注册 Omni owner；非 staged 模式为 Qwen Omni 选择 thinker，并返回
  stock runner 可消费的 bare tensor；只有 staged talker consumer 存在时才 capture hidden layers。
  runner override 必须接受上游新增 kwargs；text/MoE dummy input 可按 upstream 要求 randomize，但
  code2wav 等结构化 codec id 只能接受参数而不得随机化。worker profiling 同步保存上游新增结果字段。
- 禁止：因 upstream 已有同名 arch 就跳过 Omni 注册；plain serve 返回 staged tuple；把 vocab-uniform
  id 喂给 codec codebook；用 `**kwargs` 隐藏未审查的签名漂移。
- 验收：registry collision 解析到 Omni class；Qwen2.5/Qwen3 plain thinker 与 staged capture 分别断言
  output shape，PP intermediate 原样透传；dummy-run 覆盖 text randomize 与 generation no-randomize，
  并对 profiling result 做字段 parity。^[PR #5976]

## EXEC-1f — multimodal wrapper 与 hash algorithm 必须通过 live context 传播

- 触发：upstream multimodal input 开始保留原始 bytes、修改 hash 签名或 processor item wrapper。
- 强制：所有 `get_mm_hashes` / direct `MultiModalHasher.hash_kwargs` 调用传当前 multimodal config 的
  algorithm；processor 通过公开 `get(index)` 解包 `MediaWithBytes`，再把 frames/metadata 交给 HF。
- 禁止：直接遍历 `.data` 绕过 unwrap；让 stage/replica UUID 前缀替代 content hash algorithm；只修
  一个模型而不 census 自定义 processor 与 direct hasher caller。
- 验收：不同 algorithm 进入 hash consumer；wrapped/unwrapped video 都生成同一 HF input，metadata
  保留；stage+replica scope 只包裹 base UUID 且用户 UUID 优先。^[PR #5976]

## EXEC-1g — request-end payload 延迟 D2H 必须先取得 device snapshot

- 触发：模型设置 `omni_payload_at_request_end`，或修改 full-payload accumulation、CUDA graph
  和 D2H 策略。
- 强制：只在显式 opt-in、无 prefix cache 且下游消费完整 payload 时延迟 D2H；每步
  clone device tensor，相容 list 可 pack 后以 views 复原，结束才跨设备，中间 output 为 `None`。
- 禁止：累计 graph/input-buffer alias，或扩散到普通逐步 payload。
- 验收：覆盖 source 被下一 step 覆写、ragged list shape/value/alias、普通路径与
  finish/abort 清理。^[PR #5957]

## EXEC-1h — pooling stage 的输入输出必须走显式跨 stage bridge

- 触发：新增非生成 pooling stage，或在 batch pipeline 中跨 stage 传递音频、文本、multimodal payload 与解码结果。
- 强制：producer 只在上游请求完成后按原 request index 构造输入，分词一次并随音频携带 `aligner_words`、language 和真实 duration；pooling runner 识别 `is_pooling_model`，绕过仅适用于 AR hidden state 的 Omni prefix-cache/mm 提取，decoder 输出固定的 int32 `[n_words, 2]`；最终 consumer 使用同一份 words、duration 和原 batch 映射。
- 禁止：把 pooling 请求当作 `generate` 或隐式广播单请求 metadata；按 downstream position 压缩原 batch index；重新分词后与 interval 做未验证的 positional zip；把空结果、解码失败或缺失 word metadata 伪装成 `[0, 0]` 的有效对齐；假设真实 `OmniRequestOutput` 暴露测试 fake 才有的 `additional_information`。
- 验收：用真实 stage wrapper、`PoolingOutput` 和 `OmniRequestOutput` 做 mixed-batch 测试，分别断言音频完成门槛、metadata/shape、duration clamp、原 request 标签和 mismatch→无时间戳；同时验证普通生成 stage 的输入输出不受影响。
^[PR #4795]

## EXEC-1i — 有状态跨 stage AR bridge 必须按 request 累积并完整消费 payload

- 触发：多 stage AR 模型在 `preprocess` 中按 request 保存 KV、音频帧或 TTS 状态，并通过 connector 传递累积 timeline/code payload。
- 强制：以 request id 隔离 session；无法证明 batch-safe 时显式限制 `max_num_seqs=1` 并在 request 完成/abort 时清理；下游每次采用累积 payload，单次 wake drain 所有尚未消费的位置，prompt-region 状态也必须先推进；code2wav 只接受真实 `[frames, 31]` codec codes。
- 禁止：按一个 wake 只执行一个新位置；把延迟或合并的 chunk 当作只含一个 token；缺失 connector payload 时把 placeholder `input_ids` 当 codec codes 解码；用静默截断或 clamp 掩盖错误 shape/range。
- 验收：覆盖 delayed/coalesced chunks、首个 PAD prompt chunk、zero-progress wake、最终 marker、缺失 payload、flat/二维 code shape、越界 code 与 session cleanup；断言 talker 与 code2wav 的真实输出字段和音频类型。
^[PR #5842]

相关执行流见 [model-executor architecture](architecture.md)；scheduler 侧的对应合同见 [scheduler 规则](../scheduler/rules.md)。

## EXEC-1j — side-channel 停止队列必须按 batch row 保持占位对齐

- 触发：有状态 AR 模型在 mixed prefill/decode batch 中通过 side-channel 队列把逐请求停止信号映射为 batch logits。
- 强制：每个 batch row 每步都向 `_results_queue` 放入一个位置项；prefill 使用 `(req_id, None)` 占位，`compute_logits` 按原 batch position 消费并在无信号或未越过停止阈值时强制 continue，消费后清空队列。
- 禁止：省略 prefill/new-request 占位、压缩或重排队列、把 `(continue, stop)` 概率直接当 logits，或依赖全 `-inf` 行的偶然 `argmax` 结果维持对齐。
- 验收：用同批 prefill+decode、停止阈值以下、空队列和多请求输入测试，分别断言每行的 continue/stop 结果、请求归属与队列耗尽；batch 顺序变化不能改变停止对象。^[PR #4765]

## EXEC-1k — AR runner 必须传递逐请求预算与内部采样 seed

- 触发：共享 AR runner 把逐请求 metadata 交给 `preprocess`，而模型在 `forward` 内采样或必须在 `max_tokens`/上下文容量的最后一个有效 step 发送 multimodal payload 时。
- 强制：从每个请求的 `sampling_params` 读取 `max_tokens` 与 `seed`，随 prompt 长度、已计算 token 数和 prefill 状态传入所有 preprocess 路径；模型用 scheduler cursor 与 `max_model_len` 判定最后输出，并将 seed 交给 request-local generator。
- 禁止：用当前 span 或模型本地计数器猜预算；只让 vLLM 外层 sampler 看到 seed；依赖 `on_requests_finished` 之后再 flush，此时 request 已离开输出 batch，payload 无法路由。
- 验收：通过真实 runner 的 mixed-batch 路径覆盖停止结束、预算结束和最后一个 in-flight request，断言每个请求的 committed frame 全部送达；覆盖同 seed、异 seed 及有无 batch 邻居的结果与 request 归属。 ^[PR #5666]

## EXEC-1l — typed AR-Diffusion tick 必须隔离协议身份并以完整 metadata 提交

- 触发：新增或修改实验性 AR-Diffusion tick/session、`extra_args` 传输、跨请求事件队列或 chunk 输出元数据。
- 强制：以不可变且深拷贝隔离的 `ARDiffusionTickRequest` 作为快照；`session_id` 跨 chunk 稳定，`event_id` 在会话内单调，`chunk_index` 连续，`request_id` 只标识一个 chunk。通过 `sampling_params.extra_args["ar_diffusion_tick"]` 传输，输出在 `multimodal_output["metadata"]["ar_diffusion"]` 返回；协议 request ID 必须与 `AsyncOmni` 的内部 engine routing ID 分离。只有输出元数据与提交快照完全一致后，才能提交事件、prompt、controls 和 chunk 状态；模型失败、元数据不匹配或 reducer commit 失败都必须转为 FAILED 并关闭 worker，后续只能显式 reset 或 close。
- 禁止：用内部 engine request ID 冒充协议 request ID；在模型输出验证前确认事件已应用；原地重试失败 chunk；让通用 session 层解析 LingBot camera/action schema；在没有 session-affine routing 时启用多副本 AR stage；把内部 tick contract 当作公共 HTTP/WebSocket 协议。
- 验收：覆盖乱序/重复/过期事件、队列背压、交错 `A0 -> B0 -> A1`、ID 分离、缺失或错误元数据、reducer commit 失败、失败后 reset、cleanup 失败 tombstone 与 close retry；同时验证 consumer 对非单副本 stage fail closed，并断言普通 `AsyncOmni` 输出 ID 合同不变。^[PR #5491]

## EXEC-1m — CFG 成对请求必须按 identity 保持 batch 行步进一致

- 触发：模型使用 classifier-free guidance，以 `cond`/`uncond` 两个 engine row 解码，或在 batch compaction、prefill/decode 与 request reorder 下维护成对状态。
- 强制：通过 `cfg_pair_id` 或注册的 companion suffix 从 external/user request identity 建立 pair；在 Scheduler 构造前安装通用 pairing patch；不完整 pair 必须等待，完整 pair 保持相邻并在每次 schedule 后 equalize progress、一起结束，同时按 pair/request id 保存状态而不是按 batch row 保存。
- 禁止：用 row adjacency 或 internal UUID 推断 pair；让 prefix-cache 不对称命中、chunked prefill 或单边 preemption 使两行错位；只结束一方、把 per-request seed/position metadata 广播给整批，或让非 CFG stage 被无条件改写。
- 验收：覆盖 suffix/显式 pair id、companion 缺失与到达、row reorder/compaction、chunked prefill、prefix-cache control、一起 finish、非 CFG no-op 和 patch 安装时序；同批不同 seed 与位置必须保持各自归属。 ^[PR #6186]

## EXEC-1n — 多请求 payload 与共享调度状态必须保持 request ownership

- 触发：修改 AR/generation runner 的 multimodal payload 路由、prefix-cache 合并、sparse `meta.req_id`，或模型 KV-transfer metadata 与 downstream payload memoization。
- 强制：先由共享 resolver 校验 nested/flattened sparse declaration，按 `meta.req_id` 建立 sparse index；payload builder 按 request id/index 取值并过滤协议 metadata，singleton list 只作 request-invariant broadcast。选出 per-request list 元素后，其内部 tensor 使用 request-local axis，递归处理保留该所有权，不再按全 batch 的 token offset 切片；合并模型 KV metadata 必须 copy-on-write，`omni_final_stage_id` 未到达前只能保守返回且不得缓存，获取失败原样抛出。
- 禁止：用 batch index 代替 sparse index、用 `v[0]` 替代越界多元素值、把请求数据广播给另一 request、因局部 tensor 长度恰等于 real/padded batch token 数而二次切片、原地修改 scheduler-owned dict，或吞掉 transfer metadata 异常。
- 验收：mixed batch 覆盖 sparse subset、combined prefix-cache/direct path、singleton broadcast、越界丢 key、nested/flat conflict、scheduler 原对象不变、marker 到达后 memoization 更新及异常传播；另覆盖 prefill/decode 两种顺序、局部 frame 数分别与 real/padded token 数相等及 nested list/dict，断言每个 request 收到完整的自身 payload。 ^[PR #5452] ^[PR #7781]

## EXEC-1o — generation runner 必须逐请求构造 batch 输出并保持 zero-token 控制流

- 触发：generation runner 支持并发请求，或修改 `sample_tokens`、`total_num_scheduled_tokens <= 0`、encoder transfer、DP dummy-run 与 KV no-forward 分支。
- 强制：tensor 的 leading dimension 与 list 长度必须等于 `input_batch.num_reqs`，逐请求构造 payload 并在 mismatch 时报告两侧长度；EC producer 分支必须先于 zero-token 分支，zero/negative-token step 仍按条件执行 DP `_dummy_run(1)` 或 `kv_connector_no_forward`。
- 禁止：用同时要求输出维度为 1 和 `num_reqs` 的断言限制 batch；构造只含一个 entry 的 payload；用无条件 zero-token early return 遮蔽同步、DP 或 KV-transfer 分支。
- 验收：覆盖两个及以上 request 的 tensor/list 输出、两类长度 mismatch、zero/negative-token、external-launcher + DP、KV no-forward 与 EC producer，分别断言 request 对齐、错误信息和分支调用顺序。 ^[PR #5452]

## EXEC-1p — runtime snapshot 替换必须由 capability 和 producer marker 双重控制

- 触发：shared GPU/NPU runner 消费跨 async-chunk 请求和 step 的 runtime additional information。
- 强制：模型 capability `replace_runtime_additional_information` 必须同时控制 scheduled-new 与 scheduled-cached 路径；替换 helper 写入 request-owned CPU mirror，只保留 `num_processed_tokens`、`resumable` 等 runner bookkeeping；未 opt-in 模型继续使用增量 merge。
- 禁止：让共享 runner 默认改用全量替换；把旧 terminal payload 合并回新 snapshot；只修 scheduled-new 而遗漏 cached admission；把 runner bookkeeping 当作模型 payload。
- 验收：覆盖 new/cached replacement、未标记增量 merge、request mirror 和 sibling request 隔离，并确认 NPU 继承路径使用同一替换实现。 ^[PR #6406]

## EXEC-1q — ragged stateful batch 必须维持输入行和 request-owned cache 的一一对应

- 触发：stateful generation/codec consumer 放宽 batch compatibility key，令同一 batch 可含不同当前 chunk 长度或 final flag。
- 强制：保留 input-row 顺序，按 row 维护下一状态；短行可在一次 masked/padded compute 中执行，超出既有 encoder window 的行必须回退到已有的 safe sliced path。任何缺失 audio/state row 都 fail closed，不能 filter 后压缩结果；平台 fast path 在不支持 ragged mask/valid-length 时必须回退。
- 禁止：按 batch 位置共享或错配 session cache；让 ragged path 绕过 RelPos/encode slicing；用过滤 `None` 的方式掩盖 hole，或把模型特有的 coalescing wait 写进 generic scheduler。
- 验收：混合长度、final flag、长行和正常行，逐 request 对照 exact path 的 audio/cache；注入中间缺行应报错而非重排。模型专有的 Code2Wav cache/dtype contract 见 MiniCPM-o 4.5 rules。^[PR #6021]
