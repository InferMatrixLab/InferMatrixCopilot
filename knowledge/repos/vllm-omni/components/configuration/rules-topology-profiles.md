---
title: "topology 与部署 profile 合同"
created: 2026-09-04
updated: 2026-11-26
type: rule
tags: [vllm-omni, components, config]
sources: ["PR #4222", "PR #4795", "PR #5604", "PR #5842", "PR #5885", "PR #6082", vllm_omni/config/stage_config.py, vllm_omni/config/config_factory.py, vllm_omni/engine/stage_init_utils.py, vllm_omni/engine/stage_runtime.py, tests/engine/test_async_omni_engine_stage_init.py, vllm_omni/deploy/, "PR #6186", "PR #6813", "PR #6291", "PR #6829", vllm_omni/config/pipeline_registry.py, tests/config/test_config_factory.py, tests/config/test_omni_config.py, tests/config/test_pipeline_registry.py, tests/utils/test_tracking_parser.py, "PR #6230", vllm_omni/entrypoints/utils.py, tests/entrypoints/test_serve.py, tests/entrypoints/test_utils.py, "PR #7272", "PR #8184", "PR #10595"]
confidence: high
---

# topology 与部署 profile 合同

`CONF-5a`–`CONF-5j`：冻结 topology 的单一来源、可选辅助 stage 注入、缺少 model_type 的多 stage checkpoint 路由，以及模型专用 deploy profile 的一致传递。触发条件与其余审查组见 [Configuration 规则](rules.md) 的 Direct 代码快速入口。

## CONF-5a — 冻结 topology 只保留一份，部署开关决定 wiring

- 触发：同一模型的 sync/async processor、普通/async chunk 或部署加速出现多份近似
  pipeline/stage YAML。
- 强制：相同冻结拓扑共用一份 pipeline config，同时声明可选 processor；由 deploy
  flag 决定 wiring。模型 package `__init__` 保持轻量，FP8 等部署加速留在 deploy 层。
- 禁止：为一个 runtime flag 复制整套 topology；把无关 payload bug 或平台 cleanup
  混入 config migration。
- 验收：两种 wiring 都从同一 topology 展开，配置 diff 只包含预期 deploy 字段；
  同卡多 stage 的 `gpu_memory_utilization` 总预算不超过可用容量。

## CONF-5b — `trust_remote_code` 的未指定值必须保留 deploy 优先级

- 触发：CLI、structured factory、legacy stage factory 或 deploy YAML 同时提供
  `trust_remote_code`。
- 强制：调用方显式 `True`/`False` 才覆盖 per-stage deploy 值；未指定用 `None` 表示并
  保留 YAML 值，HF config resolution 才把 `None` 收敛为安全的实际 bool。
- 禁止：把 `store_true` 的 absent-False 当成显式 False 无条件写入 override；structured
  与 legacy 各自实现 precedence；在末端用默认值静默覆盖用户明确的 False。
- 验收：覆盖“未指定、显式 True、显式 False”三态，并从 structured/legacy 两条路径
  断言最终 stage config；`with_trust_remote_code_override` 是唯一合并入口。

## CONF-5c — 可选辅助 pooling stage 必须在最终 topology 中显式注入

- 触发：为现有 pipeline 增加可选辅助 stage，或新增 `--forced-aligner`、`--forced-aligner-config`、`--forced-aligner-device` 这类只由 orchestrator 读取的开关。
- 强制：在最终 merge 前把 CLI/YAML 解析为一个 canonical stage config；任一 aligner 来源生效时复制 pipeline/deploy，追加连接到前一 stage 的 `runner="pooling"` 终端 stage，并将 `default_pooling_params.task`、设备和 decoder hook 投影到该 stage；无来源时保持原 topology。
- 禁止：只检查 `forced_aligner` 而忽略 config-only 注入；原地修改调用方的 pipeline/deploy；把 orchestrator-only flags 泄漏为每个 stage 的 engine 参数；用 `execution_type` 猜测应构造 `PoolingParams` 还是生成参数。
- 验收：覆盖模型参数、config-only、无参数三态；断言原 pipeline/deploy 未改变，最终逐 stage config 含正确模型、`runner`、pooling task、设备和 decoder hook，并证明 stage 初始化按 `runner` 选择 `PoolingParams`。 
^[PR #4795]

## CONF-5d — 无 `model_type` 的多 stage checkpoint 必须闭合别名与架构路由

- 触发：接入缺少 `model_type` 或 `architectures` 的 NeMo 风格多 stage checkpoint，或新增其 pipeline alias、architecture 路由与默认 deploy 配置。
- 强制：同时登记所有 stage architecture 到 `_ARCH_TO_MODEL_TYPE`，注册统一 `NemotronVoiceChatConfig`，让 alias、path-basename fallback 与 `default_deploy_config_name` 指向同一 pipeline；不得只依赖路径猜测。
- 禁止：只注册 thinker 或只提供一个 alias；让空的 `model_arch` 覆盖 checkpoint architecture；让 sync 与 async deploy 配置解析出不同的 stage 拓扑。
- 验收：用 bare `config.json` 和三个 architecture 分别验证 model type、pipeline、stage config 与默认 deploy 名称；sync/async 配置只切换 processor wiring，并保持三 stage 顺序与 stage architecture 一致。
^[PR #5842]

## CONF-5e — MiniCPM-o CFM Graph 参数必须投影到实际平台与 profile

- 触发：修改 MiniCPM-o 4.5 bundled deploy YAML 或 Code2Wav connector extra 中的 CFM graph 开关、预算或配置传递。
- 强制：按目标 revision 展开 `base_config`、平台及 stage 覆盖，跟踪 connector CFM 参数到 `MiniCPMO45Code2Wav` / `BatchedToken2Wav`；Ascend 的 Stage 2 本地开关和上限按 CONF-5g 验证。
- 禁止：要求派生 YAML 重复声明继承字段、所有 profile 使用同一预算，或把 #6082 的 CUDA 路径解释为所有非 CUDA 设备永远 eager；配置存在不等于捕获、重放或测量已经发生。
- 验收：对支持的 profile 检查展开后的启停、默认/覆盖值和非默认配置确实到达 consumer；分别覆盖支持的平台与不支持的 eager 路径，不从 connector 改动推断 stage topology 改变。 ^[PR #6082] ^[PR #10595]

## CONF-5f — π0 的 checkpoint、registry 与 deploy 必须闭合为单 diffusion stage

- 触发：新增或修改 π0 的 `pipeline_registry.py`、`vllm_omni/deploy/pi0.yaml`、`OmniDiffusionConfig` 的 `type: pi0` autodetect，或 `Pi0Pipeline` 的 stage declaration。
- 强制：让 `pipeline: pi0`、`OMNI_PIPELINES["pi0"]`、`Pi0Pipeline` 与 checkpoint `type: pi0` resolver 闭合；deploy 配置只能声明一个 diffusion stage，最终输出类型为 `action`，并让 `policy_server_config` 的 `image_resolution`、`action_horizon`、`action_dim`、camera 数量与 `model_config` 保持一致。
- 禁止：只凭 checkpoint 路径或文件存在推断 pipeline，额外添加 model-executor 层或多 stage topology，或让 OpenPI metadata 与最终 stage/model config 的动作尺寸不一致。
- 验收：无显式 deploy 配置的 checkpoint autodetect 能解析到 `Pi0Pipeline`；解析最终逐 stage config 断言只有一个 diffusion stage、`final_output_type=action`，并核对 `pi0.yaml` 与 websocket handshake metadata 的关键字段一致。^[PR #4222]

## CONF-5g — MiniCPM-o NPU Code2Wav 开关必须按 Stage 2 传递

- 触发：修改 MiniCPM-o 4.5 bundled deploy YAML 或调整 `code2wav_enable_npu_graph`、`code2wav_max_npu_graphs`。
- 强制：Ascend 通过 Stage 2 的 `additional_config` 投影这两个 key，并保持最终 runner outer eager；encoder/CFM/HiFT 子图边界见 [MCPMO-1f](../../models/minicpm-o-4-5/rules-code2wav-runtime.md#mcpmo-1f--ascend-code2wav-保持-outer-eager按实际-inner-graph-路径验收)。用 `_apply_platform_overrides` 与 `merge_pipeline_deploy` 核对实际 profile，包括替换而非深合并的 `additional_config` 和继承的 connector 设置。
- 禁止：把这两个 key 放进 shared connector extra、全局配置或其他 stage；用全局 eager 改变 Stage 0/1；对所有 revision 和 profile 硬编码 `PIECEWISE` 或统一 graph cap。
- 验收：按 revision 断言最终 Stage 0/1 runner 模式、Stage 2 outer eager、开关及 cap，覆盖关闭和 cap 0。含 #10595 的配置见[基底 YAML](https://github.com/vllm-project/vllm-omni/blob/94de758f0678bb8d5e770349c463f1f267a91f20/vllm_omni/deploy/minicpmo_4_5.yaml#L253-L376)与[双卡 overlay](https://github.com/vllm-project/vllm-omni/blob/94de758f0678bb8d5e770349c463f1f267a91f20/vllm_omni/deploy/minicpmo_4_5_2gpu.yaml#L25-L91)：Stage 0/1 为 `FULL_DECODE_ONLY`；Stage 2 基底 cap=32，双 NPU overlay cap=96，Talker/Code2Wav 各两副本。断言继承后的五步 CFM 与副本/设备映射，参照[部署配置测试](https://github.com/vllm-project/vllm-omni/blob/94de758f0678bb8d5e770349c463f1f267a91f20/tests/config/test_minicpmo_4_5_duplex_mrv2_deploy.py#L84-L117)。配置解析只证明投影，不是硬件资格。 ^[PR #5604] ^[PR #10595]

部署 YAML 的展开顺序见 [deploy-yaml](deploy-yaml.md)；stage 执行合同见 [model-executor 规则](../model-executor/rules.md)。

## CONF-5h — 多组件 checkpoint 必须闭合 root 身份、stage 子目录与默认部署

- 触发：新增由多个子目录组成、root `config.json` 自报 composite architecture，或需要按 stage 加载不同 checkpoint/tokenizer 子目录的模型，并同步新增 pipeline、默认 deploy 或架构注册。
- 强制：闭合 checkpoint `model_type`、pipeline registry key、`model_arch`、`default_deploy_config_name` 与 registry architecture；每个 stage 明确 `model_subdir`/`tokenizer_subdir` 和实际权重根目录，最终 stage config 保留 CFG 等 engine gate 所需声明。
- 禁止：只依赖路径 basename、文件存在或 `trust_remote_code` 推断 pipeline；让 root architecture 覆盖 stage architecture；让单卡与多卡 profile 复制不同 topology 或行为合同。
- 验收：用 bare root config、无显式 deploy 的 startup 和 structured/legacy 路径断言 pipeline、默认 deploy、stage 子目录、architecture 与 sampling-extra key 均进入最终逐 stage config；单卡与双卡配置除设备 placement 外保持一致。 ^[PR #6186]

## CONF-5i — pipeline 专属 alias 与 hook 必须在通用配置层声明、归一并消费

- 触发：pipeline 需要把全局 CLI 字段改写到特定 stage，或声明 stage-0 prompt transform、按 task 解析 checkpoint 的 hook、显式 inline diffusion。
- 强制：alias 由 `PipelineConfig.stage_cli_aliases` 唯一声明并在 structured/legacy factory 进入 stage merge 前归一；canonical `stage_<id>_<target>` 与 alias 同时出现时，canonical 值优先并告警，alias 出现在其他 stage 必须拒绝。`prompt_transform_func`、`model_path_resolver` 与 `inline_diffusion` 由 `StagePipelineConfig` 投影到唯一 consumer；resolver 必须在构造 backend engine args 前消费并移除。所有 inline path 都要求 `plan.num_replicas == 1`；whole pipeline 只有一个 stage 时默认 inline，多 stage 只允许 `inline_diffusion=true` 或已有 `custom_pipeline_args` in-process owner opt in，其余均 subprocess。
- 禁止：在 CLI、factory 或 stage startup 为模型名硬编码 alias/resolver；把 alias 广播到所有 stage；因 diffusion stage 只有一个 replica 就隐式改变所有多 stage pipeline 的进程隔离。
- 验收：structured/legacy 两路覆盖 alias-only、canonical-only、相同/冲突值、错误 stage 与 unset control；用 root/partition/task path 证明 resolver 到达 stage model/tokenizer consumer，并用 single-stage、multi-stage opt-in/control 与 multi-replica veto 证明 exact inline predicate。PR #6813 只新增 default one/two-stage 参数化 test，其他组合仍须补验收。^[PR #5885] ^[PR #6813]

## CONF-5j — PipelineConfig 在构造时必须验证终端 topology

- 触发：修改 `PipelineConfig.__post_init__`、`get_validation_errors()`、静态 `OMNI_PIPELINES` registry，或新增/调整
  multi-stage pipeline 的 `final_output` 声明。
- 强制：`__post_init__` 必须调用 `get_validation_errors()`，有 error 时以 formatted `ValueError`
  eager fail；保留 nonempty stages、unique IDs、valid nonself inputs、entry 与任意位置 terminal
  `final_output=True`。静态 registry 在构造时通过 guard；resolver callable 仅在 concrete object
  materialize 时受 guard。
- 禁止：保留 public `validate()` API 或 registry posthoc sweep；从最后 stage、stage 数、entry/input、
  `final_output_type` 推断 terminal；不得声称 resolver 后有独立 revalidation、cycle/runtime/E2E proof。
- 验收：无 stages/terminal、duplicate ID、invalid/self input eager error；terminal-not-last 与有效 fixture
  pass；static configs pass，resolver concrete result materialization guarded。^[PR #6291] ^[PR #6829]

## CONF-5k — `--stage-overrides` parser 只验证 shape，字段语义留给下游 owner

- 触发：修改 `parse_stage_overrides`、`--stage-overrides` CLI 输入、stage-scoped
  override forwarding，或考虑在 parser 对字段名、值类型、范围或 stage existence 做校验。
- 强制：对 falsey raw input（包括 `None`、`False`、`0`、空 string/容器）在任何验证前返回
  `None`；truthy string 先 JSON parse，再要求 top-level 为 dict、每个 key 为 ASCII
  `isdigit()` string、每个 value 为 dict。非空 outer dict 中的空 inner dict 合法；parse 后的
  空 dict 返回 `None`。保留所有 inner fields 原样，不在 parser 建 allowlist，也不在此层做
  field type/range 或 stage-id existence 校验。
- 禁止：把 `[]`、`0`、`false` 等 truthy JSON string 的 parsed scalar/container 当作
  “无 override”接受；用只含 deploy schema 的 allowlist 拒绝 `extras` 或
  `kv_cache_dtype`、`seed`、`swap_space`、`block_size`、`stage_connector_spec` 等
  stage-scoped engine args；用 `str.isdigit()` 而不加 ASCII guard，导致 fullwidth digit
  绕过并在下游错误路由。
- 验收：覆盖 falsey raw passthrough、invalid JSON、truthy JSON list/scalar/bool rejection、
  non-ASCII/non-string/negative/float stage key rejection、non-dict inner value rejection，
  以及 `{"0": {}, "1": {}}` pass-through。再证明 `extras` 到 default-diffusion fallback/
  registered stage merge 的既有 owner、surviving `stage_<id>_<key>` 到 `cli_overrides` 的
  forwarding 均未被 parser 截断；未知 `OmniEngineArgs` field 仅在
  `filter_dataclass_kwargs` 被 drop 并留下命名该 field 的 WARNING。^[PR #6230]

## CONF-5l — LLM `additional_config` 必须经 runtime owner 投影，且不侵占 diffusion owner

- 触发：deploy/`engine_extras`/CLI 为 stage 传入 `additional_config`，或修改 `_RUNTIME_ENGINE_FIELDS`、`OmniStageRuntimeConfig`、`build_engine_args_dict_from_omni_stage_config`。
- 强制：LLM/AR stage 把 `additional_config` 纳入 runtime typed owner，深拷贝投影到最终 engine args；diffusion stage 继续只由 diffusion config projection 拥有该字段，runtime 侧保持 `None`。未知字段校验与跨 stage 隔离不变。
- 禁止：因 diffusion 已有同名字段就拒绝 LLM stage；让 LLM 与 diffusion 共享同一 mutable dict；在 projection 静默丢弃 NPU Code2Wav 等已声明的 LLM `additional_config`。
- 验收：LLM stage round-trip 非默认值到 engine args 且 mutation 不回写 source/邻 stage；MiniCPM-o NPU profile 读回 graph keys；diffusion stage 断言值只在 `diffusion_config.additional_config`。^[PR #7272]


## CONF-5m — stage runner override 必须与 native session 和平台门禁一起解析

- 触发：修改 `StageDeployConfig.model_runner`、runner resolution、平台overlay或 mixed V1/MRv2 pipeline。
- 强制：stage的`v1|v2`覆盖deploy默认，legacy/structured两路把同一结果投影到`use_v2_model_runner`；native MRv2、存在upstream input的stage只允许`session_mode=turn`。平台V1 fallback清除既有stage V2 opt-in，完成platform stage overlay后再校验最终值；NPU/XPU最终含任一V2仍须拒绝。
- 禁止：只按pipeline默认选择runner；通过free-form engine args绕过structured owner；让native下游MRv2接受streaming/duplex；在平台overlay前校验一次后漏掉stage override。
- 验收：stage/deploy precedence、invalid runner、native turn/control与非turn拒绝、V1平台fallback及overlay后NPU/XPU拒绝都覆盖legacy/structured结果。^[PR #8184]
