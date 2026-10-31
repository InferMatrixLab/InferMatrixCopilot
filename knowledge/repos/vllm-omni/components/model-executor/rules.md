---
title: "Model Executor 规则"
created: 2026-07-10
updated: 2026-10-09
type: rule
tags: [vllm-omni, components, model-executor]
sources: [vllm_omni/worker/gpu_model_runner.py, vllm_omni/worker/gpu_ar_model_runner.py, vllm_omni/platforms/interface.py, vllm_omni/platforms/musa/platform.py, vllm_omni/platforms/npu/worker/npu_ar_model_runner.py, vllm_omni/platforms/npu/worker/npu_generation_model_runner.py, vllm_omni/platforms/npu/worker/npu_model_runner.py, vllm_omni/engine/stage_init_utils.py, vllm_omni/utils/mm_outputs.py, tests/worker/test_omni_gpu_model_runner.py, tests/worker/test_gpu_ar_model_runner.py, docs/design/feature/omni_async_output_materialization.md, vllm_omni/config/model.py, vllm_omni/config/stage_config.py, vllm_omni/config/omni_config.py, vllm_omni/engine/stage_runtime.py, vllm_omni/engine/stage_engine_startup.py, tests/engine/test_async_omni_engine_stage_init.py, vllm_omni/experimental/fullduplex/, tests/e2e/features/fullduplex/, vllm_omni/model_executor/models/common/qwen3_code_predictor.py, vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py, vllm_omni/model_executor/models/qwen3_tts/configuration_qwen3_tts.py, vllm_omni/diffusion/models/minimax_h3/encoder.py, vllm_omni/model_extras/registry.py, examples/offline_inference/text_to_image/text_to_image.py, examples/offline_inference/image_to_video/image_to_video.py, tests/diffusion/models/minimax_h3/test_minimax_h3_contract.py, tests/examples/offline_inference/test_image_task_prompts.py, tests/engine/test_arg_utils.py, "PR #3422", "PR #3642", "PR #4730", "PR #4958", "PR #5073", "PR #5074", "PR #5310", "PR #5610", "PR #5671", "PR #5777", "PR #5792", "PR #5824", "PR #5957", "PR #5976", "PR #6049", vllm_omni/engine/arg_utils.py, vllm_omni/model_executor/models/qwen3_omni/qwen3_omni_moe_thinker.py, vllm_omni/model_executor/models/qwen2_5_omni/qwen2_5_omni_thinker.py, vllm_omni/model_executor/models/dynin_omni/dynin_omni.py, vllm_omni/engine/async_omni_engine.py, vllm_omni/worker/base.py, vllm_omni/worker/gpu_generation_model_runner.py, tests/model_executor/models/qwen3_omni/test_qwen3_omni_forward_contract.py, "claude-workflow-starter-private@09dca46", "PR #4795", "PR #5842", "vllm_omni/model_executor/stage_input_processors/nemotron_voicechat.py", "vllm_omni/model_executor/models/nemotron_voicechat/nemotron_voicechat_code2wav.py", "PR #5146", "PR #5068", "PR #5174", "PR #6076", "PR #6096", "PR #4765", "PR #5886", "PR #6061", "vllm_omni/platforms/npu/platform.py", "vllm_omni/platforms/npu/models/minimax_h3.py", "PR #5666", "PR #6152", "PR #5877", "vllm_omni/transformers_utils/configs/sensenova_u1.py", "PR #6119", "PR #6346", "PR #5452", "PR #6306", vllm_omni/worker/omni_connector_model_runner_mixin.py, vllm_omni/worker/omni_connector_validation.py, "PR #6149", "PR #4322", "PR #6674", "PR #6813", "PR #6775"]
---

# Model Executor 规则

## Direct 代码快速入口

| PR 描述信号 | 规则入口 |
|---|---|
| stage schema、projection、known fields | `EXEC-3a` |
| connector capability、worker/runner selection | `EXEC-3c` |
| runner preprocess、exact-shape input、MTP | `EXEC-4e` 与本页 runner 合同 |
| stage TP/PP/DP、replica、device capacity | `EXEC-3d` |
| runtime info、request RNG、bridge/batch/embedding width | `EXEC-1a`–`EXEC-1i`；[bridge/batch](rules-bridge-batch.md) |
| loader、checkpoint config、snapshot/subdir、packed weights | `EXEC-2a`–`EXEC-2i`；[loader](rules-loader-contract.md) |
| async output、MTP graph、output types | `EXEC-4c`、`EXEC-5a`–`5b`、`EXEC-7a`–`7b` |
| NPU/ROCm/MUSA/XPU runner/backend | `EXEC-10a`–`EXEC-13h`；[platform backends](rules-platform-backends.md) |
| image/video task envelope、`model_extras` | `EXEC-6a`–`EXEC-6d`；[image task envelope](rules-image-task-envelope.md) |
| preprocess phase/one-token prefill、sampling/codec hot path | `EXEC-4f`、`EXEC-8a`、`EXEC-9a`、`EXEC-11a`–`11h`；[runtime hot paths](rules-runtime-hot-paths.md) |


## 完整代码路由

- **EXEC-0a — PR 描述先选代码地图。** Direct review 先按 title/body 声明的 runner、stage、bridge、loader 或设备语义命中下表，再用 pinned changed files 验证真实模型 consumer；路径只负责范围反查。
- **EXEC-0b — 共享 producer 先于模型补丁。** 命中共享 runner、stage runtime 或 bridge producer 后立即沿 live consumer 审查；只有 producer 正确且问题只属于一个模型时才进入该模型 owner。

| PR 描述在做什么 | 精确规则组 | 第一批 live 源码 |
|---|---|---|
| strict stage config、known-fields/projection、service/stage 字段归属、runtime config | `strict-stage-config`：`EXEC-3a` | `vllm_omni/config/stage_config.py::{build_stage_runtime_overrides,_build_engine_args}` → `vllm_omni/config/omni_config.py::{_build_common_stage_config_kwargs,VllmOmniConfig.from_pipeline_config}` → 真实 startup consumer |
| connector-required stage、worker/runner selection、platform worker capability | `connector-capability`：`EXEC-3c` | `engine/arg_utils.py::OmniEngineArgs.__post_init__` → `worker/omni_connector_validation.py::validate_worker_omni_connector` → selected `worker_cls.model_runner_cls` |
| runner `_preprocess`、逐请求 metadata、prefill/decode phase、batch preprocess、MTP | `runner-preprocess`：本页“Runner 到模型的预处理合同” | `vllm_omni/worker/gpu_model_runner.py::{OmniGPUModelRunner._maybe_run_batch_preprocess,_preprocess,_build_model_kwargs_extra,_talker_mtp_forward}` → `vllm_omni/model_executor/models/<命中模型>` consumer |
| CUDA/NPU graph bucket padding、`seq_token_counts` 与 flat `input_ids`、codec exact-shape opt-in | `runner-preprocess`：`EXEC-4e` | GPU/NPU generation runner `_preprocess` → `maybe_unpad_input_ids` → codec model `torch.split` consumer |
| stage TP/PP/DP、devices、replica、inline/subprocess、visible devices、worker 启动、容量 fail-fast | `stage-runtime`：`EXEC-3d` + 本页“Stage 并行度和设备容量必须一起验收” | `vllm_omni/config/stage_config.py::build_stage_runtime_overrides` → `vllm_omni/engine/stage_runtime.py::{StageRuntime.initialize,StageRuntime._resolve_replica_physical_devices}` → `stage_engine_startup.py::{launch_stage_replica,get_headless_replica_devices}` |
| `runtime_info`、request RNG、batch compaction、跨 stage bridge/串线 | `bridge-batch`：`EXEC-1a`–`1c` | shared runner request state → stage input processor → model consumer |
| cross-stage embedding width、pre-projection buffer | `bridge-batch`：`EXEC-1d` | stage HF config → `get_inputs_embeds_size()` → `GPUARModelRunner.inputs_embeds` → model projection |
| loader dtype、只取 checkpoint config、避免整仓权重下载 | `loader-contract`：`EXEC-2a` | `vllm_omni/model_executor/model_loader/weight_utils.py::download_weights_from_hf_specific` → `vllm_omni/model_executor/models/<命中模型>` loader |
| stage `model_subdir`/`tokenizer_subdir`、partial snapshot、revision/cache repair | `loader-contract`：`EXEC-2h` | `engine/stage_init_utils.py` → stage engine args → model-specific root resolver |
| fused projection、HF source shard 完整性、consumer 委托、packed TP | `loader-contract`：`EXEC-2b`, `EXEC-2i` | H3 text encoder fused owner；nested wrapper 的 required callable 必须在 concrete runtime object 上委托 |
| async Omni output、background builder、D2H snapshot、connector drain/fallback | `async-output`：`EXEC-5a` | `worker/gpu_ar_model_runner.py::{_should_use_async_omni_output,OmniAsyncGPUModelRunnerOutput}` → platform runner → connector output |
| upstream runner/output API drift、`num_nans`、sample-budget 或 quant loader helper moved | `async-output` + output-contract | live upstream runner/output dataclasses → `gpu_{ar,generation}_model_runner.py` / `outputs/output_processor.py` → diffusion quant configs；moved symbol 必须用目标 release 的 canonical import，而非保留旧 fallback ^[PR #6606] |
| NPU ngram speculative decode、`SchedulerOutput` mutable map、profiling timing 或 upstream NPU runner ownership | `platform-backends`：`EXEC-10e`, `EXEC-13h` | `platforms/npu/worker/{npu_ar_model_runner,npu_generation_model_runner,npu_model_runner}.py` → inherited invalid-draft trimming / profiler gate → upstream vLLM-Ascend owner |
| Talker-MTP FULL graph、平台 capture 能力、显式 opt-out | `mtp-graph`：`EXEC-4c` | `platforms/interface.py::supports_talker_mtp_graph_capture` → platform override → model `talker_mtp_graph_safe` → `OmniGPUModelRunner._init_talker_mtp` |
| `model_extras`、shared T2I/T2V/I2V example、canonical prompt envelope 或 video output consumer | `image-task-envelope`：`EXEC-6a`, `EXEC-6d` | `examples/offline_inference/{text_to_image/text_to_image.py,text_to_video/text_to_video.py,image_to_video/image_to_video.py}` → `model_extras/registry.py::{build_text_to_image_prompt,build_image_to_video_prompt}` → model pipeline validation |
| CosyVoice3 non-module TensorRT CFM、stream/event ownership、raw-pointer buffer lifetime 或 context-pool reuse | [CosyVoice3 TensorRT handoff](../../models/cosyvoice3/rules.md)：`COSYVOICE3-1a` | `code2wav_core/cfm.py::ConditionalCFM.forward_estimator` → `flow_estimator_trt.py::TrtContextWrapper` |
| Qwen3-TTS adaptive chunk、EWMA target、all-frame emit、controller cleanup | [Qwen3-TTS adaptive ramp](../../models/qwen3-tts/rules.md#q3tts-3e-adaptive-ramp-是-host-side每段-opt-in-控制器不是-cuda-graph-计划) | `stage_input_processors/qwen3_tts.py::talker2code2wav_async_chunk` → `chunk_size_utils.py::{AdaptiveChunkController,compute_adaptive_emit}` → Code2Wav |
| Qwen3-Omni audio encoder 的 head/TP divisibility | [Qwen-Omni audio-encoder TP rule](../../models/qwen-omni/rules.md#qomni-1d-audio-encoder-tp-必须按-head-divisibility-局部回退) | `qwen3_omni_moe_thinker.py::{Qwen3OmniMoeAudioAttention,Qwen3OmniMoeAudioEncoderLayer}`；只影响该 encoder layer 的 QKV/output/FFN TP fallback ^[PR #4322] |

## 严格配置校验

### EXEC-3a — 严格配置校验不能靠扩大白名单

- 触发条件：新增或收紧 stage config、deploy YAML、CLI 到 runtime config 的未知字段校验，或者准备向 projection、`TypedDict`、known-fields 集合、compat/service allowlist 增加字段。
- 必须做：先冻结 PR 基线，逐个枚举相对基线新增的可接受字段；对每个字段写清公开 producer、入口归一化、唯一 canonical owner 和最终 runtime consumer。CLI/legacy 别名只允许在入口转换，转换后不得继续出现在核心 schema；service-only 字段必须交给 service owner，不能混入 stage runtime config。直接 constructor、结构化 YAML 和真实 startup builder 必须使用同一组最小正负样例核对。
- 禁止：不能因为严格校验开始报错，就把报错字段加入核心 dataclass、projection、known-fields 或专门白名单；不能接受字段后在分区时丢弃；不能为同一语义新增两个核心字段再补优先级；不能用“构造成功”“字段不在最终 payload”或只有测试 fixture 使用来证明字段必要。
- 验收：每个新增可接受字段都必须有 PR 前已存在或本次明确新增的真实 consumer，并有从公开入口到 consumer 的正向断言；无 consumer 或 owner 不在 stage runtime 的字段必须在入口报错；别名与 canonical 同时出现必须报冲突。最终 diff 中新增的 schema 字段数量应与 producer-consumer 表逐项一致。

### EXEC-3b — stage loader metadata 与 stateful chunk 能力必须一同到达 consumer

- 触发：新增 per-stage `model_subdir`/`tokenizer_subdir`、模型 architecture override，
  或模型在 async chunk 间保留执行状态。
- 强制：从 `PipelineConfig`/deploy 合并结果把 checkpoint/tokenizer 子目录和
  `retains_state_across_chunks` 传入最终 `OmniStageModelConfig`/runner；空的
  `model_arch` 表示使用 checkpoint 自带 architectures，而不是一个空 override。
  Stateful chunk 必须与 scheduler 的容量计数和 connector requeue 合同一起审查。
- 禁止：只在 legacy YAML 或 model helper 中记录子目录；用空字符串覆盖 checkpoint
  architecture；让 scheduler 看不到仍占用 runner slot 的 parked request。
- 验收：structured 与 legacy stage config 都能读回子目录/状态字段；Audex 等多子目录
  checkpoint 做 loader smoke；stateful async-chunk 测试证明容量上限、requeue 和 cleanup。

### EXEC-3c — connector-required stage 必须在 worker 启动前验证 runner capability

- 触发：stage 增加完整 payload 输入、downstream processor、explicit connector role，或修改 GPU/NPU/XPU worker/runner 选择。
- 强制：从 `OmniEngineArgs` 的已解析 `worker_cls`（包括 platform 自动选择）读取 `model_runner_cls`；只要 stage 需要 connector，就要求它是 `OmniConnectorModelRunnerMixin` 的子类，并在初始化前以明确错误拒绝不兼容 worker。所有平台 worker 都应把实际 runner 公开为 `model_runner_cls`，不能仅在 `init_device` 内硬编码构造。
- 禁止：按 model architecture hard-code connector 初始化 allowlist；等到 request parked 或 connector method 缺失才失败；把无 connector 的普通 async scheduler stage 误判为 connector-required。
- 验收：覆盖显式兼容/不兼容 worker、platform-resolved worker、无 connector configuration 的 control，以及 GPU、NPU、XPU AR/generation worker 的 runner-class 暴露和 mixin 继承。^[PR #6149]

### EXEC-3d — StageRuntime inline 资格必须按 whole-pipeline stage 数、replica 与 owner 精确判定

- 触发：修改 diffusion replica 的 `StageRuntime` inline/subprocess 初始化、pipeline
  `inline_diffusion`、`custom_pipeline_args` 或 stage/replica topology。
- 强制：`use_inline` 只能满足 `plan.num_replicas == 1 and (self._num_stages == 1 or
  inline_diffusion or custom_pipeline_args)`；其中 `_num_stages` 是 whole-pipeline stage 数。默认仅
  single-stage/single-replica inline；multi-stage 默认 subprocess，仅显式 `inline_diffusion` 或已有
  custom-pipeline in-process owner 可 opt in，且仍须 single replica。
- 禁止：把“当前 diffusion stage 是一个”错当 whole pipeline single-stage；仅凭一个 replica inline
  split Stage 1；让任一 opt-in 绕过 multi-replica 隔离，或称所有 subprocess/共享内存边界都已消除。
- 验收：至少覆盖默认 single-stage→inline、two-stage→subprocess、multi-stage 两种 opt-in，以及
  multi-replica 对默认/opt-in 的否决。PR #6813 新增的单元测试只参数化默认 one/two-stage，未覆盖
  flag、custom、multi-replica 或真实 in-process execution；其 L20X benchmark 仅是所列 workload 的作者证据。^[PR #6813]

### EXEC-4a — full-duplex chunk metadata 必须按 span 隔离且可安全序列化

- 触发：`experimental/fullduplex` 修改 PCM/audio span、force-listen、rollback、
  resumable append、runtime-control 或 silence continuation。
- 强制：把 speech/PCM metadata 与 rollback 状态绑定到具体 span；resumable append 重新
  arm 所需 EOS 但不得重复 turn EOS；runtime-control 输出先转换 dataclass/enum 等为
  JSON-safe 值，并在等待后重新检查 session/request 是否仍然有效。
- 禁止：用上一 chunk 的 force-listen 或 speech marker 污染不规则下一 chunk；把 stale
  session 的 silence continuation 继续发送；直接把 Python runtime object 放进控制消息。
- 验收：覆盖不规则 PCM span、rollback、resumable append、stale continuation 和控制
  redaction；首批测试看 `tests/e2e/features/fullduplex/` 下的 input、runtime adapter
  boundary 与 runtime-control redaction。

### EXEC-4b — shared runner 必须保留 resolved model architecture 与 model-owned hooks

- 触发：模型 architecture override 为空、checkpoint 含多个子目录，或模型实现拥有
  native duplex/sampling policy。
- 强制：空 `model_arch` 回退到 checkpoint architectures；loader 先按 resolved
  architecture 解析 connector/checkpoint 子目录；共享 runner 只提供 typed rows 和
  hook seam，真正的 model-owned sampling/turn-boundary policy 由模型 consumer 执行。
- 禁止：用空 override 覆盖 checkpoint metadata；只按默认架构初始化 connector；用
  generic runner policy 替换 MiniCPM/Audex 等模型自己的 native policy。
- 验收：覆盖 blank override、多子目录 checkpoint、hook 调用顺序和 mixed-batch
  request-local metadata；初始化失败必须在 scheduler/worker 继续运行前暴露。

### EXEC-4c — Talker-MTP graph 能力必须保留 tri-state 语义

- 触发：修改 Talker-MTP 的 FULL graph wrapper、模型 `talker_mtp_graph_safe`、平台 graph
  capability，或把 talker 与其他 stage 共用同一层 wrapper。
- 强制：只有实际 Talker stage 从平台声明模型能力；runner 读取能力时保留三态：未声明
  `None` 回退到既有 `has_separate_talker`，显式 `True` 允许 wrapper，显式 `False` 即使存在
  separate talker 也必须阻止 wrapper。上述判断只控制 dedicated Talker-MTP FULL graph
  wrapper，不能跳过 MTP buffer 初始化或关闭该 stage 其余 compile/capture 路径。
- 禁止：用 `has_separate_talker or talker_mtp_graph_safe` 吃掉显式 `False`；把 platform
  默认支持误写成所有模型未声明时一律开启；因一个算子 capture 失败而全局切 eager。
- 验收：CPU runner 测试至少覆盖 explicit false、未声明且 separate talker、explicit true，
  以及无 separate talker 的非 Talker stage。MUSA 当前 override 为 false，因为其 stream
  capture 到 `torch.multinomial` 会报 `operation not permitted when stream is capturing`；
  其他平台沿接口默认 true，但仍受模型声明与 FULL graph mode 共同约束。^[PR #5671]

## Runner 到模型的预处理合同

- 触发条件：修改或排查 runner `_preprocess` 的逐请求 metadata 生产、phase 判定、normal/batched preprocess 选择、MTP 路由条件，或多模型共用的输入预处理合同。
- 主要 owner：先查 `vllm_omni/worker/gpu_model_runner.py` 的 `_preprocess` 和调度状态，再查 `vllm_omni/model_executor/models/` 中命中的 consumer。共享 producer 或路由错误不能只在某个模型内打补丁。
- 必须做：把每个字段的生产时点、Python 类型、逐行语义、normal/batched preprocess 入口和后续路由写成明确 contract；phase 必须来自 prompt progress 等真实调度状态，不能用当前 span 长度代替。
- 回归测试：最近 owner 是 `tests/worker/`。直接调用生产 `_preprocess`，在同一 mixed batch 中放一个应进入和一个不应进入该路由的逐行对照，断言 metadata、hook 调用和 embedding/code 等可观察结果。只在模型 helper 测试里手动喂 metadata，或把真正的后续 helper 整个 mock 掉，不能证明 runner contract。
- MTP 内部行为：只修改 `_talker_mtp_forward` 内部的采样参数、空 batch、output key、graph wrapper 或 generator 生命周期时，使用最近生产 owner 的针对性测试；只有它同时改变逐行 phase 或 `_preprocess` 路由时，才强制上述 mixed-batch 合同回归。
- 平台边界：查 GPU generation/AR runner 和 NPU 等平台是继承共享实现还是覆盖它；没有 live 继承或调用链证据时，不得声称所有平台合同已经一致。
- 文档与兼容：已向 out-of-tree 模型开放的字段必须从 model-contribution 入口可发现，并明确旧 runtime 和新 consumer 需要怎样配套升级；不能用某个 in-tree 模型的 fallback 充当公开合同。

### EXEC-4e — exact-shape codec 只能取得语义长度的 `input_ids` view

- **触发：** generation runner 的 graph bucket padding、`_preprocess`、flat `input_ids`、
  `seq_token_counts`，或 codec 模型以逐请求 token counts 分割输入发生改变。
- **必须：** CUDA/GPU 与 Ascend NPU runner 都在 `_preprocess` 后、模型前调用同一 opt-in
  helper；仅 `requires_exact_input_shape` 的模型取得截至 semantic total 的 `input_ids`
  （GPU `num_tokens_unpadded` 与 NPU `scheduler_output.total_num_scheduled_tokens` 相同）。
  positions、attention metadata、graph bucket/buffer 和 forward context 继续 padded。以
  `seq_token_counts` 精确 `torch.split` flat input 的 codec consumer 必须显式 opt in。
- **禁止：** 不得把 padding token 计入 `seq_token_counts`，不得复用
  `requires_raw_input_tokens`（只决定 token IDs 与 embeddings），不得无条件 unpad、只修
  GPU/NPU 一侧，或改写 graph capture 的 padded metadata/buffer。
- **验收：** CPU/mock runner regression 构造 semantic 3 tokens→graph bucket 4，捕获真正进入
  model boundary 的 argument：opt-in case 断言 `input_ids.numel() == sum(seq_token_counts) == 3`，
  exact bucket 不变；negative control（无 flag）仍收 4-token padded view。helper 单测覆盖 flag、
  缺失 flag、`input_ids is None`；GPU/NPU call site 共用 helper。NPU graph hardware/E2E 须另验，
  CPU mock 不证明硬件或性能 parity。^[PR #6775]

## Async Omni output materialization

### EXEC-4d — GPU/NPU AR runner 必须在统一采样点接入 duplex hook

- 触发：GPU/NPU AR runner 或 full-duplex sampling hook 修改 `prepare_duplex_sampling` 的接入与采样顺序。
- 强制：GPU、NPU sibling runner 共同使用 `DuplexSamplingRunnerMixin`；初始化、模型加载解析和 state 更新走统一 helper，并在 `model_sample` 前对 prepared metadata 应用逐行 hook。
- 禁止：只在 GPU `_sample` 接入而遗漏 NPU；在 `model_sample` 后调用 hook；复制平台专属实现或依赖 import side effect 建立 hook。
- 验收：静态测试枚举全部 AR runner，断言 mixin、四个 hook site 与 sampler 前顺序；再覆盖 duplex 行映射和无 hook 模型的 no-op。 ^[PR #6346]

### EXEC-5a — snapshot、live drain 与 fallback 必须构成同一个 output-cycle 合同

- 触发：修改 `GPUARModelRunner` async output、background builder、D2H copy、connector
  output、model opt-in/postprocess，或 prefix cache/spec decode/routed-expert/platform guard。
- 强制：下一 decode step 会覆盖或 mutate 的 scheduler metadata、request mapping、token span
  和 CUDA output 必须先做 step-owned snapshot；可复用 tensor 要 clone，并由独立 stream、
  pinned host buffer 和 ready event 固定 D2H 生命周期。采样 token feedback 与下一步所需的
  model postprocess state 保持 eager。未 snapshot 的 connector signal 必须只有 background
  builder 一个 drain consumer；`get_output()` 必须 join builder 并传播 background exception。
- 禁止：把 live connector state 写进 snapshot 清单；让同步路径和 background path 重复 drain；
  把“CUDA/ROCm 已验证”写成不存在的 platform guard；在 prefix cache、speculative decode、
  routed-expert output 或非 eager stateful postprocess 下强开异步路径。
- 验收：覆盖 reusable buffer 被下一 step 覆盖、逐请求 snapshot 隔离、connector drain 次数/
  顺序、builder exception 和每个 compatibility guard 的 synchronous fallback。平台结论按真实
  runner 分开：CUDA/ROCm 已验证，XPU/MUSA 可能进入共享 GPU path 但未验证，Ascend NPU 的
  独立 runner 仍同步 materialize。 ^[PR #5610]

### EXEC-5b — NPU KV connector finalize 必须按 PP 所有权延迟

- 触发：NPU AR 或 generation runner 在 speculative decoding、pipeline parallel 或 `broadcast_pp_output` 场景调用 `maybe_get_kv_connector_output`。
- 强制：仅当 `self.speculative_config is not None` 且当前 rank 是 `get_pp_group().is_last_rank` 或 `self.broadcast_pp_output` 为真时设置 `defer_finalize=True`；AR 与 generation runner 必须保持相同条件。
- 禁止：仅依据 speculative decoding 开启状态就在所有 PP rank 延迟 finalize，或继续使用不区分 PP 所有权的 `clear_kv_metadata` 反向条件。
- 验收：覆盖无 speculative config、speculative config、last rank、非 last rank 和 `broadcast_pp_output` 的组合，断言两个 runner 传入 connector 的 `defer_finalize` 值及最终 finalize 时机一致。 ^[PR #6096]

## Stage 并行度和设备容量必须一起验收

- 触发条件：修改或排查全局 CLI、per-stage override、deploy YAML、平台 overlay、stage 并行度、`runtime.devices`、设备映射或 worker 启动。
- 必须做：沿公开入口展开配置合并，记录每个 stage 最终生效的 TP、PP、DP 和其他会增加 local world size 的并行参数，以及解析后的设备列表；在创建底层 worker 前校验所需设备数不超过该 stage 的可见设备数。
- 禁止：不能只证明并行参数和设备参数各自解析正确；不能截断设备列表或用 fallback 继续；best-effort 的锁、清理、日志和观测代码不能吞掉配置、拓扑或容量错误。
- 验收：至少有一个从全局参数加 per-stage override 进入最终配置的负向测试。并行 world size 大于解析后设备数时，测试必须在 worker 创建前失败，并在错误中报告 stage、并行参数、所需设备数和实际设备列表；匹配的配置必须保留原有启动行为。

源码中已经存在校验、但实际日志仍越过它继续执行时，必须读完校验所在函数及 caller 的完整控制流，包括后续 `except`、fallback 和返回值。只有代码 diff 或运行环境证据排除控制流吞错后，才可以把原因留给版本或镜像不一致。

最终配置、设备容量、被绕过的校验和 worker 失败已经形成一条与用户日志一致的因果链时，根因即闭环，应先报告。只有版本差异会改变这条因果链或修复位置时才继续查 tag、commit 和历史 PR。

首次根因报告只需要四项：用户最终配置、runtime 实际设备、失效或被吞的前置校验、与日志一致的失败点。四项齐全就立即给出“结论 / 证据 / 未验证边界”；相关 warning、硬件排除、完整版本历史和扩展修复建议随后按需补充，不得延迟首次报告。

当 issue 已提供最终配置和失败堆栈、current source 可读时，先按 [架构职责锚点](architecture.md#当前源码职责锚点) 完成一轮窄调查，目标是在 owner 确定后两分钟内给出首次根因。超时仍未闭环时必须指出缺失证据，不能静默扩大到历史 commit、其他模型或环境猜测。

Stage 拓扑错误的最小充分源码证据只有三段：一处最终配置日志加全局/per-stage 合并函数；一处启动前容量校验及其完整异常控制流；一处与日志一致的 worker 失败点。三段一致即可决定“配置触发 + fail-fast 缺陷”的主要修复位置，不再为首次结论读取 config factory、模型 pipeline、完整 deploy、spawn 实现或 tag diff；只有三段之间发生冲突时才补这些文件。

### EXEC-14a — 复合模型配置必须使用注册的 typed PretrainedConfig

- 触发：新增复合模型的 transformer 配置、嵌套 `llm_config`/`vision_config`，或修改 `transformers_utils.configs` 的 AutoConfig 注册与模型加载路径。
- 强制：为顶层和嵌套配置定义 typed `PretrainedConfig` 子类；从 checkpoint 加载时把嵌套 dict 提升为对应配置对象，并同步完成 `AutoConfig.register`、模块导出和 pipeline 的 `from_pretrained` 消费，默认值集中在 typed config 构造器中。
- 禁止：在 pipeline 内手写 JSON/`SimpleNamespace` 和分散的 `.get` 默认值；只注册顶层 model type 却遗漏嵌套配置；让配置注册、导出和真实 consumer 使用不同的 schema。
- 验收：local 与 HF 配置都能解析顶层及嵌套 model type，断言字段和默认值、AutoConfig 注册及模块导出一致，并用最小真实模型构造覆盖配置对象被 consumer 接收。^[PR #5877]

### EXEC-15a — Ming-family 可复用模块必须归一到 `common/ming`

- 触发：第二个 Ming-family 模型复用或重命名 CFM、Aggregator、speaker embedding 等实现，或修改其模型侧构造参数。
- 强制：可复用实现以 `vllm_omni/model_executor/models/common/ming/` 为唯一归属，`ming_tts` 与 `ming_flash_omni` 从该目录导入；保留 checkpoint-facing 的类和参数语义，模型侧必须显式传入真实 latent width，且 checkpoint 配置覆盖默认值。
- 禁止：在任一 Ming 模型目录保留近似副本，让一个模型跨包导入另一个模型的可复用实现，或在重命名后遗留旧 import；不能依赖 shared `Aggregator` 的默认 `in_channels` 代替模型真实的 `latent_dim`。
- 验收：共享模块测试断言 `Aggregator`、CFM、speaker extractor 等符号的 `__module__`，确认模型包没有本地重复类；用 Ming-flash 的真实 latent width 构造并断言输出 shape，同时运行 Ming-TTS 与 Ming-flash 的模块回归。 ^[PR #6119]

- **EXEC-0c — generation capture 与 replay 必须保持 preprocess 输入路径一致**
  - 触发：修改 generation runner 的 `_dummy_run`、CUDA-graph capture/replay 输入路径，或模型声明 `has_preprocess`。
  - 强制：generation override 必须在与 base runner 相同的决策点保留 `has_preprocess` 分支；capture 与 replay 必须读取和写入同一对固定 `input_ids.gpu`/`inputs_embeds.gpu` buffer，且不得依赖继承实现的偶然覆盖。
  - 禁止：capture 读取 `input_ids` 而 replay 由 `_preprocess` 写入 `inputs_embeds`，删除 generation override 后让测试仅因 fallback 到 base 实现而通过，或在不同分支改变 graph 输入语义。
  - 验收：静态 AST contract 同时检查 base `_dummy_run`、base `_preprocess` 和 generation `_dummy_run`，并验证 generation override 实际存在。AST 属性链检查只证明 guarded branch 的 buffer 引用；声称 CUDA graph 数值或输出一致时，必须另提供真实 `has_preprocess` capture/replay 验证，未执行则明确保留缺口；不能外推 NPU parity。 ^[PR #5452]

### EXEC-16a — 共享 GroupNorm 融合算子必须保持 eager、数值与布局合同

- 触发：在 `vllm_omni/model_executor/models/common/ops/` 增加或复用 `fused_group_norm_silu`、`fused_adaptive_group_norm_silu`，或在混合精度模型中替换 GroupNorm/AdaGN 与 SiLU 组合。
- 强制：wrapper 必须在 Triton/CUDA 不可用时回退到 eager 实现，支持 `(B, C, *spatial)` 并在计算后恢复原 shape；启动 kernel 前将输入和 `torch.chunk` 产生的 `scale`/`shift` materialize 为所需连续布局；均值和方差使用 FP32 的 Welford/centered reduction，输出 dtype 与 eager GroupNorm 一致，autocast 下保留其 FP32 输出语义。
- 禁止：仅凭 `HAS_TRITON` 让非 CUDA、unsupported dtype 或错误布局进入 kernel；使用 `E[x^2] - E[x]^2` 计算方差；对 strided conditioning view 使用 `view`；或用同 dtype 的不稳定 reference、输入 dtype 推断 autocast 输出 dtype。
- 验收：以 eager reference 覆盖 FP32/FP16/BF16、任意 spatial rank、非 contiguous 输入、chunk-derived `scale`/`shift` 和 autocast；加入大偏移小方差用例验证无 NaN 和有界误差，并分别断言 native fallback、shape、dtype 与 CUDA kernel 数值正确性。^[PR #6306]
