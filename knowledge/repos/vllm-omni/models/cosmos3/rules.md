---
title: "Cosmos3 规则"
created: 2026-07-20
updated: 2026-11-11
type: rule
tags: [vllm-omni, models, diffusion]
sources: ["PR #4657", "PR #5001", "PR #5634", "PR #6049", docs/features/session_state_manager.md, recipes/cosmos3/Cosmos3-Nano.md, vllm_omni/diffusion/models/cosmos3/, vllm_omni/model_extras/cosmos3.py, vllm_omni/model_extras/registry.py, vllm_omni/experimental/world_models/adapters/state_cosmos3_adapter.py, vllm_omni/platforms/rocm/platform.py, tests/diffusion/models/cosmos3/test_session_memory_equivalence.py, tests/diffusion/models/cosmos3/test_cosmos3_pipeline.py, "PR #6107", "PR #5614", "PR #6325", "PR #6913", "PR #6920", "PR #7427", "PR #7971", "PR #7592", "PR #5712", "PR #9304"]
confidence: high
---

# Cosmos3 规则

只有 `COSMOS-数字字母` 是可审计规则 ID。

## Direct 代码快速入口

| PR 描述信号 | 规则组 | 第一批源码 |
|---|---|---|
| Cosmos3/Edge、layerwise offload、专有 block | COSMOS-1a | `diffusion/registry.py::_DIFFUSION_MODELS`；`diffusion/models/cosmos3/transformer_cosmos3_edge.py::Cosmos3EdgeVFMTransformer` |
| Distilled、SDE scheduler、`t_list` | COSMOS-1b | `diffusion/models/cosmos3/pipeline_cosmos3.py::Cosmos3OmniDiffusersPipeline` 及 scheduler config consumer |
| seed/generator、逐步噪声 | COSMOS-2a | `pipeline_cosmos3.py::Cosmos3OmniDiffusersPipeline` 的 request-local sampling 路径 |
| `guidance=0`、request extra | COSMOS-2b | `model_extras/cosmos3.py` → pipeline sampling params；canonical task envelope 由 shared example 拥有 |
| session manager、UND K/V、`freqs_gen`、CFG branch、request ID | COSMOS-2c | `state_cosmos3_adapter.py` → `pipeline_cosmos3.py` → `transformer_cosmos3.py` |
| online/offload/HSDP/VAE parallel 支持声明 | COSMOS-3a | capability 文档/recipe → 对应公开入口与实现路径 |
| ROCm、MI350X、AITER、latency/peak memory | COSMOS-3b | recipe 的 measurement commit/protocol → `platforms/rocm/platform.py` backend gate → 目标配置复测 |
| formatted final prompt、`prompt_suffix`、rank-zero log level | COSMOS-6a | `diffusion/models/cosmos3/pipeline_cosmos3.py::Cosmos3OmniDiffusersPipeline` |
| multi-chunk transfer、distributed VAE decode、overlap broadcast、rank-zero assembly | COSMOS-6b | `diffusion/models/cosmos3/pipeline_cosmos3.py::{_transfer_vae_executor,_sync_transfer_overlap,_forward_transfer}`；`tests/diffusion/models/cosmos3/test_cosmos3_pipeline.py` |

若描述只写 Cosmos3，先从 registry 的 class key 进入 pipeline；只有命中共享 RNG、graph
或 offload 机制时再加 [Diffusion owner](../../components/diffusion/rules.md)。

## COSMOS-1a — Edge 的 offload 声明覆盖全部专有 block

- 触发：修改 Edge transformer 层级、layerwise offload 或 component discovery。
- 强制：Edge 新增/重命名 block 时同步更新 offload block 声明，并验证启用 offload 后
  每个目标 block 都发生预期迁移。
- 禁止：从常规 transformer 的声明推断 Edge 自动覆盖。
- 验收：结构测试枚举 Edge block 与 offload 声明集合，真实 smoke 证明无遗漏驻留。 ^[PR #5001]

## COSMOS-1b — Distilled checkpoint 强制 stochastic scheduler 合同

- 触发：scheduler `_class_name` 或 checkpoint config 表明 distilled 变体。
- 强制：验证 `fixed_step_sampler_config.sample_type=sde` 且 `t_list` 非空；缺失立即失败。
- 禁止：静默使用普通 scheduler/default，使 distilled 输出“能跑但语义错”。
- 验收：有效 distilled config 选择正确 scheduler；sample type 或 timestep 缺失分别有
  fail-fast 测试。 ^[PR #5001]

## COSMOS-2a — 逐步重加噪只使用请求本地 generator

- 触发：pipeline 在 scheduler step 中生成额外噪声或根据 seed 重建 generator。
- 强制：generator 从 request sampling params 传入每次随机操作，并确认当前 diffusers 版本
  的 scheduler 真正消费该参数。
- 禁止：调用 `torch.manual_seed`/global RNG 实现逐请求确定性；并发请求会互相改状态。
- 验收：交错执行两个不同 seed 的请求与各自单独运行结果一致；相同 seed 可重复。 ^[PR #5001]

## COSMOS-2b — guidance=0.0 必须原样到达 consumer

- 触发：request/dataclass 初始化 guidance 或其他允许零值的字段。
- 强制：仅以 `is None` 判断未提供；保留显式 `0.0` 并沿 request → sampling params →
  pipeline 断言。
- 禁止：`value or default`、truthy sentinel。
- 验收：未提供使用默认，`0.0` 保持零，普通正值保持不变，三类测试都到 consumer。Cosmos3 T2I
  builder 已删除：shared example 写 `modalities=["image"]`，registry identity-return，pipeline 保留模型
  validation/normalization；不得把 extra-body registry 误当 task-envelope owner。^[PR #5001] ^[PR #6049]

## COSMOS-2c — session manager 缓存必须按 branch/layer 完整装卸

- 触发：启用 session-state manager，或修改 UND cached K/V、`freqs_gen`、CFG/request key。
- 强制：每层以 conditional `und_kv/{i}` 或 unconditional `und_kv_neg/{i}` 分支存 native
  `(K,V)` 引用，并随分支保存 M-RoPE cos/sin；仅未初始化分支可 capture，标准与 RoboLab/action
  路径都必须传 request ID。generation 开始 reset，整个 dispatch 用 `finally` drop session。
- 禁止：capture 新分支时接受缺 K/V 或缺 freqs，或 load 只初始化部分 layer K/V；已有完整
  K/V 但没有 cached freqs 时允许装载，并由 transformer 独立重算 freqs。flag 开启时
  `diffuse_transfer()` 必须 fail closed，`_bde_kv_state` 非空也必须拒绝，因为当前没有
  writer。默认关闭时保留原有 transformer-instance cache 行为。^[PR #4657]
- CFG：sequential CFG 可在同 GPU 保留 cond+uncond，CFG-parallel 每 rank 只保留自身分支，
  no-CFG 只保留 cond；UND K/V 随 SP rank 复制。该 state 隔离不等于 pipeline 并发安全。
- 验收：CPU adapter/mocked pipeline 测试覆盖 branch round-trip、encode-once、freqs restore、
  partial rejection、reset/LRU pin、RoboLab key、Transfer guard 与首次 step 前异常清理；真实
  GPU/model flag-on equivalence 和 peak-memory A/B 仍是缺口，错误 env var 下的早期零差异不能引用。

## COSMOS-3a — 支持表中的每个能力都有独立证据

- 触发：声明 Edge/Distilled、online、offload、HSDP、VAE parallel 或公开 checkpoint 支持。
- 强制：每个勾选项绑定可运行命令、checkpoint、输出和对应路径测试；未发布 checkpoint
  不得提前标完整支持。
- 禁止：用 offline unit test 支撑 online/HSDP/offload 多项 claim。
- 验收：公开矩阵逐项引用当前 head 证据；pending 项明确未验证或暂不声明。 ^[PR #5001]

## COSMOS-3b — 硬件 recipe 数字必须绑定测量 commit、协议和未覆盖维度

- 触发：引用 Cosmos3 ROCm latency、显存、determinism、backend 或最低硬件声明。
- 强制：PR #5634 的数值只绑定 vLLM-Omni `b3f4fbf9`、单张 MI350X/gfx950、HIP 7.2、
  guardrails off、Nano T2I/T2V、每配置一次 warmup 加一次 measured run。T2I 1024²/50 steps
  报告约 2.7 s、49.5 GiB reserved；T2V 1280×720/189 frames/35 steps 报告 161 s、
  120 GiB reserved/95 GiB allocated；tiling、layerwise offload、FP8 分别报告约
  183 s/38/36 GiB、163 s/84/69 GiB、149 s/107/82 GiB。数字是该协议的观察值，
  不是 merge target `b4581b29` 的复测结果。
- 禁止：把单次 measured run 变成稳定性能界、从 38 GiB peak 推断已验证 40 GiB 可运行，
  或把同配置 byte-identical 外推为跨 flags/hardware determinism。ROCm platform 的 AITER gate
  包含 gfx942/gfx950，但 PR 只实测 gfx950；gfx942、multi-GPU、Cosmos3-Super 均不得标已验证。
- 验收：任何 target-head 或硬件矩阵 claim 都重新记录 commit、完整软件栈、warm/cold、样本数、
  reserved/allocated 定义、backend assertion 与输出检查；quality 必须有独立 metric/golden。
  PR #5634 只验证尺寸、帧数和同配置复现，明确没有 quality evaluation。^[PR #5634]

共享 RNG/graph 规则见 [Diffusion rules](../../components/diffusion/rules.md)；公开证据分层见
[model adaptation guardrails](../../review/guides/model-adaptation-guardrails.md)。

## COSMOS-4a — 默认 guardrails 必须具备依赖并在缺包时 fail closed

- 触发：Cosmos3 以默认开启的 guardrails 加载或运行，或修改 Cosmos3 guardrail 依赖与缺包处理。
- 强制：在公共依赖中声明 `cosmos-guardrail>=0.3.1`，使标准 vLLM-Omni 镜像具备默认 guardrails 所需包；guardrail import 失败时必须立即抛出明确的 `ValueError`，说明 guardrails 已启用但不可用、缺少 `cosmos-guardrail`，并保留 NVIDIA Open Model License Agreement 合规提示。
- 禁止：依赖用户手动安装额外包才能获得默认行为；缺少 guardrail 包时静默继续、把状态描述为用户主动禁用了 safety checker，或把安装成功外推为 HF authentication、模型质量和其他变体能力已验证。
- 验收：干净环境安装公共 requirements 后 `cosmos-guardrail` 可导入，Cosmos3 默认 guardrails 路径可进入模型运行；模拟缺少该包时启动/构造 fail fast，错误文本同时包含包名和许可证链接。^[PR #6107]

## COSMOS-5a — Transfer 请求与提示词合同

- 触发：修改 Cosmos3 transfer 的 `edge`、`blur`、`depth`、`seg`、`wsm` 控制提示解析、控制权重或 transfer prompt 构造。
- 强制：每个 active hint 只接受声明的字段，并将 `control_weight` 校验为 finite、non-negative 且总和为正；按 hint 声明顺序归一化为相对权重，单个正权重归一为 `1.0`，单控制的绝对强度仍由 `control_guidance` 控制。Transfer 必须使用专用 system prompt，同时对 CFG 两支启用；默认在 positive prompt 追加列出 active hints 并要求逐帧遵循形状、位置和运动的 directive，`emphasize_control_in_prompt=false` 才关闭。默认启用 duration/FPS 与 resolution metadata，并以 `negative_metadata_mode=same` 同步到 negative branch；可分别关闭模板，模式只允许 `same`、`inverse`、`none`。Transfer 不自动生成 negative prompt，调用方需显式传入可选参考 prompt。
- 禁止：把 `weight` 等未声明字段静默当作 `control_weight`；接受负数、非 finite 或全零权重；用单控制归一化权重改变绝对 guidance；让 request 的 `system_prompt`/`use_system_prompt` 覆盖 transfer system prompt；把默认 metadata 或 negative prompt 行为外推到普通 T2I/T2V。
- 验收：覆盖单/多控制权重归一化、未知字段、负数、全零和 `emphasize_control_in_prompt` ablation；断言 positive/negative 两支的 system prompt、metadata mode、模板和显式/省略 negative prompt；真实 serving 还需固定模型 revision、输入、seed、步数和硬件分别验证输出质量，当前单测与附件不构成通用质量或性能证据。^[PR #5614]

## COSMOS-5b — Transfer canonical bucket 与 JSON aspect_ratio 元数据必须一致

- 触发：修改 Cosmos3 transfer 的 `size`、`resolution`、`aspect_ratio`、控制输入 bucket 选择、JSON prompt 元数据，或 I2V/V2V 的 canvas ratio 格式化与多 rank prompt 处理。
- 强制：Transfer 必须根据控制输入和选定 resolution 从 `VIDEO_RES_SIZE_INFO` 选择 canonical `(height, width, aspect_ratio)` bucket；原始请求尺寸先保存到 `COSMOS3_TRANSFER_REQUESTED_SIZE_KEY`，仅用于冲突告警，不能改变控制输入选出的几何。Transfer JSON prompt、以及 I2V/V2V 生成 canvas 的 aspect ratio 元数据必须使用 canonical ratio；若请求或 JSON prompt 的 ratio 与 canvas 冲突，所有 rank 都重写元数据，只有 rank 0 输出 warning。非 canonical canvas 必须映射到最近的受支持 ratio，而不是使用 gcd 形成新标签。
- 禁止：把 Transfer 的任意 `size=HxW` 当作最终几何或宣称其会被采用；保留控制输入已覆盖的 stale `aspect_ratio`；将元数据重写也放在 `_is_rank_zero()` 条件内导致 rank 间 token ids 不一致；用非 canonical 的精确 gcd ratio 触发无意义的冲突告警。
- 验收：覆盖 Transfer 请求尺寸与控制 bucket 不一致、请求/JSON ratio 冲突、rank 0 与非 rank 0 的 warning/rewrite 行为，以及 `1104x816`、`832x468` 等近似 canvas 映射到 `4,3`、`16,9` 的回归；JSON prompt 解析后必须逐 rank 得到相同 canonical `aspect_ratio`。^[PR #6325]

## COSMOS-6a — final prompt 仅在 rank zero 以 DEBUG 留存

- 触发：修改 Cosmos3 JSON/metadata prompt formatting、可选 `prompt_suffix`，或该 pipeline 的
  final-prompt observability。
- 强制：formatting 与可选 suffix 完成后，rank zero 使用同一最终 prompt 和 generation 路径，但只以
  `logger.debug("Final prompt: '%s'", prompt)` 记录；非 rank zero 不发出该 message。
- 禁止：恢复 INFO emission；把此处调级称为全局 prompt redaction/privacy guarantee；忘记 DEBUG
  仍会以 plaintext 输出，或借此推断其他 trace、error 或 logger 已修改。
- 验收：rank-zero test 用含 sentinel 与 suffix 的 prompt 断言最终字符串和 generation 输入不变，且
  DEBUG 才有该条记录；non-rank control 无此 emission。target 未新增 log-level test，证据仅为作者
  报告的 CPU unit/manual check，不能外推为其它模型、日志配置或生产运行结论。^[PR #6913]

## COSMOS-6b — multi-chunk transfer 必须同步分布式 VAE overlap

- 触发：修改 Cosmos3 transfer 的 chunk 边界、`previous_output` 条件帧、VAE decode，或 distributed
  VAE 的 rank 输出/collective 路径。
- 强制：只有 `vae.distributed_executor` 存在且 `vae.is_distributed_enabled` 可调用并返回 true 时才启用
  distributed VAE 路径。每个非最终 chunk 的 overlap 必须是
  `min(num_conditional_frames, chunk_frames)`；overlap 小于等于零时返回 `None`，且不调用 broadcast。
  active executor 下所有 rank 都对每个需要同步的 overlap 调用 `broadcast_tensor`：rank 0 从已 decode
  output 尾部 slice 并 `contiguous()`，其他 rank 分配与 reference video 同 batch/channel/height/width、但
  frame 维为 overlap 的 receive tensor，且使用 `self.device` 和 `vae.dtype`。仅 rank 0 拼接完整 video/control output；其他
  rank 在 collective 完成后返回本地最终 decoded output 与 metadata。
- 禁止：仅因 executor 属性存在就进入分布式路径；让任一 rank 跳过应参与的 broadcast；broadcast 整段
  output、从非 rank-zero 的空 output slice overlap，或在非 rank-zero 拼接 full/control output。该同步
  不得放宽 `COSMOS-2c` 的 session-state guard：flag 开启时 `diffuse_transfer()` 仍须 fail closed，即使
  `_bde_kv_state` 非空。
- 验收：mock executor 覆盖 enabled/callable gate、rank-zero tail slice、nonzero-rank receive shape/dtype/device、
  每个 non-final chunk 的 collective 以及 nonpositive-overlap 的 no-broadcast；仅有界的真实回归是一次
  4×GB300 multi-chunk transfer（Ulysses 4、VAE patch parallelism 4、tiling，1280×720、101 frames、24 FPS、
  53-frame chunks、5 conditional frames、2 steps），确认完成且 metadata/video 为 101 frames at 24 FPS。
  该证据不构成广泛 parity、质量或性能声明。^[PR #6920]

## COSMOS-1c — Distilled Cosmos3 必须在加载前拒绝 CFG parallel

- 触发：scheduler `_class_name` 判定 distilled Cosmos3、修改 `cfg_parallel_size` admission，或 distilled 路径的 `guidance_scale` / `negative_prompt` 语义。
- 强制：在加载 tokenizer/组件之前，若 distilled 且 `cfg_parallel_size > 1`，立即 `ValueError`，并指引 `--cfg-parallel-size 1` 与 `--ulysses-degree` 做多卡。distilled 路径强制 `guidance_scale=1.0`；调用方显式请求其他值时 `warning_once`，并说明 `negative_prompt` 不影响生成。
- 禁止：静默忽略 CFGP 导致无收益的重复实例；先加载权重再报并行非法；把 distilled 的 guidance override 写成仍消费 negative branch。
- 验收：参数化覆盖 distilled/non-distilled × `cfg_parallel_size`/`ulysses_degree`，断言 distilled+CFGP>1 在组件加载前失败；guidance resolver 覆盖显式非 1.0 的 warning 与强制 1.0。^[PR #7427]

## COSMOS-7a — 共享 HF metadata 的 Cosmos3 拓扑只能经 deploy `pipeline:` 选择

- 触发：注册 Cosmos3 pipeline、写 `cosmos3_*.yaml`，或让 `--deploy-config` 的 stage/`model_config` 进入 Cosmos3 T2I/video/policy 启动。
- 强制：T2I、video 与 policy checkpoint 共享 `model_type=cosmos3_omni` 和同一 `model_index.json` `_class_name`。`OMNI_PIPELINES` 不得注册可被该 metadata 自动命中的 `cosmos3_omni`。policy 用 `cosmos3_policy`，omni overlay 用 `cosmos3_omni_deploy`，两者都不声明 `hf_architectures` / `diffusers_class_name`，只经 YAML `pipeline:` 选中。单 stage overlay 必须 `async_chunk: false`；`guardrails: false` 等 stage extras 只有选中该 key 后才会合并。无 `--deploy-config` 时 T2I/video 仍走 CLI 单 stage fallback；`--no-guardrails` 仍是 CLI-only 路径。
- 禁止：为了让 `--deploy-config` 生效而把 `cosmos3_omni` 做成 auto-detect（会吞掉 policy/video）；静默丢弃 deploy YAML 却宣称 guardrails/stage extras 已生效。
- 验收：断言 `cosmos3_omni` 不在 registry、opt-in key 无 auto-capture 字段；`cosmos3_omni.yaml` 经 `merge_pipeline_deploy` 得到 `guardrails is False` 与 `final_output_type="video"`。^[PR #7971]


## COSMOS-8a — 采样状态与 transformer forward 必须分开控制 dtype

- 触发：修改 Cosmos3 noise、scheduler/CFG state、conditioning mask 或 transformer 输入/输出 cast。
- 强制：默认 sampling_dtype 为 FP32；初始 latent、条件图像/控制 mask、action/sound 和 scheduler 更新沿该 dtype 保持。仅在 predict_noise 的指定模型输入边界递归转 model dtype，transformer tensor/tuple 输出再转 sampling dtype。
- 禁止：把默认 sampling state 随 BF16 权重整体降精度；遗漏 nested list/tuple 的输入 cast，或让 transformer 输出 dtype 反向决定 scheduler dtype。代码允许显式 sampling_dtype override，不能把默认策略写成禁止任何 override。
- 验收：断言默认 FP32 state、BF16 model boundary 和 FP32 prediction return；覆盖 tensor/tuple、nested media、action/sound、transfer 以及显式同 model dtype 的 control。改变默认精度还须固定输入与 seed 比较输出质量，shape smoke 不能替代。^[PR #7592]

## VLLM-OMNI-PR5712-COSMOS3-VIDEO-TRANSPORT — uint8 视频直通必须逐字节保持历史呈现，guardrail 与非 np 输出留在浮点通道

- 触发：修改 `vllm_omni/diffusion/models/cosmos3/pipeline_cosmos3.py` 的 `_cosmos3_media_output`、Cosmos3 guardrail adapter，或共享 `postprocess/device_reduction.py` / `media.py` / `ipc.py` 中 `enable_device_postprocess`、`preserve_input_dtype`、uint8 frames 编码与 Cosmos3 视频输出传输路径。
- 强制：device-side reduction 默认关闭，只能经 `--video-output-transport` 的 `enable_device_postprocess` 显式开启。`_cosmos3_media_output` 仅在 output rank、纯五维 `[B,C,T,H,W]` 视频、无图像/audio/action 等辅助输出、未启用 CPU/layerwise/distributed-layerwise offload、无 guardrails 且 `output_type` 未设置或为 `"np"` 时才创建 `DiffusionMediaOutput`；该判定与开关状态无关，开关只决定 transport 准备后是否出现 `VideoTensorEncoding.UINT8_FRAMES`（关闭时仍为浮点传输）。uint8 直通要求 worker 在 D2H/IPC 前把 `[B,C,T,H,W]` 转为 contiguous `[B,T,H,W,C]` uint8 frames，在 decode dtype 内反归一后再做 float32 缩放与取整（`preserve_input_dtype=True`），按 64 MiB 时间块预算分块且不得改动输入张量；转换 OOM 必须回退浮点传输，插值等 float consumer 约束由共享 transport 裁决。
- 强制：guardrail 视频保留原浮点路径与 float32 guardrail 算术：分类器收到 uint8 display 字节，adapter 返回 float32 归一浮点（[-1,1] 往返，测试容差 atol=1/127.5），未加载 guardrail 时原对象直通。其余一律走 legacy 路径：非 output rank、含辅助输出、非五维视频、offload、`"pil"`/`"pt"`/`"latent"` 时返回 legacy `DiffusionOutput`（`media` 为 None），不得改变返回类型或把 decoded pixels 当 latents 暴露。
- 禁止：把 uint8 传输做成默认行为或新增采样 flag、私有 output type、auxiliary-media contract、engine postprocessing hook；为 guardrail、pil/pt/latent、offload、非 output rank 或含辅助输出的路径启用直通；在 decode dtype 之外反归一造成 display 字节漂移；OOM 时静默改变输出契约；把 byte parity 宣称成量化后浮点值不变——它只指最终 display/encoding 字节；也不得据此改动 Wan 的默认 reduction 算术。
- 验收：参数化覆盖 fp16/bf16/fp32 × CPU/CUDA display-byte parity（含 bf16 舍入边界与饱和值）、chunk 边界（1 字节与 64 MiB）、guardrail 输入与修改输出的全字节、`output_type` 全集、aux payload IPC round-trip、offload gate、插值与初期/后期 OOM 回退。对完全合规的输入（output rank、纯五维视频、无辅助输出、无 offload、无 guardrails、`output_type` 为 None/`"np"`）断言无论开关状态 `media` 均存在且初始 `prepared_for_transport` 为假，开关只决定 transport 准备后编码是否为 `UINT8_FRAMES`；对 guardrail、非 np output_type、offload、含辅助输出或非 output rank 的路径断言 `media` 为 None 并走 legacy 后处理。`check_video_safety` 无 guardrail 时返回原对象、有 guardrail 时保持输入/输出的 dtype/device 合同。^[PR #5712]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR9304-EDGE-PP-OWNERSHIP — Edge 权重校验必须从活模块读取 PP 归属

- 触发：修改 `vllm_omni/diffusion/models/cosmos3/transformer_cosmos3_edge.py` 的 `validate_loaded_weights` 或 `_weight_marker_is_pp_missing`，或在仓库模型代码中引入上游 `is_pp_missing_parameter` 的权重归属判定。
- 强制：marker 是否被跳过必须由本 rank 的活模块决定：`language_model.*` 直接 `isinstance(self.language_model, PPMissingLayer)`；`gen_layers.{i}.*` 检查对应 GEN slot 模块本身（UND backbone 是单一模块、只在第一个 PP stage；每个 GEN slot 是独立模块）；其余 marker 不跳过，本 rank 拥有的 marker 缺失仍必须报错。机制事实：上游 `is_pp_missing_parameter` 把缺失层名缓存在按 `id(model)` 键控且从不清理的 `_model_to_pp_missing_layer_names`，对象 id 复用会让陈旧空缓存把活 `PPMissingLayer` 误判为本 rank 拥有的实际层，继而对不属于本 rank 的 `gen_layers.1` marker 误报缺少必需权重；[上游 base-fail/head-pass 证据](https://github.com/vllm-project/vllm-omni/pull/9304#issuecomment-6369776059) 给出了该 `ValueError`。上游 vLLM 保持不动，也不得 vendoring 该 helper。
- 禁止：以该 id 键控缓存的返回值决定 marker 归属；在测试中真实写入该进程级全局缓存来构造失败（同一失败会泄漏进后续测试）；借修复放宽本 rank 拥有 layer 的必需 marker。
- 验收：测试以 `monkeypatch.setattr(model_utils, "get_pp_missing_layer_names", lambda _model: [])` 模拟陈旧缓存查找，断言 PP-missing marker 被跳过、`relu2` 等必需 marker 缺失时 `validate_loaded_weights` 仍 fail，且未污染全局缓存；验收必须核对当前 head 的 CI 和后续讨论，不能把较早的本地 CPU 证据（需预载 cosmos3 包命名空间绕过 API server import）当作最终状态。[后续维护者报告](https://github.com/vllm-project/vllm-omni/pull/9304#issuecomment-6371457488) 记录 AMD build #14506 的两个目标测试所在 shard 通过，整体仍因无关 Wan 测试失败；这是维护者报告的 CI 证据，不能据此声称真实多 stage PP parity。^[PR #9304]

<!-- kb:rule status=active since=v0.30.0 -->
