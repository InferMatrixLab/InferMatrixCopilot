---
title: "模型执行器规则 — 多模态输出标量广播"
created: 2026-10-20
updated: 2026-10-20
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #9021"]
---

# 模型执行器规则 — 多模态输出标量广播

## VLLM-OMNI-PR9021-SCALAR-BROADCAST — 多模态批量输出标量必须经共享助手广播

- 触发：改动 `vllm_omni/utils/mm_outputs.py` 的 `split_mapping_outputs` / `to_payload_element`，或 `vllm_omni/worker/gpu_generation_model_runner.py`、`vllm_omni/platforms/npu/worker/npu_generation_model_runner.py` 中把批量多模态输出 Mapping 拆分为每请求 payload 的逻辑；音频类模型在输出旁附带采样率等标量元数据时同样适用。
- 强制：批量输出 Mapping 里的 int/float/bool 原语（典型如 sampling rate）必须经共享助手 `split_mapping_outputs(outputs, num_reqs, copy_tensor)` 广播进每个请求的 payload；GPU 与 NPU 生成 runner 必须复用该助手（NPU 侧再以 `_ensure_tensor_values` 包裹），不得各自内联实现。`to_payload_element` 必须保留 `element.ndim > 0` 守卫，0-d 张量视为全体请求共享的常量，不做 `start:end` 切片。
- 禁止：不得恢复用 0-d 张量包装标量以绕过类型限制的旧 hack；不得在平台 runner 里重复 list 长度校验与逐键拷贝代码；长度不等于 `num_reqs` 的列表必须抛 `ValueError`（消息含 "one entry per request"），不得静默截断；未知类型仅记录 warning 并跳过该键。
- 验收：`tests/utils/test_mm_outputs.py` 参数化覆盖 int 与 0-d 张量两种采样率输入均广播为每请求一致的值；错位列表触发 `ValueError`；`to_payload_element` 对 0-d 张量在任意 `idx` 下原样共享同一张量。^[PR #9021]

<!-- kb:rule status=active since=v0.30.0 -->
