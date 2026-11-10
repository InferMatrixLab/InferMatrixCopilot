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

只有 `ROUTER-PRI-数字` 是可审计规则 ID。特性例子和 issue 链接不单独编号。

相关入口：[bug 主题](_index.md)、[仓库入口](../_index.md)。

## ROUTER-PRI-0 — 先看影响，再看特性

- 触发：为 `vllm-project/router` 的 issue 定级或打标。
- 必须：先看影响，再看特性。致命和高按影响判定；中和低按特性给默认档。主路径不可用或大面积 CI 失败时，特性再低也要升档。
- 禁止：主路径不可用或大面积 CI 失败时，按特性默认档留下。
- 验收：定级说明先给出影响判断；只有影响未达到致命或高时，才使用特性默认档。

## ROUTER-PRI-1 — 致命

- 触发：为 `vllm-project/router` 的 issue 定级，并判断调度是否直接不可用，或一次失败是否打掉大量 CI 用例。
- 必须：出现「直接无法调度」时标致命。
- 必须：出现「一次导致大量 CI 用例失败（不是单个 flaky job）」时标致命。
- 禁止：把单个 flaky job 标成致命。
- 验收：命中上列任一条必须项时，优先级为致命。

## ROUTER-PRI-2 — 高

- 触发：为 `vllm-project/router` 的 issue 定级，且未达到致命，或单子带有 `ci-failure`。
- 必须：`ci-failure` 默认归这一档。
- 必须：主路径还能跑，但结果会错：错路由、取消/泄漏、主干回归、安全或数据错误。
- 必须：已标为 `ci-failure` 的单子，只有确认是「大面积用例失败」或「调度直接不可用」时，才升到致命。
- 禁止：未确认「大面积用例失败」或「调度直接不可用」时，把 `ci-failure` 升到致命。
- 禁止：把主路径结果错误、安全或数据错误、主干回归降到中或低。
- 验收：未确认「大面积用例失败」或「调度直接不可用」时，`ci-failure` 与主路径结果错误的优先级为高。确认是这两类情况后，优先级为致命。

## ROUTER-PRI-3 — 中（默认特性）

- 触发：影响没有达到致命或高，需要按特性给默认档。
- 必须：下列特性默认标为中。
- 禁止：把 P/D 与 Program 调度算进「其他调度」。
- 验收：未因影响升档时，命中下列特性的 issue 优先级为中。

- KV-aware / prefix / affinity 调度，包括 `kv_aware_affinity`、`cache_aware`、prefix reuse。
- P/D 与 Program 调度（这是另一条主线，不要算进「其他调度」）。
- Worker 发现与注册（对不上则亲和策略不会生效，例如 https://github.com/vllm-project/router/issues/468 、https://github.com/vllm-project/router/issues/299）。
- 取消、泄漏、drain、健康检查误清计数（会让 KV 亲和被绕开，例如 https://github.com/vllm-project/router/issues/464 、https://github.com/vllm-project/router/issues/453）。
- 主路径协议和鉴权转发：OpenAI/gRPC 字段丢失、auth header、tool-calling 直接 503。
- 有临时规避、或只打特定后端/配置的上述缺陷，以及这些方向里已达成共识、缺实现的 enhancement。

## ROUTER-PRI-4 — 低（默认特性）

- 触发：影响没有达到致命或高，且特性属于下列低档列表。
- 必须：下列特性默认标为低。
- 禁止：把 CI failure 本身算进「CI 加速」而标低。
- 验收：未因影响升档时，命中下列特性的 issue 优先级为低。

- 其他调度策略：least-load、round-robin、`power_of_two`、`consistent_hash`、topology、QoS/token-cost、heterogeneous stage-graph。
- 可选生态集成：Mooncake、DeepInfer、Consul、LMCache、Anthropic `/v1/messages`。
- 容量仿真、多租户配额、离线 search、长期 HA。
- 可观测性增强、文档和配置体验、CI 加速（不是 failure 本身）、多模态远期架构。
- 还在讨论、尚未排期或没人认领的 RFC / Feature。

## ROUTER-PRI-5 — 判定顺序

- 触发：同一条 issue 同时沾到影响条件和特性默认档。
- 必须：按下列顺序判定。特性只决定中、低的默认值；满足第 1 步或第 2 步时必须升档。
- 禁止：满足第 1 步或第 2 步时，因为特性默认档是中或低而不升档。
- 验收：`power_of_two` 若导致完全无法调度，优先级为致命，而不是低。

1. 是否直接无法调度，或一次挂掉大量 CI 用例 → 致命。
2. 是否为 `ci-failure`，或主路径结果错误、安全/数据错误、主干回归 → 高。
3. 其余按上面的特性默认到中或低。
4. 特性只决定中、低的默认值；满足 1 或 2 时必须升档，例如 `power_of_two` 若导致完全无法调度，仍标致命，不会因为属于「其他调度」而标低。
