---
title: "Distributed 传输规则"
created: 2026-08-05
updated: 2026-10-26
type: rule
tags: [vllm-omni, components, distributed]
sources: ["PR #5744", "PR #5976", "PR #6001", "PR #6089", "PR #6834", vllm_omni/diffusion/distributed/parallel_state.py, tests/diffusion/distributed/test_expert_parallel_layout.py, vllm_omni/distributed/omni_connectors/adapter.py, vllm_omni/distributed/omni_connectors/kv_transfer_manager.py, vllm_omni/distributed/omni_connectors/transfer_adapter/chunk_transfer_adapter.py, vllm_omni/distributed/omni_connectors/transfer_adapter/base.py, vllm_omni/worker/omni_connector_model_runner_mixin.py, tests/distributed/omni_connectors/test_kv_recv_tp_consensus.py, tests/distributed/omni_connectors/test_chunk_transfer_adapter.py, tests/worker/test_omni_connector_mixin.py, "PR #5146", "PR #6021", "PR #6033", "PR #6360", "PR #6406", "PR #6626", "PR #6529", "PR #7136", "PR #6889", "PR #6093", "PR #7870", "PR #7166", "PR #8082", "PR #7733", "PR #9406"]
confidence: high
---

# Distributed 传输规则

只有 `DIST-数字字母` 是本页可审计规则 ID。connector backend 的选择和端口分配见
[Distributed architecture](architecture.md) 与 [connector pitfalls](connector-pitfalls.md)。

## DIST-1a — TP KV receive 必须做全 rank 一致的成功/放弃决定

- 触发：纯 TP stage 接收 KV、CFG companion 参与 KV transfer，或 connector receive
  只在部分 rank 得到 state。
- 强制：只在 TP process group 内交换 receive state；任一 rank 发现 metadata、shape 或
  payload 不一致时，所有 rank 都走同一个 no-KV fallback，不能让部分 rank 继续 collective。
- 禁止：用本地 `if` 丢掉 KV 后仍让其他 rank进入 receive；使用 global world group 代替
  stage 的 TP group；把 companion 当成普通 stage-0-final request 静默跳过。
- 验收：模拟单 rank divergence，断言所有 TP rank 都放弃本次 KV 并保持后续 collective
  可继续；覆盖普通和 CFG companion 角色。

## DIST-1b — chunk transfer 要区分 upstream exhaustion 与本 stage 完成

- 触发：async-chunk/full-payload connector 传递空 segment、最后一个 chunk 或
  `WAITING_FOR_CHUNK`/active-window 状态。
- 强制：保留空 segment 的边界语义，区分 upstream 已耗尽和当前 stage 已生成完成；收到
  后续 chunk 时刷新 prefill/request state，并让 active-window admission 继续推进。
- 禁止：把空 segment 当作完成信号；用本 stage 的 generation completion 终止上游；在
  connector 没有新数据时无限占用 active window 或静默丢掉 terminal update。
- 验收：覆盖非空→空→非空、upstream exhaustion、stage completion、重复 terminal
  update 和窗口恢复；首批测试看 `test_chunk_transfer_adapter.py`。

## DIST-1b1 — per-segment adaptive controller 不得越过 connector lifecycle

- 触发：Qwen3-TTS adaptive chunk、resumable segment finish，或 transfer adapter/local runner cleanup 修改。
- 强制：controller 可按 request 持有，但 `is_segment_finished` 与 request cleanup 均必须清除；下一段从 chunk 0 重新建立 EWMA、emitted ledger、playback cursor 与 telemetry。request-global connector continuity key 不能延长 controller 生命周期。
- 验收：normal finish、segment finish、abort cleanup 与 empty terminal payload 都断言 state 不残留；覆盖 `OmniChunkTransferAdapter` 和 `OmniConnectorModelRunnerMixin`。^[PR #6001]

## DIST-1c — payload connector 按 edge 所有权创建且不与 KV manager 捆绑

- 触发：修改 AR/DiT 解耦、stage connector role、`custom_process_next_stage_input_func`、
  runner connector 初始化或 KV-only edge。
- 强制：receiver/非 sender 即使无 downstream hook 也创建 payload connector；sender 只在
  `custom_process_next_stage_input_func` 是非空字符串时创建，空值或非字符串都表示
  KV manager 独自拥有该 edge。KV manager 与 payload connector 必须独立初始化和关闭。
- 禁止：不得只因已配 connector/role 就创建 payload transport；不得从“无 outgoing
  hook”推导 receiver 也不需要 connector；不得用 consumer-side hook 反推 sender 所有权。
- 验收：参数化锁定 sender+空/非字符串→不创建、sender+非空字符串→创建、
  receiver/非 sender+无 hook→创建，并覆盖安全 shutdown。Bagel/Hunyuan KV-only 和
  Qwen3-Omni/MiniCPM-o payload 路径各做真实拓扑 smoke。该矩阵不证明 issue #5595 中独立的
  shutdown/orphan 问题已解决。 ^[PR #5744]

## DIST-1d — async-chunk 发送失败必须保留内部请求身份并可观测

- 触发：async-chunk sender 的队列任务被 `popleft()` 后发送抛异常，或 connector 返回失败但不抛异常。
- 强制：从 task 内的 `request.request_id` 取得 scheduler 使用的内部请求 ID，在发送线程以锁保护的 ledger 中按请求记录首次失败原因；scheduler sweep 时原子 drain 并分别记录仍存活与已离开 scheduler 的请求。
- 禁止：读取不存在的 `task["request_id"]`；用 `external_req_id` 代替 `self.requests` 的内部 key；发送失败后静默丢弃，或把已完成/已 abort 请求误当作仍可调度请求。
- 验收：覆盖发送抛异常、`connector.put` 返回 `False`、内部/外部 ID 不同、同一请求多次失败、并发 record/drain，以及请求在 sweep 前后离开 scheduler；日志必须包含正确 request ID 和失败原因，且 ledger 只被消费一次。^[PR #6033]

## DIST-1e — chunk connector 仅按隐藏队列所有权恢复状态并幂等回收 receiver

- 触发：`OmniChunkTransferAdapter.finish_requests` 处理 resumable 请求的 `FINISHED_STOPPED` segment，且请求可能停在 `waiting_for_chunk_waiting_requests`、`waiting_for_chunk_running_requests` 或 `_held_non_active` 中。
- 强制：只在 connector 隐藏队列实际持有请求时恢复 `requests_origin_status`；对每个目标统一调用幂等的 `cleanup_receiver`，清理 active stream、ready/exhausted/finished/cancelled/waiting 状态，并移除 chunk 等待队列中的已完成请求；请求已回到 scheduler 可见队列后不得再使用过期 origin status 覆盖当前状态。
- 禁止：仅凭 `requests_origin_status` 存在就恢复状态；把 resumable `FINISHED_STOPPED` 当作普通 finished 请求而跳过 connector 回收；分散复制部分 receiver 清理逻辑导致重复释放或残留 active-stream 占用；用无关请求的队列状态扩大清理范围。
- 验收：覆盖 connector 隐藏持有、scheduler `running`/`waiting`/`skipped_waiting` 持有、无 connector 所有权及 stale origin；断言隐藏队列、origin ledger、active streams 和 receiver 状态在重复 finish 后仍为空且 active-window 可被新请求重新占用。^[PR #6360]

## DIST-1f — Code2Wav 快照替换必须显式标记并保持终端就绪

- 触发：async-chunk connector 收到 generation runtime payload、终端 chunk 或重复 terminal boundary。
- 强制：只有 producer 显式设置 `replace_runtime_additional_information=True` 才全量替换；Code2Wav 的空 replacement 必须同时带 `is_segment_finished=True` 以标记就绪；替换时删除旧 `codes.audio`，但保留当前 snapshot 的 `codes.ref` 等 sibling 字段。
- 禁止：把未标记的 generation/diffusion payload 改成替换语义；把 control-only boundary 写成未就绪的空更新；跨 segment merge/replay 旧 terminal audio。
- 验收：覆盖标记/未标记、audio+ref、空 replacement 和重复 terminal；断言新 prompt、finished/ready 状态正确，旧 audio 不会进入下一次 Code2Wav。 ^[PR #6406]

## DIST-1g — 异步 processor 任务必须在入队时冻结边界状态

- 触发：`OmniChunkTransferAdapter.save_async` 向 custom next-stage processor 入队，且 producer request 会在 worker 发送前继续被 streaming update 原地修改。
- 强制：只为 custom processor shallow-copy request，并递归复制其可变容器输入（additional information、prompt/token lists）；tensor leaves 保持共享。每个 queued segment boundary 在入队时移除 `requests_num_chunks_sent` chunk-progress watermark，使下一个 segment 能从 chunk 0 建立自己的 watermark；这不重置 `_segment_generation` gate，后台发送旧 task 也不得清除新 segment 的 watermark。^[PR #6834]
- 禁止：给无 hook 的直通路径增加 request snapshot/copy；让 queued task 读取后来 mutation 的 token、metadata 或 output history；在后台 terminal cleanup 无条件删除可能已属于下一 segment 的 watermark。
- 验收：old/new segment 在 background flush 前交错时，新 watermark 保持；连续入队后各 processor 观察各自 admission-time request snapshot；无 custom processor 时 task 仍引用原 request。^[PR #6021]

## DIST-1h — abort 不能让旧 sender generation 复活

- 触发：async-chunk sender 在 background save/connector put 期间 abort，且 external request ID 可复用。
- 强制：queued task 与其 sender generation token 绑定；abort 取消 token，旧 task 在 put 前后均须拒绝更新 state，in-flight task 在 finally 中回收。sender-state lock 只保护 token/state transition，不能包住 payload construction 或 connector I/O。
- 禁止：`cleanup_sender` 清空 map 后让 pending save 用默认 chunk counter 写回旧 chunk 0；让 scheduler thread 等待 connector I/O；把同一 external ID 的新 generation 误当作旧 task 的 cleanup target。
- 验收：覆盖 pending/in-flight abort、stale put completion、ID reuse 和普通 async-chunk transport。^[PR #6089]

## DIST-1i — receiver registration 必须隔离旧 poll 与新 request generation

- 触发：async-chunk receiver 注册/清理与 `connector.get()` 并发、同一 internal request ID 恢复或复用，
  或已消费 payload 需在 scheduler thread 才能安全修改 prompt。
- 强制：每次 receiver registration 持有独立 identity entry；`connector.get()` 可在锁外阻塞，但结果
  commit 必须在 receiver-state lock 下确认当前 map 仍指向同一 entry。cleanup 是 commit barrier：旧
  queued/in-flight poll 的迟到结果只能丢弃，不能重建已删除或新 generation 的 prompt/window state。
  AR payload 先冻结为 pending update，再由 scheduler thread 用当前 live request 应用；contract error
  进入一次性 receive-failure ledger，不得重取已消费 chunk。
- 禁止：只按可复用 request-id 判断 poll 仍有效；在 I/O 期间持有 scheduler-facing lock；让旧
  terminal cleanup 清掉新 registration；使用被移出 scheduler 的 fallback request 应用 pending mutation。
- 验收：覆盖 cleanup-during-get、same-ID re-registration、late success/error、重复 register/cleanup、
  scheduler live-ID replacement 与 invalid payload ledger drain；证明旧 entry 不影响新 request。^[PR #6626]

## DIST-1j — sender dedup watermark 必须按 generation/segment 限定

- 触发：async-chunk sender dedup、segment terminal task、background cleanup 或同 request ID 的
  streaming segment replacement。
- 强制：`save_async` 将 request 的 segment generation 冻结进 queued task；小于当前 generation 的
  late save 必须拒绝。terminal 必须清除 chunk-progress counter；仅当 request
  `._omni_segment_generation is not None` 才把 expected generation 推进 `+1`，否则保持 current
  generation，使 counter-less follow-up 仍以 current/0 enqueue。chunk watermark 只负责同段 progress
  dedup，generation gate 负责跨段 stale fencing；request 最终 cleanup 同时回收 generation 和 chunk state。
- 禁止：只按裸 request ID 判断 late task 有效；让旧 terminal boundary 因旧 chunk count 被误拒；
  background completion 无条件删除下一段 watermark；把未 ACK 的 serving response snapshot描述成
  此 sender generation 已给出有界 retention。
- 验收：覆盖 counter-bearing terminal `+1` fencing 与 counter-less terminal hold/current-0 follow-up、
  counter clear、late old save 和普通 dedup/preemption。该变更不修复已有 stuck ID 或 abort 行为。^[PR #6529] ^[PR #6834]

## DIST-2a — diffusion EP group 必须满足运行中 MoE backend 的 communicator 合同

- 触发：上游 MoE factory/oracle 或 `init_model_parallel_group` 增加 all-to-all manager 要求。
- 强制：EP group 在运行版本支持 `use_all2all` 时显式启用；兼容旧版本用签名能力检测，不能按版本
  字符串猜测。非 EP group 保持原参数，避免扩大 communicator 行为。
- 禁止：创建普通 device communicator 后等 weight load 才触发 manager assert；向不支持该 kwarg
  的旧 vLLM 无条件传参；把 TP/EP 配置存在当作 manager 已初始化。
- 验收：mock 新旧签名分别断言 EP 转发/旧版省略，普通 group 不启用；目标环境用真实 MoE DiT
  TP+EP smoke 验证 manager 非空。^[PR #5976]

## DIST-3a — Omni 输出序列化必须按扁平 dataclass 合同兼容旧线格式

- 触发：修改 `OmniMsgpackEncoder`/`OmniMsgpackDecoder`、`OmniRequestOutput` 字段或跨 stage 输出 wire format。
- 强制：当前 `OmniRequestOutput` 按其 dataclass 字段重建，不能落入基类 `RequestOutput` 的动态属性编码路径；decoder 只构造已知字段，并在旧 wire format 含嵌套 `request_output` 时完成一次向扁平对象的内容合并。基类 `RequestOutput` 的字段集合必须跟随 pinned upstream 合同。
- 禁止：让 subclass 走基类序列化而丢失 diffusion/stage 字段或形成递归；不得把未知 key 直接传给 `RequestOutput`/dataclass 构造器；upstream 已移除的 `multi_modal_placeholders` 不得仅为旧动态属性重新作为当前字段恢复。
- 验收：msgpack round-trip 分别覆盖 pipeline 文本输出和 diffusion 图像输出，保持 request metadata、prompt、token、outputs、finished、stage 字段及图像内容；旧嵌套 wire payload 能解码为扁平对象，编码不递归且解码后的字段可被直接消费。^[PR #5146]

## DIST-1k — Mooncake TCP write completion 必须表示目的端已应用

- 触发：升级 `mooncake-transfer-engine`、修改 TCP `batch_transfer_sync_write`/completion 语义，或让零拷贝 consumer 在 write completion 后立即读取 destination buffer。
- 强制：CUDA Mooncake 依赖必须钉在包含 acknowledged TCP writes 的版本（`0.3.12` 起）；`batch_transfer_sync_write` 返回成功后，destination 内存必须已更新到可读状态，不能只表示本地 send 入队。回归必须在 receiver 被挂起时证明 completion 不会提前返回，恢复后再断言目的端字节一致。
- 禁止：降级到无 ACK 的 TCP 实现却不更新测试合同；把 RDMA 路径的 completion 语义外推到 TCP；或在 completion 前启动依赖 destination 内容的零拷贝消费。
- 验收：强制 TCP 的挂起-恢复测试覆盖「停止期间不完成、恢复后成功且字节匹配」；依赖声明与该测试同步变更。^[PR #7136]

## DIST-1l — parked async-chunk sender 收到终态 update 必须先投递 terminal chunk

- 触发：修改 `OmniSchedulerMixin.add_request`、`OmniChunkTransferAdapter.save_async` / sender cleanup，或 realtime/async-chunk 在 `WAITING_FOR_STREAMING_REQ` 上的终态竞态。
- 强制：当终态 update（`resumable=False`）落在已 parked 且 `has_active_sender` 的 session 上时，不得只走 upstream 的本地 `FINISHED_ABORTED`。必须先 `force_request_finished` 入队携带 `finished` 的 request-terminal chunk（必要时推进 `_omni_segment_generation` 以通过 dedup），再本地 finish。`terminal_pending` 必须挡住丢弃该 chunk 的 cleanup，直到 connector 接受。
- 禁止：假设 orchestrator 的一次性 StreamingUpdate 对下游 receiver 足够；在 `finish_requests` 之后才尝试 enqueue terminal；让 cleanup 在 terminal 上线前回收 generation，导致下游卡在 `WAITING_FOR_CHUNK` 直至输入超时。
- 验收：覆盖 parked+active-sender 终态、非 sender/非 parked 回退、`force_request_finished` 顺序、terminal_pending 与 deferred cleanup；证明下游经 payload `finished` 结束，而不是仅靠 600s stall。^[PR #6889]

## DIST-1l2 — NIXL connector 超时后必须保留 native 资源直到终端态

- 触发：新增/修改 `NixlConnector`、NIXL READ、ZMQ claim/`XFER_DONE`、lease 或 receive timeout。
- 强制：NIXL 无远端 READ 取消时，超时/放弃后仍保留 buffer、registration、descriptor、transfer handle 与 remote agent，直到 native 终端态；`close()` 等待后再释放。源 registration 有界 lease（`lease_seconds` / `VLLM_OMNI_NIXL_LEASE_S`）并由 reaper 回收。跨 stage 请求须把 producer endpoint/`payload_sender_info` 经 scheduler handle 传到 consumer。
- 禁止：超时后立刻 deregister 或丢弃仍可能被 DMA 写入的内存；把 vLLM P/D block-table NIXL 接口当作 Omni 任意 payload connector；依赖未声明的固定 `zmq_port` 却不经 resolver。
- 验收：覆盖超时保留、lease 回收、metadata claim/ack，以及 `OmniChunkRecvHandle.payload_sender_info` 贯通；缺 NIXL 依赖时 lazy import 为 None。^[PR #6093]

## DIST-1l3 — NIXL ownership 与 metadata timeout 使用独立可控路径

- 触发：修改 NIXL terminal/deferred ownership 测试、metadata claim/query 或 transfer wait。
- 强制：ownership 用例使用 reliable_claim_queries 隔离短 ZMQ discovery，保留 metadata 编解码、claim 与 producer 状态机。metadata timeout 单测只对 metadata query socket 注入 zmq.Again，断言 get 返回 None、零 copy 且不登记未获得的 ownership；真实 handshake 仍单独跑 ZMQ roundtrip。
- 禁止：靠放宽 entered.wait 掩盖短 query 未进入 transfer；让 ownership fixture 悄悄屏蔽 metadata timeout，或删除真实 handshake。
- 验收：覆盖 direct 与 done/error/timeout/unknown ownership；timeout 与真实 handshake 各有独立断言，roundtrip 比较 tensor/size 和唯一 copy/ownership，清理 socket/thread。CPU core_model 门禁不能证明 NIXL DMA 硬件行为。 ^[PR #7870] ^[PR #7733]

## DIST-1l4 — 原生 Mooncake AR→DiT KV 必须关 async_chunk 并按物理页序释放

- 触发：部署或修改 `kv_transfer_config`/`MooncakeConnector`、AR→DiT 原生 KV handoff、`OmniARScheduler` 的 block free，或与 legacy `OmniKVTransferManager` 并存的路径。
- 强制：存在 stage `kv_transfer_config` 时必须 `async_chunk=False`。原生路径由 scheduler 拥有 diffusion KV 页；connector 失败 fail-close diffusion engine 后再拆页。非 caching P/D 池释放时按递增 `block_id` 归还，便于相邻页合并；deferred free 存逆序以匹配 vLLM drain。原生 transfer 边界用已确认 computed tokens，排除 async output placeholders；需要时短暂改写 request status/token 计数并在 `finally` 恢复。
- 禁止：`async_chunk=True` 搭配原生 KV transfer；把 eviction-priority free 顺序当成无缓存池的物理布局；用含 placeholder 的 token 计数作为 transfer 边界；connector 失败后仍继续拆页当成功。
- 验收：配置拒绝 async_chunk+kv_transfer；物理序 free；confirmed-token transfer 边界；失败 fail-close。^[PR #7166]

## DIST-1m — SHM producer 只能按完整 key 回收，并有界 reap 跨进程已消费段

- 触发：修改 `SharedMemoryConnector` 的 `_pending_keys` / `cleanup` / `close` / `reap_consumed`，chunk adapter abort 后的 sender 清理，或 lock-file 与 `/dev/shm` 回收。
- 强制：`cleanup` 按调用方传入的完整 key 精确 unlink，不得按 request-id 做 `_` 前缀/后缀模糊匹配；即使 key 不在 `_pending_keys` 中仍会尝试 unlink，不能把该记账集合当成所有权拒绝门禁。receiver 读到 SHM 字节后即可标 consumed 并删 lock file，即使随后 deserialize 失败。producer 在 `put` 与空闲 save loop 上做有界 round-robin `reap_consumed`（丢弃对端已 unlink 的 key）。abort 后在 in-flight 排空再按 adapter 生成的精确 chunk key 入队清理；成功终态 payload 必须留给下游消费，且清理发生在 external-id 复用之前。receiver 用当前 registration identity 拒 stale work，不得堆积 cancellation tombstone。
- 禁止：用 request-id 子串误删无关 SHM；把已跨进程消费的 key 永远留在 `_pending_keys`；deserialize 失败就留下 lock file；用 underscore-prefix 猜测所有权。
- 验收：覆盖相似 request id、abort-during-put、external-id 复用、无效 payload 后 lock 消失、consumed-key 回收，以及取消后 `/dev/shm` 与 pending keys 归零。^[PR #8082]

## VLLM-OMNI-PR9406-ASYNC-POLL-SLEEP — stage memory 生命周期测试的 _wait_until 轮询禁止 sleep(0) 忙等

- 触发：改动 `tests/engine/test_stage_memory_lifecycle.py` 中 `_wait_until` 轮询助手——`while not predicate()` 循环内的休眠间隔或外层 `asyncio.wait_for` 的超时取值。
- 强制：`_wait_until` 的轮询必须以真实让出事件循环的休眠代替零延迟忙等，随 PR #9406（修复 #9401）合入的取值为 `await asyncio.sleep(0.001)`。依据随修复落在树内的代码注释：谓词的满足依赖 `asyncio.to_thread` 中执行的 replica 注册，`asyncio.sleep(0)` 的循环连续可运行，会在 GIL 上饿死该工作线程，使替换快照在期限耗尽前无法发布（PR 记录的单 CPU 绑定 + 加大线程切换间隔的压复现：修复前 1 failed/141 passed，修复后 220 passed）。修该文件此类时序 flake 时保持 `timeout=2` 与全部生命周期断言不变（含防止已脱离 RPC 覆盖其替换者快照的断言），调整的是轮询让出方式而非期限或断言。
- 禁止：把 `_wait_until` 循环内的休眠改回 `asyncio.sleep(0)` 忙等；以拉长 `asyncio.wait_for` 超时或删除/放宽该文件生命周期断言的方式"修复"此类时序 flake。
- 验收：`_wait_until` 循环内为 `await asyncio.sleep(0.001)` 且 `timeout=2` 未变；`python -m pytest tests/engine/test_stage_memory_lifecycle.py tests/engine/test_membership_controller.py tests/engine/test_stage_pool_collective_rpc.py -q` 通过。^[PR #9406]

<!-- kb:rule status=active since=v0.30.0 -->
