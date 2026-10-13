---
title: "Scheduler 规则"
created: 2026-07-16
updated: 2026-10-09
type: rule
tags: [vllm-omni, components, scheduler]
sources: ["PR #8459", "PR #8259", "PR #5957", "PR #5976", tests/core/sched/test_omni_ar_scheduler_stale_drain.py, tests/core/sched/test_omni_ar_scheduler_streaming.py, "vllm-omni-rebase-agent@122a9468:agent/skills/fix-talker-truncated-prefill-prefix-cache-key-cap/SKILL.md", "vllm-omni-rebase-agent@122a9468:agent/skills/gpu-hang-low-max-num-batched-tokens/SKILL.md", vllm_omni/worker/gpu_ar_model_runner.py, vllm_omni/core/prefix_cache.py, vllm_omni/utils/mm_outputs.py, vllm_omni/core/sched/omni_ar_scheduler.py, vllm_omni/core/sched/omni_generation_scheduler.py, vllm_omni/core/sched/omni_scheduler_mixin.py, vllm_omni/core/sched/omni_scheduling_coordinator.py, vllm_omni/core/sched/output.py, tests/core/test_prefix_cache.py, tests/core/test_prefix_cache_async_write.py, tests/core/sched/test_omni_scheduler_mixin_shared.py, tests/core/sched/test_omni_scheduler_mixin_timeouts.py, tests/distributed/omni_connectors/test_chunk_transfer_adapter.py, tests/utils/test_mm_outputs.py, tests/entrypoints/test_omni_new_request_data.py, "PR #4106", "PR #5310", "PR #5461", "PR #4795", "PR #5842", "PR #6021", "PR #6033", "PR #6089", "PR #6149", "PR #6360", "PR #6406", "PR #6150", "PR #6619", "PR #6680", "PR #6626", "PR #6529", tests/core/sched/test_omni_ar_scheduler_aborted_queue_sweep.py, tests/core/sched/test_omni_scheduler_streaming_input_counter.py, tests/core/sched/test_omni_sched_deferred_free_fence.py, "PR #6831", tests/e2e/online_serving/test_nemotron_voicechat_duplex.py, "PR #7781", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_generation_scheduler.py#L112-L133", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_generation_scheduler.py#L190-L213", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_generation_scheduler.py#L244-L262"]
---

# Scheduler 规则

只有 `SCHED-数字字母` 是可审计规则 ID。运营 runbook 以 rebase-agent 仓库为准，
本页是知识树沉淀快照（2026-07-16，agent @122a9468；skills 工作树含未提交遥测更新，
快照以工作树为准）。

## Direct 代码快速入口

PR 描述先命中下表，再打开对应规则组和首批源码；changed files 强制校验并补齐是否还
跨到 Distributed、Serving 或 Model Executor，描述与 diff 冲突时以后者为准。

| PR 描述信号 | 规则组 | 第一批源码 |
|---|---|---|
| prefix cache、truncated prefill、mm key、deferred/passthrough payload | SCHED-1a/1b | `core/prefix_cache.py::OmniTensorPrefixCache`；`utils/mm_outputs.py::{build_mm_cpu,to_payload_element}`；`worker/gpu_ar_model_runner.py::_deferred_prefix_cache_mm_keys` |
| `max_num_batched_tokens`、prefill throttle、低预算 GPU hang | SCHED-2a | `core/sched/omni_ar_scheduler.py::OmniARScheduler.schedule`；`core/sched/omni_generation_scheduler.py::OmniGenerationScheduler.schedule`；触发它的 deploy/test 配置 |
| vLLM bump、scheduler rebase、KV connector stats | SCHED-3a | 两个 scheduler 的 `schedule` / `update_from_output` 与 live upstream `vllm/v1/core/sched/` |
| side-stream D2H、pinned host tensor、源 buffer 复用 | SCHED-4a/4b | `worker/gpu_ar_model_runner.py::_copy_tensor_payload_to_cpu`、`_get_or_create_omni_payload_copy_stream`；`core/prefix_cache.py` 的 async copy 路径 |
| sampled-token logprobs、spec decode trim、request-local output error | SCHED-5a | `core/sched/omni_ar_scheduler.py::_slice_sampled_logprobs`、`update_from_output` |
| stateful async chunk、full-payload input、KV cleanup | SCHED-5b/5c | `core/sched/omni_generation_scheduler.py`、`omni_scheduling_coordinator.py`、`omni_ar_scheduler.py::_free_request` |
| native chunk 的 terminal step、空 token prompt 与真实 code payload | [SCHED-STREAM-1b/1c](rules-stream-terminals.md) | `omni_scheduling_coordinator.py::get_scheduled_input_terminal_req_ids` → generation scheduler admission/finish |
| KV extraction wait、connector acknowledgement、partial interval cleanup | SCHED-5h | `core/sched/omni_ar_scheduler.py::{_free_request,update_from_output}` → scheduler stats → orchestrator metrics |
| consumed chunk validation failure、receive ledger、live request termination | SCHED-5i | `core/sched/omni_scheduler_mixin.py::_process_chunk_receive_failures` → `chunk_transfer_adapter.py` |
| async stop、streaming update、scheduled/async stale token fence、abort queue/counter drift | SCHED-6a/6f | `core/sched/omni_ar_scheduler.py::{_handle_stopped_request,update_from_output}` → scheduler mixin abort sweep/counter resync → streaming/stale tests |

若描述只写模型症状，先从模型 owner 找到 payload producer/consumer；只有实际断点落在调度、
prefix cache 或 copy lifetime 时才把 Scheduler 加为 owner。

## SCHED-1a — prefix cache 的关键 key 必须显式声明，禁止抬 cap 或关缓存

skill 元数据：`fix-talker-truncated-prefill-prefix-cache-key-cap`，
modules=[worker_runner, model_executor]，status=active，run_count=54，2026-06-10 创建 / 07-11 最后使用。

- 症状：`tests/e2e/online_serving/test_qwen3_omni.py::test_mix_to_text_audio_001[default]`
  失败；stage-1（`StageEngineCoreProc_stage1_replica0`）死于
  `RuntimeError: The size of tensor a (6) must match the size of tensor b (9) at non-singleton dimension 0`，
  位置 `vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py::_get_talker_assistant_parts`
  （`input_embeds = assistant_text_hidden + assistant_codec_hidden`）。因 OmniServer fixture
  是 session 级、stage-1 已死，同文件其余用例随即全部 `EngineDeadError`，watchdog 杀
  pytest（rc=143）。shape 解码：`assistant_codec_hidden` 恒为 9 行；
  `assistant_text_hidden`==6（0 + 4 pad + 1 BOS + 1 zero-fill）意味着
  `thinker_embed[im_start_index:segment_end_index]` 为**空**——stage-0 发来的
  `embed.prefill` 远短于 prompt。
- 诊断（跨 stage 截断通用配方）：
  1. 在 stage-0 日志 `grep "Skipping mm prefix cache key"` —— 命中
     `hidden_states.layer_0` / `hidden_states.layer_24`（embeddings 层与
     accept_hidden_layer 层）即适用本规则。
  2. payload 遥测 `grep "_send_single_request"`：健康时 mix 测试所有请求 ~21–22 MB；
     损坏时首个请求 ~21 MB（cache miss），随后同 prompt 请求骤降 ~1.4 MB
     （prefix 命中、只剩后缀行）。
  3. 先归因再调试：与**上一次通过的 run** 同一测试日志对比
     （`rebase_logs/runs/<prev-run>/tests/00_omni_qwen3-omni_test.log`）；核对 vLLM 版本行
     `Initializing a V1 LLM engine (vX.Y...+g<sha>)` 是否一致——一致则回归在 vllm-omni
     树（合入的 origin/main 或模块 agent 提交）而**不是** vLLM bump；
     `git log -S"<告警文本>" --all` 快速定位引入提交。
- 根因链（首见于 vllm-omni #3689 / `57227dc7`）：测试用 `--stage-overrides` 强制打开
  stage-0/1 的 prefix caching；qwen3-omni thinker 把逐 token
  `hidden_states.layer_0`（embeddings）与 `hidden_states.layer_{accept_hidden_layer}`
  打进 thinker→talker payload；prefix 命中时只执行后缀 token，前缀行必须从
  `OmniTensorPrefixCache` 重建；#3689 加了逐 key 512 MiB cap
  （`_MAX_MM_CACHE_BYTES_PER_KEY`，为 qwen3-tts 类模型设的 OOM 防线）——大 KV cache
  下（如 L20X 682,896 token → 2048 维 bf16 每 key 2667.6 MiB）两个 layer key 被
  **静默丢弃**，prefix 命中的请求带着截断的 `embed.prefill` 在 talker 侧爆 shape。
- 修法（skill 的 4 处小改，模型声明豁免，沿既有
  `requires_full_prefix_cached_hidden_states` / `deferred_prefix_cache_mm_keys` 模式）：
  1. `vllm_omni/core/prefix_cache.py` —— `OmniTensorPrefixCache.__init__` 增
     `required_mm_cache_keys: set[str] | None`；`maybe_init_missing_mm_cache_keys`
     中该集合内的 key 绕过 cap（改打 `warning_once` 报实际 MiB，而不是跳过）。
  2. `vllm_omni/worker/gpu_model_runner.py` —— `initialize_metadata_builders` 构造
     cache 时传 `required_mm_cache_keys=set(getattr(getattr(self, "model", None),
     "required_prefix_cache_mm_keys", ()) or ())`。
  3. `qwen3_omni.py` thinker 分支 `__init__`：声明
     `self.required_prefix_cache_mm_keys = {"hidden_states.layer_0"}`，再取
     `accept_layer = getattr(talker_config, "accept_hidden_layer", None)`，仅当 `accept_layer is not None` 时加
     `f"hidden_states.layer_{int(accept_layer)}"`。
  4. `tests/core/test_prefix_cache.py` —— 单测把 cap monkeypatch 到 1 字节，断言
     required key 仍被缓存、可选超大 key 被跳过。
  - key 命名：`flatten_payload`（`vllm_omni/data_entry_keys.py`）把
    `{"hidden_states": {"layers": {N: t}}}` 展平成 `"hidden_states.layer_N"`，
    **声明展平后的名字**。
  - main @5c390096 现状：布尔合同 `requires_full_prefix_cached_hidden_states`
    （`gpu_ar_model_runner.py:603`，qwen3_tts/higgs_v3 talker 显式 False）与集合
    `deferred_prefix_cache_mm_keys`（:692/:730，模型声明如
    `qwen3_tts_talker.py:321` `{"codes.audio"}`）已在；skill 的 `required_*` 命名
    在该提交尚未出现——按 skill 的 Watch Out，该修复属 vllm-omni upstream，需提/跟
    upstream PR。
- 验证：`python -m pytest tests/core/test_prefix_cache.py -q` 全过；
  `CUDA_VISIBLE_DEVICES=0,1 python -m pytest -s -v
  tests/e2e/online_serving/test_qwen3_omni.py -m 'core_model' --run-level 'core_model'`
  → 4 passed；stage-0 日志出现 `... exceeds the 512.0 MiB cap ... but is required for
  downstream-stage correctness; allocating it on CPU anyway`，且 `_send_single_request`
  对**所有** mix 请求都 ~21 MB（不只是第一个）。
- 禁止：全局抬高/删除逐 key cap（那是别的模型的 OOM 防线）；在测试或 deploy 配置里
  关 prefix caching 掩盖正确性 bug；在 talker 侧容忍短 embed（payload 早已错了）。
- Watch out：下游 stage 消费**全 prompt 逐 token** mm 输出的模型，在 stage-0 可能开
  prefix caching 时都必须声明 required keys —— Qwen2.5-omni 今天不用 layer-capture
  机制，机制变了要复查；绕过 cap 的缓存住在主机内存（本例每 key ~2.6 GiB）——TB 级内存的 CI 主机
  可承受，小内存主机要标注。^[SK-fix-talker-truncated-prefill-prefix-cache-key-cap]

## SCHED-1b — prefix-cache passthrough 必须保留逐请求边界与载荷语义

- 触发：修改 `build_mm_cpu`、`to_payload_element`、prefix-cache merge/deferred payload，
  或 runner 对 combined multimodal output 的拆包。
- 强制：token-aligned tensor（首维等于本步 scheduled token 总数）按每个请求的
  `start:end` 切片；非 token-aligned tensor 给每个请求完整独立 clone；prefix-cache
  merge 中的 list（如 `codes.ref`）保持整表并逐请求 clone，直到 runner 用**原 batch
  index** `_unwrap_lists`。D2H 后每个嵌套 tensor leaf 必须在 CPU、detached、contiguous，
  并保持 dtype/shape/value；dict/list 与非 tensor leaf 的结构和值不变。
- 生命周期：abort/discard 必须删除 staged chunks，之后的 commit 是 no-op；同 req-id
  重用从空状态开始，不能混入旧 chunk。
- 禁止：把所有 passthrough tensor 一律按 token 切；在 merge 阶段提前按 downstream
  subset index 拆 list；用 float staging buffer 改写 int metadata；把本规则解释为后续 W2
  优化已经实现——`f24a6165` 只增加当前行为的 characterization tests。
- 验收：mixed hit/miss + 不等 scheduled length 同时覆盖 1-D/2-D token-aligned、多个
  非对齐 shape、per-request tensor list 和 int32/int8 metadata；修改一请求结果不影响
  其他请求或源值。另测 discard→late commit、discard→req-id reuse。^[PR #5310]

## SCHED-2a — 极小 max_num_batched_tokens 与并发 prefill 会挂死 GPU

skill 元数据：`gpu-hang-low-max-num-batched-tokens`，
modules=[online_serving, worker_runner]，status=active，run_count=38，2026-06-18 创建 / 07-11 最后使用。

- 触发：启动约 2s 后 GPU hang，NVIDIA watchdog 杀进程，日志只有
  `Received cancellation signal, interrupting`（无 Python 栈）；条件为并发请求 ≥5 且
  `max_num_batched_tokens < 256`（如 64）。
- 机制：upstream 调度器新增 `throttle_prefills` 后与极小 token 预算交互不良。
- 修法：小批路径测试用适中小值——`tests/e2e/online_serving/test_qwen3_omni_expansion.py`
  的 `get_batch_token_config`，两个 stage 一起改：
  `updates={"stages": {0: {"max_num_batched_tokens": 512}, 1: {"max_num_batched_tokens": 512}}}`，
  并注释"64 过小会挂 GPU；调度器再变需重估"。
- 验证边界：完整验证需  2×H100；skill 落盘时无卡，实际验证为 import 检查（已确认无语法错误）——这是
  **纯测试配置改动**，不碰产品代码。
- 禁止：把挂死当模型/驱动 bug 直接重试；用超大预算掩盖小批路径不测。
  ^[SK-gpu-hang-low-max-num-batched-tokens]

## SCHED-3a — rebase 前必须对照 live upstream 调度接口

- 触发：对齐 upstream vLLM 版本（rebase/分支合并）涉及 `vllm/v1/core/sched/`。
- 强制：对照 live upstream 源码核对本组件继承的接口（`schedule`/
  `update_from_output`/异步调度、kv-connector 统计等挂点）是否有签名、时序或语义
  变化，逐条登记后再改代码；曾发生 `kv_connector_stats` 提取时序在上游更新后错位、
  需要移到 `_update_from_kv_xfer_finished` 之后的案例（omni_ar_scheduler.py 与
  omni_generation_scheduler.py 都要改）。base `NewRequestData` 重包必须按 live dataclass
  字段无损复制后再附 Omni payload；共享 mixin 只集中机械 lifecycle，AR/generation 的
  admission、cached-payload 和 synthetic-abort 等差异用调用参数保留为显式本地策略。
- 禁止：只跑单测绿灯就认定调度语义未变；不能用缺少真实 mixin helper 的
  `SimpleNamespace`/`object.__new__` stub 绕过共享调用链，也不能手列一份会随 upstream
  schema 漂移的 base output/request 字段。
- 验收：枚举 live `NewRequestData.__dataclass_fields__` 逐字段断言 identity/equality，覆盖
  Omni fast path 不重建与 generation fallback；AR/generation 分别验证 pending-input、queue
  restore、finished-set/abort policy。直接调用 scheduler 方法的轻量 stub 必须继承 mixin 或
  显式绑定本次方法真实依赖，使断言确实到达目标分支。 ^[PR #5461]
- vLLM 0.28 的具体补充：当 `defer_block_free` 启用时，AR 与 generation scheduler 都必须在每个
  非空 schedule step 推进 `sched_step_seq`，并在 `update_from_output()` 的开头推进
  `processed_step_seq`、调用 `_drain_deferred_frees()`；generation fast path 的推进必须早于
  `_update_after_schedule` 的 `last_sched_seq` 标记。EC connector 完成请求时先调用
  `request_finished(request)`，将 delayed-free 合入 block 生命周期，并把
  `ec_transfer_params` 经 `OmniEngineCoreOutput` 传给 output processor。prefill stats 必须在
  request 尚在 KV cache manager 中时以 `estimate_cached_tokens()` finalize，随后在所有
  `RequestOutput` 路径保留 `num_cache_creation_tokens`。这些是 upstream 0.28 合同，不是
  optional compatibility shim；同时覆盖 AR/generation 两条 override 路径和 hook-order 测试。
  ^[PR #6606]

## SCHED-4a — side-stream 复制必须拥有源 buffer 的完成期

- 触发：prefix-cache 异步写、side-stream D2H、persistent GPU buffer 或下一 step 会重写
  的 `slot_mapping`/hidden/mm tensor。
- 强制：源 tensor 在 copy event 完成前保持有效且不可被重写；使用显式 stream ordering、
  retain/`record_stream` 或消费屏障。drain/early-return 也必须完成或转移生命周期责任。
  单 pending-write 流水线中 event 未 ready 时 drain 必须不触碰 cache；ready 后恰好消费
  一次；调度第 N+1 次 write 前必须先消费第 N 次，不能覆盖 pending state。
- 禁止：仅同步目标 CPU tensor，却允许下一 step 复用源 GPU buffer；这会得到合法 shape
  但错误行内容。
- 验收：连续两 step 写入不同 sentinel，在人为延迟 side stream 下证明第一轮 CPU 结果
  不被第二轮覆盖；强制 event query false→true，断言 0→1→0 次 drain，并验证未显式
  drain 时下一次 schedule 仍先落盘上一轮。 ^[PR #4106] ^[PR #5310]

## SCHED-4b — pinned CPU 分配必须先判断 CUDA 能力

- 触发：CPU-only 测试、CUDA 不可用环境或异步 copy 可降级路径。
- 强制：根据 CUDA availability 选择 pinned/non-pinned allocation，再决定是否启用异步。
- 禁止：先无条件构造 pinned tensor，失败后才关闭 async path。
- 验收：CPU-only 环境能构造并走同步 fallback；CUDA 环境仍使用 pinned + async，两个
  分支产生相同内容。 ^[PR #4106]

## SCHED-5a — sampled-token logprobs 先校验再修改 request

- 触发：AR scheduler 消费 model-runner sampled tokens 和 `num_logprobs`，尤其是
  spec-decode 或 batched output slicing。
- 强制：按 request 切出 logprob rows，校验二维 shape、行数、第一 token 对齐和有限值；
  失败只将当前 request 置为 `FINISHED_ERROR`，不能先 append token 或污染同批 request。
  stop/EOS 截断后再按最终 emitted token 数重新 slice。
- 禁止：`logprobs` 缺失时静默跳过；用 truthiness 判断合法空输出；把 runner 的错位
  row 当作用户请求成功。
- 验收：覆盖 missing、wrong shape/count、token misalignment、NaN、spec-decode trim
  和同批健康 request 继续完成。

## SCHED-5b — stateful async-chunk request 必须占用调度容量

- 触发：模型在等待下一 chunk 时保留 runner state、native generation 重新接纳已完成的
  chunk，或 full-payload connector 负责把下一段重新送入 generation stage。
- 强制：`retains_state_across_chunks` 为真时，把 connector 中等待 chunk 的 request
  计入 `max_num_seqs`；full-payload consumer 在 chunk 到达时重新进入 waiting queue，
  不要被 base scheduler 提前停放。native stateless request 仅在当前 output 已结算后
  释放执行槽并排到 waiting 尾部，保留 request-owned model/KV state 与 `WAITING_FOR_CHUNK`；
  running 与 restored waiting 中仍有 in-flight tokens 的请求均不得重复执行，也不能阻塞其他就绪流。
- 禁止：stateful 请求只按 `running` list 计数导致超额 admission；把 connector-fed chunk 当成 API
  streaming update；仅在 abort 路径释放 receiver；把执行槽释放等同 model/KV 清理，或让
  同一请求的两个 native chunks 同时拥有执行槽。
- 验收：mixed batch 覆盖等待 chunk 的容量上限、full-payload requeue、normal finish
  的 receiver cleanup 和 abort/replica-loss cleanup；stateless/stateful controls 分别验证
  chunk requeue 后的容量、状态保留、无新输入不重复执行，以及 running/waiting 的 in-flight
  guard 和其他 ready request 继续接纳。 ^[PR #7781]

## SCHED-5c — stage-0 final request 的 KV transfer 例外必须显式标记

- 触发：stage-0 的 text/final 输出通常不需要下游 KV，但 CFG companion 或其他终端
  payload 仍需要复用该 cache。
- 强制：用 `omni_force_kv_transfer` 等 request metadata 明确覆盖“final stage=0”的
  shortcut；scheduler、engine 和 companion tracker 使用同一个标记语义。
- 禁止：按 final stage id 静默跳过所有 stage-0 KV；只在 companion 的 producer 设置
  标记而不测试 scheduler 的 transfer decision。
- 验收：普通 stage-0-final request 不传 KV，标记 request 传 KV，且 metadata 在
  `OmniARScheduler._request_omits_kv_transfer_to_next_stage` 中可读回。

## SCHED-5d — CFG companion 必须成对推进，缺失时有限降级并终止等待

- 触发：Audex/扩散 CFG companion、双 waiting queue、不同 chunk 进度或 companion
  abort/split。
- 强制：不完整 pair 暂存并按 parent/companion 对齐进度；完成时一起收敛，缺失或拆分
  的 companion 只能走显式、有界的 fallback，并清理两侧状态。
- 禁止：只推进 parent 让 companion 永久停在 waiting；把缺 companion 当普通 batch
  请求静默放行；用无限等待掩盖 replica loss 或 abort。
- 验收：覆盖完整 pair、不同进度、missing/split、parent abort 和 companion abort，
  断言请求不会挂死、错误归属保持 request-local、队列和 connector state 都释放。

## SCHED-5e — pooling output decoder 必须覆盖两个 scheduler 且失败即为 request error

- 触发：pooling stage 配置 per-stage output decoder，或修改 `OmniARScheduler`、`OmniGenerationScheduler` 的 pooling output 完成路径。
- 强制：两个 scheduler 都经共享 mixin 在 terminal status 处理前调用 decoder；成功时把解码 payload 写入 `EngineCoreOutput`，失败时只将当前 request 置为 `FINISHED_ERROR`、保留错误原因并停止继续调度。
- 禁止：只在 AR scheduler 接 hook；decoder 异常 fail-open 为空成功；先完成请求再解码；把原始 pooler tensor 或错误 request 的 payload 发送给下游。
- 验收：分别覆盖 AR/generation scheduler 的成功、decoder 抛异常、无 decoder 和同批健康 request；断言成功 payload、`FinishReason.ERROR`、request-local 错误归属及无跨请求污染。
^[PR #4795]

## SCHED-5h — KV extraction wait 只能观测完整区间

- 触发：AR scheduler 进入/离开 `waiting_for_transfer_free`，或传递 KV transfer
  acknowledgement、connector type、abort/error cleanup metric。
- 强制：在 `_free_request()` 进入 KV-transfer-free wait 时以 monotonic clock 记录 start；
  仅在 extraction acknowledgement 到达时 emit completed duration，并携带实际 connector
  type。任何 finish、abort、error 或 request removal 都清除未完成 start，不能输出 partial
  wait。
- 禁止：以 request wall-clock、enqueue time 或 cleanup time 补造 KV wait；把没有 start
  的 acknowledgement 当零秒样本；因 telemetry 给 terminal request 保留 scheduler state。
- 验收：覆盖 complete acknowledgement、无 start、abort/error/normal finish 清理和 iterator
  request-id 输入；断言只有完整 wait 有一个样本，connector label 有界且没有 stale start。
  ^[PR #6150]

## SCHED-5f — async-chunk 等待必须有可配置的截止时间

- 触发：async-chunk 请求进入 `WAITING_FOR_CHUNK`，或配置/测试设置 `VLLM_OMNI_INPUT_WAIT_TIMEOUT_S`。
- 强制：统一校验超时值，拒绝负数和非有限值，`0` 仅作为显式禁用并记录 warning；chunk adapter 在请求停等时开始计时、每次收到 chunk 后重置，并由 scheduler 及时结束超时且仍在 `self.requests` 中的请求。
- 强制：stall diagnostic 在 scheduler import 前设置 `0 < server wait < client wait`。
- 禁止：只保护 full-payload 的 `WAITING_FOR_INPUT`；让负数、`nan` 或 `inf` 静默关闭 deadline；按 stream 总生命周期计时；对已离开 scheduler 的请求调用 `finish_requests`；把 server error 当 client delivery/生产预算。
- 验收：覆盖解析、stall/reset、完成/abort 与健康同批请求；超时为 `FINISHED_ERROR`。Nemotron 固定
  `240 < 300`，server request-ID/error 先于 client timeout，健康 control 两者皆不触达。^[PR #6033] ^[PR #6831]


## SCHED-5i — consumed connector contract failure 必须在 scheduler thread 终止 live request

- 触发：receiver 已消费 async-chunk，但 prompt/window validation 失败，后台线程不能安全修改 request
  status 或释放 scheduler ownership。
- 强制：receiver 只按 internal request ID 记录首次失败原因；scheduler 每轮原子 drain ledger，仅对仍在
  `self.requests` 的 ID 调 `finish_requests(..., FINISHED_ERROR)`。已离开或被同 ID 新 generation 替换的
  request 不得由 stale failure 终止；错误日志保留 request 与原因。
- 禁止：在 recv thread 直接结束 request；重试已消费 chunk；将 stale ID 当作 live request；因单个
  malformed duplex condition 阻断同批健康请求。
- 验收：覆盖 live、已完成/abort、同 ID replacement、重复 failure 与同批健康 request，断言只终止
  当前 owner 且 ledger 只消费一次。^[PR #6626]

## SCHED-6a — async discard 的计数单位必须与 stale drain 一致

- 触发：上游 scheduler 改动异步占位、stale output 或 streaming segment replacement。
- 强制：每个 discard site 用 `num_in_flight_tokens`（scheduled-token 单位）建立
  `num_stale_output_tokens`；同一 in-flight decode 若已由 queued streaming update fence，stop/replacement
  site 必须赋值而非累加，避免残留吞掉下一 duplex unit。每个迟到 frame 先按本次
  `num_tokens_scheduled` 结算 in-flight 与 stale 两个同单位 counter，再在 append/emit 前丢弃。
  placeholder rollback 与 stale drain 分开记账。
- 禁止：用 `num_output_placeholders` 初始化 stale counter；对已由同次 streaming update fenced 的
  in-flight token 再累加；清零 placeholder 后仍让迟到输出进入 placeholder/computed-token 的正常
  结果更新；把 scheduler 修复宣称为音频 WER 修复。
- 验收：AR 与 generation 路径覆盖多 token frame、prefill 无 placeholder、连续旧/新 segment 和
  queued update 后的 async stop，断言 stale counter 恰好归零且首个新 segment 输出不被吞；真实质量
  指标必须另行验证。^[PR #5976] ^[PR #6619]

## 相关

## SCHED-6b — full-payload admission 是解析后的 downstream capability

- 触发：新增或修改只在 request end 消费完整上游 sequence 的 stage，或修改 full-payload waiting coordinator。
- 强制：以 pipeline 解析的 `requires_full_payload_input` 驱动 stage_id > 0 的 non-async coordinator；async-chunk stage 走 streamed connector，不创建 full-payload wait。完成前零传输，完成时只 enqueue 一次完整 sequence。
- 禁止：按模型名、architecture/stage key 或旧 allowlist 猜测能力；扩大到整个模型家族；把 stage-0 async sender 因没有 full-payload input 而不初始化 chunk adapter；逐 token 重复搬运完整 payload。
- 验收：覆盖 stage 0、async-chunk consumer、未声明 consumer 和声明的 sync consumer 四个 gates；另覆盖 stage-0 async sender 与 stage-1 receiver 都保有 adapter、完成时一次传输和 abort 无残留。^[PR #5957] ^[PR #6149]

- 机制与边界见 [architecture](architecture.md)；跨 stage 数据面见
  [Distributed 组件](../distributed/_index.md)。

## SCHED-6c — payload transport 的发送与接收能力必须分开验证

- 触发：新增 async-chunk processor、full-payload consumer，或变更 runner connector 初始化。
- 强制：`requires_full_payload_input` 只声明同步 downstream receive/wait；`async_chunk` 独立保留 producer 与 receiver 的 chunk adapter。connector 需求还可来自 downstream processor hook 或 explicit connector role，且每个选择的 GPU/NPU/XPU worker 的 `model_runner_cls` 必须实现 `OmniConnectorModelRunnerMixin`，在 worker 启动前 fail fast。
- 禁止：从 consumer capability 倒推出 sender；把 Nemotron token-only thinker→talker 设为 full-payload；让没有 connector mixin 的平台 worker 在运行时才挂起；让非 async deploy 逐 token 搬完整 code stack，或让 async terminal chunk 缺少 finished/empty-terminal 语义。
- 验收：对一个支持和一个不支持 connector 的显式 worker、以及 platform-resolved worker 断言启动前成功/`ValueError`；验证 GPU、NPU 和 XPU worker 公开正确 runner class，并覆盖 sync request-end payload、streaming 累计/terminal chunk、requeue 和 abort cleanup。^[PR #5842] ^[PR #6149]

## SCHED-6d — 显式 prompt replacement 必须一次性释放旧状态并回到 admission

- 触发：running 或 computed 的 async prompt 显式携带 `replace_streaming_prompt`，或 ready 的 replacement 进入 scheduler。
- 强制：替换先释放旧 KV/encoder state 一次，清除 in-flight prefill 所有权和 connector watermark；stale fence 先设 `drop_stale_output=True`，以当前 `num_in_flight_tokens` 赋值，再将请求回到 `WAITING` 并走正常 admission。
- 禁止：只更新 prompt 而继承旧 cache/watermark；把同一 in-flight frame 在更新和 replacement 路径重复计入 stale；绕过 scheduler admission 直接恢复运行。
- 验收：覆盖 running replacement、ready async replacement 的重复 reset 和正常重新调度；断言 KV/encoder 释放各一次、watermark 清零、stale counter 恰好可排空且首个新 segment frame 不被丢弃。 ^[PR #6406] ^[PR #8259]

## SCHED-6e — 流式 segment 边界必须在 request mutation 前冻结发送 watermark

- 触发：`OmniARScheduler.update_from_output` 停止 resumable async-chunk request，且同一处理周期可能由 streaming update 替换或重置该 mutable `Request`。
- 强制：在 `_handle_stopped_request` 及任何会重置 computed-token 状态的 transition 前，从 chunk adapter 取得 confirmed computed-token watermark；将该冻结值显式传给 `save_async`。后续 segment 只能建立自己的 watermark，不能让旧边界任务读取已重置 request。
- 禁止：在 request replacement 后重新计算旧 segment 的 send watermark；以当前 `num_computed_tokens` 推断已确认旧边界；把模型专用 Code2Wav batching/wait policy 塞进通用 generation scheduler。
- 验收：模拟 stop handler 将同一 request 重置为新 segment，断言 queued old boundary 使用 transition 前的 confirmed token 数；并验证 generation scheduler 没有新增 MiniCPM/Code2Wav-specific coalescing state。^[PR #6021]

## SCHED-6f — stale output 必须同时耗尽 scheduled 与 async 两类计数

- 触发：streaming segment replacement 后收到 late model frame，或修改 abort queue cleanup、
  streaming-input admission counter 与 segment-boundary handoff。
- 强制：同一迟到 frame 必须联合结算 scheduled-token stale window 与
  `async_tokens_to_discard`；前者按 scheduled-token accounting，后者按该 frame 的 generated-token
  数递减，任一 fence 命中就不得 append/emit。处理 pending input 前后都从
  `waiting`、`kv_holding_waiting` 与 `running` 清除 `FINISHED_ABORTED`，随后按 live queue ownership
  清理相应 `deferred_waiting` 集合成员，重算 `num_waiting_for_streaming_input`。
- 禁止：两个 stale domain 只消费一个；只清普通 waiting/running 而遗留 KV-holding queue 或 deferred set；
  用增减猜 streaming counter；把 unacknowledged response snapshot 当作有界 lifetime，或把
  sliding-recompute/后续 playback ACK 修复归因于本提交。
- 验收：覆盖一个 frame 同时命中两种 fence、仅命中任一 fence、三类 queue 的 abort sweep、
  pending-input 前后 abort，以及 live-owner counter drift 修复；断言旧 frame 不进入 output，
  两个 counter 恰好归零且新 segment 首帧保留。^[PR #6529]
