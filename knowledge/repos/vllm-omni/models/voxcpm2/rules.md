---
title: "VoxCPM2 请求噪声与音频输出规则"
created: 2026-09-22
updated: 2026-10-09
type: rule
tags: [vllm-omni, models]
sources: ["PR #7866", "PR #7010", "PR #5452", vllm_omni/model_executor/models/voxcpm2/voxcpm2_talker.py, tests/model_executor/models/voxcpm2/test_talker_output_marker.py]
---

# VoxCPM2 请求噪声与音频输出规则

## Direct 代码快速入口

| PR 描述信号 | 规则组 | 第一批 live 源码 |
|---|---|---|
| request seed、CFM noise、batched prefill/decode、graph replay | `request-noise`：`VOXCPM2-2a` | `worker/gpu_ar_model_runner.py` request seed → `model_executor/models/voxcpm2/` request generator 与各 noise fill 站点 |
| LocDiT timesteps、CFG 或 runtime overrides | `VOXCPM2-3a` | `model_executor/models/voxcpm2/runtime_config.py` coercion → talker 与 solver |
| dense/sparse 发射、coalesce D2H 或 subset 音频 | `VOXCPM2-4a` | talker `make_omni_output` → shared sparse resolver/payload builder |

## VOXCPM2-2a — 请求 seed 必须驱动模型内 CFM/噪声流，且与 batch 位置无关

- 触发：TTS/talker 在 `forward()` 内绘制 CFM、flow-matching 或同类噪声，且公开协议声称 `seed` 可复现；修改 `_omni_seed`、`SamplingParams.seed`、per-request `torch.Generator` 或 `deterministic_cfm_noise`。
- 强制：`SamplingParams.seed` 到达 vLLM sampler 不等于到达模型内噪声点。有 seed 的请求必须在首次 prefill 从 runner 的 `_omni_seed` 构造请求私有 `Generator`，并在每个噪声 fill 站点（eager、cuda-graph、batched prefill/decode、unified replay）对 shape `(1, …)` 的行切片使用该 generator。同 text+seed 的噪声流必须与 batch 组成、request id 无关；无 seed 保持全局 RNG；`deterministic_cfm_noise` 的 request-id 哈希只服务 replay，且请求 seed 优先。
- 禁止：仅依赖 `SamplingParams.seed` 或全局 `normal_()` 宣称 speech seed 合同；用 request-id 哈希冒充用户 seed；让 seeded 行的噪声依赖同 batch 其他请求。
- 验收：跨 request 同 seed 字节一致、异 seed 敏感、batch 位次无关、seed 优先于 replay 哈希；覆盖所有噪声站点。^[PR #7866]

## VOXCPM2-3a — LocDiT 设置必须统一校验并保留至少一次 estimator 调用

- 触发：修改 `voxcpm2_runtime_config`、LocDiT `inference_timesteps`/`cfg_value`/`cfg_cutoff_ratio`，或 eager、batched、graph solver 参数。
- 强制：三字段只从 `_VoxCPM2RuntimeConfig` 消费，默认 `10`、`2.0`、`1.0`；`inference_timesteps` 钳制到至少 `2`，`cfg_value` 必须有限，否则初始化失败，`cfg_cutoff_ratio` 夹至 `[0,1]`。所有 solver 路径使用同一已解析值。
- 禁止：让 CFG-Zero* 的一阶输入跳过全部 estimator 并返回原始噪声；让 NaN/inf CFG 流入 solver；在 talker 或 graph path 保留另一套硬编码默认。有限负 CFG 当前仍可解析，不能把未实现的非负门禁写成已有合同。
- 验收：覆盖负/0/1/2/4/10 timesteps、非有限 CFG、cutoff 两端以及不同 batch size；断言 estimator 调用数、有限且非原始噪声的输出、输入 noise 未被修改，以及省略配置的默认保持。^[PR #7010]

## VOXCPM2-4a — dense 发射仍必须声明 subset 音频的请求顺序

- 触发：修改 `make_omni_output` 的 plain/coalesce D2H 分支、音频发射节奏，或通过 runtime override 切换 `_uses_sparse_audio_outputs()`。
- 强制：有音频的输出按实际发射顺序携带 `meta.req_id` 与 `sparse_audio=["1"]`，`model_outputs`/`sr` 与其一一对应；dense 模式也可能因 prefill 请求无音频而只发 batch 子集。共享 consumer 的校验与失败合同见 [EXEC-11c](../../components/model-executor/rules-runtime-hot-paths.md)。
- 禁止：把模型内部的 dense 名称当作 full-batch 保证，遗漏任一输出分支的 marker，或用默认 sparse 配置的 E2E 结果证明 dense override/coalesce 路径已执行。
- 验收：混合 `[r0,r1,r2]`、仅 `[r1,r2]` 就绪时断言 marker、id 顺序与两类输出长度；full-batch 同样携带 marker。CPU producer 测试只直接执行 plain 分支，coalesce 的设备复制与真实音频结果需单独验证，不能由 sibling test 外推。^[PR #5452]
