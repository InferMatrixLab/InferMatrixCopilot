---
title: "Diffusion runner peak memory 日志单位规则"
created: 2026-10-19
updated: 2026-10-19
type: rule
tags: [vllm-omni, components, diffusion]
sources: ["PR #8865"]
---

# Diffusion runner peak memory 日志单位规则

## VLLM-OMNI-PR8865-MEM-UNIT-LABELS — runner peak memory 日志必须按除数标注 GiB 并冻结 peak_memory_mb 命名

- 触发：修改 `vllm_omni/diffusion/worker/diffusion_model_runner.py` 中 `_sample_peak_memory_mb` 的 per-request peak memory 日志行，或该方法的返回字段 `peak_memory_mb` 的命名与语义。
- 强制：该日志行中按 `1024**3` 换算的 reserved、allocated、pool overhead 数值必须标注 GiB，并复用 `vllm.utils.mem_utils.format_gib` 生成字符串，与 vLLM 其余二进制尺寸输出一致；pool overhead 百分比守卫直接以 bytes 相除（`pool_overhead_bytes / peak_reserved_bytes`），不得经过换算后的 GB 浮点中间量。`peak_memory_mb` 按公开字段名冻结：其值实为 `1024**2` 换算的 MiB，名字与单位的不匹配在 `_sample_peak_memory_mb` 的 docstring 中说明（`Named _mb to match the public peak_memory_mb field`）。
- 禁止：把该方法中按 `1024**3` 换算出的数字标成 GB；以“修正单位”为由重命名 `peak_memory_mb`/`_sample_peak_memory_mb` 或改变其返回值；在纯标签/docstring 改动中顺手改变数值、返回值或字段名。
- 验收：日志三个分项均以 GiB 标注且数值等于对应 bytes 按 `1024**3` 换算；百分比由 bytes 直接计算；`peak_memory_mb` 字段名、返回语义与既有消费方保持不变。^[PR #8865]

<!-- kb:rule status=active since=v0.30.0 -->
