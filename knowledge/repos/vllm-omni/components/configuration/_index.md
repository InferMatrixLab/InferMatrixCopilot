---
title: "vLLM-Omni Configuration"
created: 2026-07-16
updated: 2026-10-09
type: index
tags: [vllm-omni, components, config]
sources: ["claude-workflow-starter-private@296ea45", vllm_omni/config/]
---

# vLLM-Omni Configuration

- 主要源码：`vllm_omni/config/`
- 跨边界入口：`vllm_omni/diffusion/data.py`、`vllm_omni/engine/arg_utils.py`、`vllm_omni/engine/stage_init_utils.py`、`vllm_omni/engine/async_omni_engine.py`
- 主要测试：`tests/config/`、`tests/config/test_config_factory.py`、
  `tests/diffusion/test_diffusion_config_propagation.py`，以及各公开入口附近的配置测试
- 部署配置：`vllm_omni/deploy/*.yaml`，以及 `pipeline_registry.py`、
  `endpoint_policy.py`、`server_settings.py`、`yaml_util.py`、`composable_parallel/`
- 源码校验：以上路径在 `main @ 44d3ae10`（upstream `main` 快照）验证存在；机器基线见
  `adapters/vllm_omni/release_baseline.yaml`

## 什么时候查这里

- 修改 deploy、pipeline、stage、CLI、默认 factory 或 direct kwargs 的配置构造。
- 调查字段来源优先级、alias、flat→nested、strict unknown-field 校验、shared/stage/diffusion owner 分区。
- 核对 structured、legacy startup、默认 single-stage factory 和 direct factory 的语义一致性。

## 不放什么

- HTTP 请求字段和 serving 层限制；这些属于 [Serving](../serving/_index.md)。
- 最终 stage config 之后的并行与设备启动；这些属于 [Model Executor](../model-executor/_index.md)。
- 某个模型独有的配置或 checkpoint 语义；这些放模型目录。

## 目录内容

| 遇到什么 | 查看哪里 |
|---|---|
| API speech_cache budgets、继承与 process-local singleton | [speech cache](rules-speech-cache.md) |
| 理解配置从 deploy、CLI、默认 factory 到 structured/legacy config 的稳定边界 | [配置构造架构](architecture.md) |
| 根据 PR 描述直达 strict schema、pipeline sampling constraints、deploy/topology、composable strategy、global engine args 的 owner 校验、CLI-only 负向 alias 或显存配置的规则组与第一批源码、环境变量 inventory | [配置开发门禁与代码地图](rules.md)   新增核对：CONF-3e。 |

| 对象存储 URI、HF cache snapshot 或空 `config.json` 的模型名称回退与 pipeline 路由 | [模型引用路由规则](rules-model-reference-routing.md) |
| stage full-payload transport capability、topology-owned projection、async-chunk 自动模式或 deploy/CLI override rejection | [stage transport capability](rules-stage-transport.md) |
| diffusion Ulysses SymmMem transport flag 的 deploy/CLI/default-stage projection | [diffusion parallel transport rules](rules-diffusion-parallel-transport.md) |
| diffusion attention shorthand、structured config 与 global override 的优先级和最终规范表示 | [diffusion attention precedence](rules-diffusion-attention-precedence.md) |
| legacy stage engine args 的 copy boundary、connector/default injection、repeated build mutation、global engine args 的 owner 校验或 CLI-only 负向 alias | [legacy engine-args isolation](rules-legacy-engine-args.md)   新增核对：VOMNI-CFG-1u。 |
| 审计配置来源和多层加工 | [config audit](config-audit-plain-language.md) |
| 设计配置归一化与 parity 验证 | [config normalization parity](config-normalization-parity.md) |
| 查询 `Omni()` 初始化参数 | [Omni init args](omni-init-args.md) |
| 写或修改 deploy YAML | [deploy YAML](deploy-yaml.md) |
| 核对 pipeline/deploy schema | [pipeline deploy schema](pipeline-deploy-schema.md) |
| 审查或新增 process 环境变量、stage `env` 和安全诊断输出 | [environment-variable contract](environment-variable-contract.md) |
| 计算多 stage 显存预算 | [deployment memory budget](deployment-memory-budget.md) |
| 添加新模型和注册点 | [adding a model](adding-a-model.md) |
| composable strategy、并行拓扑、VAE batch mode 的 WORLD→DP 门禁与 load balancing owner | [并行拓扑合同](rules-parallel-topology.md) |

| 冻结 topology、辅助 stage、mixed V1/MRv2 runner、native capability 缺失警告、deploy profile 与 `additional_config` 投影 | [topology 与部署 profile](rules-topology-profiles.md) |

- [部署 platform overlay 的最终配置合同](rules-platform-overlays.md)。
- [配置规则 — CUDA graph 显存预留与 KV 预算](rules-cudagraph-kv-reserve.md)
