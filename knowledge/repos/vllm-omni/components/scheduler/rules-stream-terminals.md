---
title: "Streaming terminal 与 staged payload 规则"
created: 2026-10-06
updated: 2026-10-09
type: rule
tags: [vllm-omni, components, scheduler]
sources: ["PR #8213", "PR #7781", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_scheduling_coordinator.py#L401-L434", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_scheduler_mixin.py#L578-L596", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_generation_scheduler.py#L36-L77", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_generation_scheduler.py#L271-L303", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/core/sched/omni_generation_scheduler.py#L596-L618", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/model_executor/models/qwen3_tts/qwen3_tts_code2wav.py#L34-L69", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/core/sched/test_generation_scheduler_restore.py#L155-L175"]
---

# Streaming terminal 与 staged payload 规则


## SCHED-STREAM-1a — empty terminal 必须在上一 consumable chunk 被调度后才能替换 prompt

- 触发：修改connector receivegate、control-message drain、generation prompt replacement或emptyEOF。
- 强制：missingcodes不改prompt；presentempty snapshot清旧chunk/placeholder。该区分依赖receivegate：consumable payload仍staged时不能获取下一chunk；按requestack/schedule顺序推进，保护真实tail不被后续emptymetadata覆写。MOSS、Qwen3、Higgs、Fish、Cosy的terminal格式分别核对。
- 禁止：用truthiness混同missing与empty；last-writer-wins drain先收真实tail再收EOF后只调度empty；重复decode上一prompt，或为一个模型clear逻辑绕过全局receivegate。
- 验收：上一chunk未schedule时emptyEOF不能被fetch；正常tail→ack/schedule→emptyfinish、same-drainattempt、cancel与多request交错都验证frame守恒与no-duplicate。模型控制组必须穿过真实scheduler/connector，不只独立调用metadata更新。 ^[PR #8213]

## SCHED-STREAM-1b — native input terminal 必须绑定实际执行的 scheduler step

- 触发：修改 native MRv2 的 terminal readiness、scheduler output 包装或 generation output 的 finish 判定。
- 强制：包装 scheduler output 时，在清除 coordinator 的 per-cycle 状态前，将 input-terminal IDs 与本步实际 scheduled IDs 求交并保存为独立 `input_terminal_req_ids`。native output 只用其对应 step 的快照判定输入终态，再确认该输入单元已执行完成；未调度的 terminal readiness 保留给后续 admission。
- 禁止：消费异步旧 output 时读取可变 `finished_requests` 推断本步 terminal；把仅已接收的 EOF 当作较早执行已经结束；清理本步未调度请求的 terminal marker。
- 验收：交错两个 chunk 的 schedule/异步完成，并在前一步 output 返回前接收后一步 EOF；前一步不提前 finish，执行 terminal 输入的 step 才结束。覆盖 terminal 已 ready 但未获预算、正常完成和取消，断言各 step 的集合独立且未执行 marker 不丢失；readiness 的线程归属另遵守 [shared lifecycle](rules-shared-lifecycle.md)。 ^[PR #7781]

## SCHED-STREAM-1c — code payload 的 terminal 工作不能仅按 token prompt 是否为空判定

- 触发：支持 code-based native input 的 generation consumer 收到 terminal chunk，或修改其零长度 prompt admission。
- 强制：在已支持的 `codes.audio` 合同内区分真实 payload 与分配用 token prompt：空 prompt 仍有非空 codes 时，为当前输入单元分配执行 control slot，完成后再 finish；terminal 且没有可执行 payload 时直接进入完成清理。消费状态与 in-flight 所有权共同防止同一输入重复 admission。codec 内容的消费规则留在 [Qwen3-TTS owner](../../models/qwen3-tts/rules.md)。
- 禁止：因 prompt 为空丢弃真实 terminal payload；没有 payload 时解码 placeholder；仅凭从未赋值的 readiness flag 放行，或把当前 codes 合同扩展为任意模型 payload 协议；绕过 SCHED-STREAM-1a 的 staged receivegate。
- 验收：用真实 native coordinator/connector 驱动多次 schedule→output→下一次 schedule，分别覆盖空 prompt+非空 codes、空 EOF、正常 token prompt、in-flight 和取消，断言输入单元消费一次、真实 tail 完整且无重复。FakeAdapter 的单次 schedule 断言只能证明 admission 分支，不能证明 native 生命周期恰好一次。 ^[PR #7781]
