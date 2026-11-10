---
title: "vLLM Router issue 分级规则"
created: 2026-11-10
updated: 2026-11-10
type: rule
tags: [vllm-router, bug]
sources: []
---

# vllm-project/router issue 分级规则

适用范围：`vllm-project/router` 的 issue 分诊与打标。可直接当作标签规则使用。

## 总原则

先看影响，再看特性。致命和高按影响判定；中和低按特性给默认档。主路径不可用或大面积 CI 失败时，特性再低也要升档。

## 致命

- 直接无法调度。
- 一次导致大量 CI 用例失败（不是单个 flaky job）。

## 高

- `ci-failure` 默认归这一档。
- 主路径还能跑，但结果会错：错路由、取消/泄漏、主干回归、安全或数据错误。
- 已标为 `ci-failure` 的单子，只有确认是「大面积用例失败」或「调度直接不可用」时，才升到致命。

## 中（默认特性）

- KV-aware / prefix / affinity 调度，包括 `kv_aware_affinity`、`cache_aware`、prefix reuse。
- P/D 与 Program 调度（这是另一条主线，不要算进「其他调度」）。
- Worker 发现与注册（对不上则亲和策略不会生效，例如 https://github.com/vllm-project/router/issues/468 、https://github.com/vllm-project/router/issues/299）。
- 取消、泄漏、drain、健康检查误清计数（会让 KV 亲和被绕开，例如 https://github.com/vllm-project/router/issues/464 、https://github.com/vllm-project/router/issues/453）。
- 主路径协议和鉴权转发：OpenAI/gRPC 字段丢失、auth header、tool-calling 直接 503。
- 有临时规避、或只打特定后端/配置的上述缺陷，以及这些方向里已达成共识、缺实现的 enhancement。

## 低（默认特性）

- 其他调度策略：least-load、round-robin、`power_of_two`、`consistent_hash`、topology、QoS/token-cost、heterogeneous stage-graph。
- 可选生态集成：Mooncake、DeepInfer、Consul、LMCache、Anthropic `/v1/messages`。
- 容量仿真、多租户配额、离线 search、长期 HA。
- 可观测性增强、文档和配置体验、CI 加速（不是 failure 本身）、多模态远期架构。
- 还在讨论、尚未排期或没人认领的 RFC / Feature。

## 判定顺序

1. 是否直接无法调度，或一次挂掉大量 CI 用例 → 致命。
2. 是否为 `ci-failure`，或主路径结果错误、安全/数据错误、主干回归 → 高。
3. 其余按上面的特性默认到中或低。
4. 特性只决定中、低的默认值；满足 1 或 2 时必须升档，例如 `power_of_two` 若导致完全无法调度，仍标致命，不会因为属于「其他调度」而标低。
