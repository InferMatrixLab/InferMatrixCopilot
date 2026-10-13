---
title: "Model Executor — duplex per-unit turn metadata 规则"
created: 2026-10-13
updated: 2026-10-13
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #8822"]
---

# Model Executor — duplex per-unit turn metadata 规则

## VLLM-OMNI-PR8822-MINICPMO-TURN-END — MiniCPM-o 4.5 llm2tts 必须在每次 native duplex handoff 写入 turn_end

- 触发：修改 `vllm_omni/model_executor/stage_input_processors/minicpmo_4_5_omni.py::llm2tts` 中 `turn_end`/`turn_start` 标志，或 `is_native_duplex_handoff`/`native_turn_end_handoff` 分支写入 `model_intermediate_buffer["meta"]` 的逻辑。
- 强制：每次 `is_native_duplex_handoff` 都要执行 `model_intermediate_buffer.setdefault("meta", {})["turn_end"] = native_turn_end_handoff`，True 与 False 均显式写入，与 `turn_start` 的每-unit 写法一致；`turn_end` 只能描述当前 unit，不得沿用上一 unit 留下的值。
- 禁止：只在 `native_turn_end_handoff` 为真的 unit 上写 `turn_end = True`、其余 unit 不写。connector 会跨 unit 合并 per-request meta，上一 turn 最后一个 unit 留下的 True 会粘到之后每个 unit，使其都拿到 multi-unit turn-end Talker budget：语音越过 unit 边界多生成帧并复述上一 unit 尾部文字，从第二个 turn 起表现为重复词和语速变慢。
- 验收：MiniCPM-o 4.5 多 turn、每 turn 多 unit 的 duplex 用例须断言非结尾 unit 的 meta 携带 `turn_end=False`，并确认第二个 turn 起不再出现越过文本多生成帧或复述前文的现象。^[PR #8822]

<!-- kb:rule status=active since=v0.30.0 -->
