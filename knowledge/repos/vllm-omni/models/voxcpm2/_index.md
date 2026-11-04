---
title: "VoxCPM2（单 stage AR + 模型内扩散侧路,48 kHz）"
created: 2026-07-21
updated: 2026-10-09
type: index
tags: [vllm-omni, models]
sources: [vllm_omni/model_executor/models/voxcpm2/, vllm_omni/deploy/voxcpm2.yaml, vllm_omni/worker/gpu_ar_model_runner.py]
---

# VoxCPM2

以下事实在 `main @ 5d44868e` 复核。

## 名称与范围

- 正式名称 VoxCPM2（OpenBMB;示例/测试用 `openbmb/VoxCPM2`,YAML 不 pin);
  无其他官方别名有据。代码标识（非别名）：pipeline key/model_type
  `voxcpm2`;模块名 `voxcpm2_talker`;架构类
  `VoxCPM2TalkerForConditionalGeneration`;stage 键 `latent_generator`。
  MiniCPM4 基座单 stage AR TTS,文本一趟直出 **48 kHz**（全清单多数 TTS 是
  24 kHz,metrics 定义单列）。
- AR registry 单入口 `VoxCPM2TalkerForConditionalGeneration`
  →（`voxcpm2`, `voxcpm2_talker`）;**无 diffusion registry 入口**——
  LocDiT/AudioVAE 扩散计算在 talker 步循环内部（"扩散侧路"）。
- pipeline key `voxcpm2`：单 stage `latent_generator`（LLM_AR,
  `owns_tokenizer`,`final_output=True`,`engine_output_type="audio"`,
  采样约束 `detokenize=False` + stop_token_ids `[1]`）;**声明自定义调度器**
  `scheduler_cls=…voxcpm2.scheduler.VoxCPM2OmniARAsyncScheduler`
  （少数换 scheduler 类的家族）。无 stage input processor（单 stage 无桥）。
- config 管线特判：`arg_utils.py` 把 arch 映射到 model_type `voxcpm2` 并注册
  `VoxCPM2Config`;`config_factory.py` 处理其单数 `architecture` 键。
- 依赖共享模块：AR runner 的
  `get_runner_assisted_full_attention_metadata_request` 钩子
  （`gpu_ar_model_runner.py`,NPU runner 有镜像）、`utils/speaker_cache`
  （`_validate_voxcpm2_profile`）、
  [Config 组件](../../components/configuration/architecture.md)。
- serving 入口：`entrypoints/openai/tts_adapters/voxcpm2.py`
  （`VoxCPM2Adapter`,stage key `latent_generator`;声音校验:零样本 +
  预计算 profile,拒绝纯 embedding 上传）;prompt 链
  `serving_speech._build_voxcpm2_prompt` → talker 模块的
  `build_voxcpm2_prompt`。

## 目录内容

| 遇到什么 | 查看哪里 | 说明 |
|---|---|---|
| 混合 AR+扩散、统一 decode graph、显存策略 | [architecture](architecture.md) | 数据流与 reviewer 陷阱 |

## 配置与 checkpoint 差异

- 单 pipeline key、单 deploy——无拓扑/checkpoint 变体;运行期变化来自
  `hf_overrides.voxcpm2_runtime_config` 加上平台能力回退（CUDA graph /
  torch.compile 可用性检查、`decode_graph_capture_policy`）。runtime_config
  （~25 个 knob 的冻结 dataclass:LocDiT 融合、CFM/VAE CUDA graph、统一
  decode graph、发射节奏、确定性噪声/seed;未知键告警,冲突有
  `_normalized()` 消解——batched CFM 赢过 CFM graph）。
- **显存策略特例**：`kv_cache_memory_bytes: 6442450944`（6 GiB 定额）而非
  `gpu_memory_utilization`——LocDiT/VAE/graph 池的分配对 KV 记账不可见,
  定额让 H20 141 GB 与 L4 24 GB 同配置可跑（YAML 注释详述）。
- **构建即 import 外部原生 VoxCPM2 包**（`import_voxcpm2_core`,连
  `load_format=dummy` 也要,文档注明有意为之）;pin 版本不可见于树内。

## 什么时候查这里

- 审查 voxcpm2 的 runtime knob、统一 decode graph/调度器协作或显存策略;
  评审"统一改用 gpu_memory_utilization"类清理时本家族是刻意例外。
- 语义验收见 [model-validation](../../review/guides/model-validation.md)。

- [rules](rules.md)：VOXCPM2-2a 请求噪声、VOXCPM2-3a LocDiT 参数，以及 VOXCPM2-4a dense/plain/coalesce 的 subset 音频顺序与验证边界。

- [NPU estimator exact graphs 与延迟平台注册](rules-npu-graphs.md)
