---
title: "VoxCPM2 请求噪声规则"
created: 2026-09-22
updated: 2026-10-19
type: rule
tags: [vllm-omni, models]
sources: ["PR #7866", "PR #7010", "PR #8311"]
---

# VoxCPM2 请求噪声规则

## Direct 代码快速入口

| PR 描述信号 | 规则组 | 第一批 live 源码 |
|---|---|---|
| request seed、CFM noise、batched prefill/decode、graph replay | `request-noise`：`VOXCPM2-2a` | `worker/gpu_ar_model_runner.py` request seed → `model_executor/models/voxcpm2/` request generator 与各 noise fill 站点 |
| LocDiT timesteps、CFG 或 runtime overrides | `VOXCPM2-3a` | `model_executor/models/voxcpm2/runtime_config.py` coercion → talker 与 solver |

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

## VLLM-OMNI-PR8311-VOXCPM2-DEFAULT-SYNC — VoxCPM2 运行时默认值翻转必须与 deploy YAML 的 hf_overrides pin 对账

- 触发：修改 `_VoxCPM2RuntimeConfig`（`vllm_omni/model_executor/models/voxcpm2/runtime_config.py`）的运行时开关默认值（如 `enable_batched_fsq_fusion`、`enable_batched_prefill_tail` 由 False 改 True），或增删 `vllm_omni/deploy/voxcpm2.yaml` 对应 stage `hf_overrides` 中的同名运行时字段。
- 强制：`voxcpm2.yaml` 的 `hf_overrides` 值优先于 dataclass 默认，且两者可以有意不同（如 `unified_decode_graph_max_batch_size` 在 YAML pin 为 8、dataclass 默认 64）。翻转 dataclass 默认时必须在同一 PR 对账 YAML 是否 pin 该字段：本例两个批处理开关在翻转默认的同时以 `true` 显式写入 YAML；对已 pin 的字段，只改 dataclass 不会改变 bundled 部署的实际取值。把批处理/融合开关由默认关闭改为默认开启属于改变执行路径的决定，须附目标硬件实测收益（本例 Ascend 910B3 TTFP -103ms）与音频质量回归（WAV 输出无退化），且单硬件结果不得外推为全平台承诺。
- 禁止：只改 `runtime_config.py` 或只改 YAML 而未对账另一侧的最终生效值；把 dataclass 默认与 YAML pin 的有意分歧（如 `unified_decode_graph_max_batch_size` 64 vs 8）当作必须消除的错误；以 kernel dispatch 开销降低的推断代替基准数据；默认开启改变执行路径的开关而不做输出质量验证。
- 验收：逐字段核对翻转后 dataclass 默认与 YAML `hf_overrides` 的最终取值符合意图（未 pin 字段吃到新默认，已 pin 字段显式更新或保持 pin），并随 PR 提交开启前后的 TTFP/延迟基准与音频质量检查结果。^[PR #8311]

<!-- kb:rule status=active since=v0.30.0 -->
