---
title: "Serving"
created: 2026-07-10
updated: 2026-10-09
type: index
tags: [vllm-omni, components, serving]
sources: []
---

# Serving

- Duplex 公共客户端：`vllm_omni/clients/`；HTTP/WS 实现位于 `vllm_omni/entrypoints/duplex/`，模型 adapter 留在各模型的 `duplex/` 子目录。
- 主要源码入口：`vllm_omni/entrypoints/`（cli、openai、openpi 及 omni/async_omni 入口）和 `vllm_omni/engine/`（orchestrator、stage engine core、stage pool/runtime、output processor）
- 源码校验：以上路径均已在 `main @ 44d3ae10`（upstream `main` 快照）验证存在
- 主要职责：用户入口、请求解析、在线服务和 engine 边界

## 什么时候查这里

- CLI、HTTP、OpenAI-compatible API 或 offline/online 请求行为不一致。
- 参数在入口处丢失、默认值改变，或请求没有进入预期 engine 路径。

## 不放什么

- 模型内部 attention、checkpoint 或 diffusion 算法问题。
- 通用 API 设计方法；这些放 `general/review/`。

## 目录内容

| 遇到什么 | 查看哪里 |
|---|---|
| 理解入口到 engine 的边界，选择 unified HTTP/WS endpoint、区分 wire protocol，或确认 PersonaPlex/JoyVL 是否是独立 server | [architecture 与 API route owner](architecture.md) |
| 根据 PR 描述直达 request contract、stage sampling constraints、streaming format、artifact readiness、factory lifecycle 或 metrics 的规则组与第一批源码 | [rules 与代码地图](rules.md) |
| Prometheus family、pipeline waiting/running gauge、stage/replica snapshot 或 collector lifecycle | [metrics 生命周期规则](rules-metrics.md)   新增核对：SERV-2g。 |
| upstream launcher shutdown、renderer warmup、多 replica compile-cache 隔离与parsed CLI logging初始化 | [upstream 兼容规则](rules-upstream-compat.md)   新增核对：SERV-7f、SERV-7e、SERV-7g、SERV-7h。 |
| stage/replica 死亡、request-local failure、readiness/liveness 或驱逐后 cleanup | [fault-isolation rules](rules-fault-isolation.md)   新增核对：SERV-5u。 |
| batched chat frontend fan-out、choice cardinality、whole-batch error/cancellation | [batch chat rules](rules-batch-chat.md) |
| engine startup/shutdown、stage lifecycle、acknowledged abort 的 shutdown race、duplex stage prewarm/CFG companion，或 opt-in event-driven orchestration / reader-poller reconcile / final-output drain | [engine 生命周期规则](rules-engine-lifecycle.md) |
| duplex stage-keyed segment state、跨 stage boundary/metadata 隔离与model harness共享有界output buffer | [duplex 编排规则](rules-duplex-orchestration.md) |
| adapter-owned full-duplex session、deferred cognition/gate、barge-in cancellation、response ownership、`to_thread` worker drain、close/flush/session cleanup，或 startup warmup/realtime admission | [session lifecycle 规则](rules-session-lifecycle.md)   新增核对：SERV-6j。 |
| acknowledged abort 的 RPC ACK、queue/router shutdown race、cancellation cleanup 或 backpressure/timeout 保持 | [acknowledged abort 生命周期规则](rules-abort-lifecycle.md) |
| assembled FastAPI route 覆盖、method/path ownership 或 request-body forwarding | [app assembly 规则](rules-app-assembly.md) |
| stage config→EngineArgs projection、explicit `devices`、TP/local-DP/PP、replica layout 或 worker 创建前的 layout guard | [stage 启动与设备布局规则](rules-stage-startup.md)   新增核对：SERV-12c、SERV-8b。 |
| 公开请求字段的校验、来源冲突、alias/extras 归一与 consumer view | [请求输入合同](rules-request-input.md)   新增核对：SERV-4r、SERV-4r2、SERV-4r3、SERV-4r4、SERV-4s、SERV-4r5、SERV-4i2。 |
| speech 输出采样率 capability、resample、streaming header/flush、内部失败 500 与既有异常传播 | [speech 输出规则](rules-speech-output.md) — SERV-9c、SERV-9d。 |
| OpenAI Realtime routing/capability admission、chat preprocessing、cache-safe preflight、取消后 prompt、PCM16/resample、manual 会话与 transcript truncate | [Realtime 请求与会话规则](rules-realtime-openai.md) — SERV-RT-1a、SERV-RT-1f、SERV-RT-1g、SERV-RT-1h、SERV-RT-1i。 |

- [Realtime video 增量 prefill 规则](rules-video-stream-prefill.md) — warmup/abort、窗口身份与已解码帧 snapshot。

- [Sleep/wake ACK 与 admission](rules-sleep-control.md) — worker/RPC failure、partial-stage recovery 与 stage-ID 预校验。

- [effective video config 与 borrowed RGB eligibility](rules-video-encoding.md)。

- [duplex bounded delivery、cancel guard 与creation payload大小](rules-duplex-output-delivery.md)。

- [voice删除403/404与typedexceptions](rules-voice-management.md)。

- [sleep/wakeup非空stage_ids与nonnegativelevel](rules-control-request-schema.md)。

- [Realtime model-context 自动截断合同](rules-realtime-truncation.md)。

- [多模态缓存身份与存储合同](rules-multimodal-cache.md) — replica UUID 隔离、waveform buffer 所有权与 array/list 复用边界。
