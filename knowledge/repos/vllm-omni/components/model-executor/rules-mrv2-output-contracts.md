---
title: "MRv2 shared output 规则"
created: 2026-10-06
updated: 2026-10-09
type: rule
tags: [vllm-omni, components]
sources: ["PR #8184", "PR #7781", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/omni_model_runner.py#L54-L81", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/model_states/omni_model_state.py#L193-L210", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/model_states/omni_model_state.py#L898-L940", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/omni_data_plane.py#L249-L342", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/native_output_worker.py#L12-L99", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/delivery.py#L74-L218", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/omni_data_plane.py#L533-L604", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/omni_model_runner.py#L238-L242", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/worker_v2/omni_data_plane.py#L144-L206", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/worker_v2/test_omni_data_plane.py#L294-L355", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/worker_v2/test_omni_data_plane.py#L444-L481", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/worker_v2/test_omni_data_plane.py#L384-L442"]
---

# MRv2 shared output 规则

## EXEC-MRV2-1a — FULL graph auxiliary output 必须显式声明稳定 tensor tree

- 触发：修改 supports_mrv2_full_graph_aux_outputs、tuple output capture/replay、make_omni_output_mrv2 或模型 stage hooks。
- 强制：tuple unwrap 按模型 `_returns_tuple` 声明选择，与 stage 名称无关；FULL aux path 另外要求显式 `supports_mrv2_full_graph_aux_outputs` 且返回 tuple。每个非空 pytree leaf 都是 tensor，leading dimension 与 hidden token axis 一致，所有 capture 的 tree spec 稳定，否则明确拒绝。capture 临时 wrapper/aux flag 在 finally 恢复；replay unflatten 后用本步 live batch/request state 生成输出，保留 real-token 与 padded-token 的切片边界。未声明 tuple/捕获 side-state 继续排除 FULL；外层 stage wrapper 必须暴露子模块有效 hooks，遗漏有 warning。
- 禁止：根据 `stage_name` 猜 tuple 合同；把任意 Python/request metadata 捕入 graph；重放过期 prefill payload；容忍变化的 aux tree 或把 padding 发布给真实请求；模型没有 opt-in 就默认改用新 hook。
- 验收：稳定嵌套树、非tensor/空/错 token-axis、跨 capture spec 变化、eager/FULL/padded batch 与 live metadata 改变都覆盖，异常后原 forward/flag 恢复；另测惯用 stage 名但未声明 tuple 的模型，以及使用其他 stage 名且已声明 tuple 的模型。 ^[PR #8184] ^[PR #7781]

## EXEC-MRV2-1b — Narrow sampler 与 sampled embedding 发布必须按实际模型和有效行投影

- 触发：修改 custom sampler、logits_vocab_size、identity decode preprocessing 或 embed.sampled handoff。
- 强制：模型已声明的 narrow head 在 sampler 构造前同步 runner/request-state/warmup vocab；custom sampler 或 static staged-write shortcut 仅按显式 hook/capability。identity decode 跳过仅限非 eager-MTP 的 decode 行。sampled embedding 仅给 async non-text multimodal handoff；一步必须每请求一枚 sample，未完成 prefill 的行留空，最后 prefill/有效 decode 行才取 model embedding；copy stream 等 producer event 后嵌套合并，不能覆盖其他 payload。speculative 多 sample 不走此发布。
- 禁止：用 HF 大词表覆盖 codec head；给 partial prefill 发布丢弃 sample；无条件复用 upstream/custom sampler staged-write 生命周期；把 embed.sampled 优化说成所有模型性能保证。
- 验收：narrow/no-declared vocab、custom/default sampler、partial/final prefill、mixed rows、multi-sample 与异步 producer stream分别回归，token/embedding 请求顺序和其他 payload 不变。 ^[PR #8184]

## EXEC-MRV2-1c — Generation 输出必须保持真实请求 ID、精确 token 数和完成的 host 数据

- 触发：修改 generation runner request-owned cache、requires_exact_input_shape、V1 batched D2H 或 sparse conditioning。
- 强制：requires_request_ids 的模型接收本步 scheduler ID、按 batch 顺序；声明 exact input 的 MRv2 generation eager 路径检查实际 input token 数等于 scheduled 数，错配拒绝，dummy run 不套用。V1 pinned CUDA 输出逐项 nonblocking copy 后全步一次同步，未 pinned/非CUDA保留 blocking；返回前 host 完整。per-row tensor/list cardinality 不可改变，None payload 保留区别，稀疏 conditioning 的 None 表示无更新并跳过。
- 禁止：给每行合成固定/占位 ID；把 padded input 给按每请求 seq count 切分的模型；发布尚未完成 D2H 的 host tensor；把 None 归一为 {} 或丢失请求位置。
- 验收：异长、batch>1、真实 ID reorder、exact-shape mismatch、dummy/control、pinned/unpinned/CPU 与 delayed D2H 覆盖，对旧逐项 copy 检查字段和数值一致。 ^[PR #8184]

## EXEC-MRV2-1d — shared MTP 必须消费模型声明的 capability 与输出 key

- 触发：新增或修改 MRv2 的 MTP 初始化、graph wrapper、采样参数或 intermediate-buffer writeback。
- 强制：共享状态层消费通用 `mtp`/`mtp_*` capability；存在 `mtp` hook 时要求模型声明 string 或二元 tuple 的 `mtp_output_key`，不合法即拒绝。graph wrapper 只有模型声明 graph-safe 且当前模式支持 FULL 时启用，显式 disable 优先；writeback 依声明 key 与可选 validity key 按 request slot 保存，采样配置由模型声明并保留请求 seed 所有权。
- 禁止：在共享状态层硬编码某模型的 `talker_mtp_*`、TTS 输出 key 或 seed 默认；用隐式 `codes.audio` fallback 让缺失声明的模型继续运行；把支持 MTP 当作 graph-safe 声明。
- 验收：非 TTS 输出 key 的模型通过真实 MTP writeback；missing/非法 key 启动失败；无 MTP、graph-safe、显式 disable 和 eager controls 均覆盖，batch reorder 后输出与 validity 仍属于原请求。请求 RNG 的其余约束见 [EXEC-1c](rules-bridge-batch.md#exec-1c-请求随机状态跨-batching-和-yield-保持请求所有权)。 ^[PR #7781]

## EXEC-MRV2-1e — native terminal 与 abort 必须服从 deferred output 所有权

- 触发：修改 native request/receiver 注册、deferred output handoff、terminal、abort 或完成清理。
- 强制：异步 handoff 前先为 live request 保留 output hold；自然 terminal 等所有已保留输出完成交付再发送。提交 data 时先取得发送顺序所有权，abort 不得越过已提交 data。abort 清理每个目标 ID 的 receiver/load/cache 状态，即使该 ID 尚未取得 model slot；只给 live native request 发 terminal，清除其 holds 与 pending terminal，清理后迟到 output/重复 terminal 不再发布。
- 禁止：只遍历 model-slot request 而遗漏已注册 receiver；为 receiver-only ID 合成 terminal；materialization 尚未完成就自然终止，或取消后让 stale output 重新创建请求状态。
- 验收：覆盖 receiver 注册后、model admission 前取消；延迟 materialization 后自然结束和取消；data send 与 abort 竞争；重复 abort/terminal 与迟到输出。断言 receiver 状态耗尽、每个 live request 终止一次，以及已提交 data 先于 terminal。 ^[PR #7781]

## EXEC-MRV2-1f — native materialization 与 connector signal 必须各有唯一线程 owner

- 触发：修改 `NativeOutputWorker`、native async output 的 `get_output` 或 connector notification handoff。
- 强制：完成 metadata 写入和 lifecycle reservation 后才能提交有界 FIFO materializer；后台仅物化 output 并发布 native data queue，不修改 scheduler state、不 drain connector signals。engine owner thread 的 `get_output` 等待 publication 后单次 drain connector output，缓存成功值或异常，并释放原 future/data-plane 引用；close 先完成已提交 publication 再关闭 data plane。
- 禁止：后台与 owner thread 重复 drain、重复消费时重复 finalization；把 V1 background-builder 的 drain 策略直接套到 native worker；绕过实际 executor 的 TP 消费/rank ownership 约束。
- 验收：提交两个物化耗时不同的 output，断言 FIFO、容量背压、materialization/消费的线程 identity 和 connector drain 次数；重复 `get_output` 返回同一结果或异常，关闭后拒绝新提交，TP1 materializer 与多 rank executor controls 分别验证。 ^[PR #7781]

## EXEC-MRV2-1g — native delivery 失去完成保证时必须有界失败并 quarantine

- 触发：修改 native output queue、delivery ticket、connector put、drain 或 shutdown。
- 强制：物化和发送队列有界；每个 ticket 使用正的 monotonic delivery deadline，完成状态只提交一次。发送等待实际 delivery 并传播错误；首次永久失败/timeout quarantine 整个 manager，使 pending tickets 失败且拒绝新 ticket。失败不能释放未交付 output 的 hold 或发布成功 terminal；drain/connector shutdown 使用有界 deadline，shutdown 唤醒等待者并在异常路径继续清理。
- 禁止：只 enqueue 就当作 delivery 成功；吞掉后台失败后继续接收 payload；connector shutdown 无期限等 blocked connector put/close，或把 deadline 到期描述为 backend 调用已被成功取消。
- 验收：注入 permanent put failure、阻塞 delivery、排队 ticket 和阻塞 close，断言 waiter 唤醒、所有 pending ticket 收敛、后续提交拒绝、重复完成无效、未交付 hold 保留且 shutdown 在界限内报告阻塞线程。buffer snapshot 与 producer-event 另遵守 [output snapshots](rules-output-snapshots.md)。 ^[PR #7781]
