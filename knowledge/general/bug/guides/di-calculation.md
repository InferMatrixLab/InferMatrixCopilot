---
title: "DI 计算说明"
created: 2026-08-07
updated: 2026-11-09
type: guide
tags: [general, bug]
sources: ["PR #444 review 2026-11-09"]
---

# DI 计算说明

按 Bug 优先级给出基础 DI（Defect Intensity）与 SLO，并说明偶现降级与 SLO 升级的计算方式。
现行口径不按未关闭天数累加：issue 达到 SLO 后严重度升一级，DI 取升级后级别的基础值。
相关入口：[bug 主题](../_index.md)、[调试](../../debug/_index.md)、[测试与 CI](../../ci/_index.md)。

## Bug 分级标准

| Bug 优先级分类 | 划分标准 | DI 值 | SLO |
|---|---|---|---|
| critical | 数据损坏或结果错误、服务不可用、安全或凭据问题、静默路由错误 | 10 | 1 天 |
| high priority | 默认分支 CI 失败、主路径功能不可用、明确的精度或性能回退 | 3 | 5 天 |
| medium priority | 功能受损但存在可用绕过、非主路径报错 | 1 | 10 天 |
| low priority | 体验、日志、文档和提示类问题 | 0.1 | 14 天 |
| invalid | 非问题 | 0 | / |

定级由 issue 负责人按上表判定；同一个问题同时满足多行时取最高一级，并以仓库实际使用的
severity label 或发布记录留痕。

## 其他规则

1. **偶现降级**：偶现问题在初始级别上降一级。
2. **SLO 升级**：DI 不按天数累加；issue 未关闭时间达到 SLO 后严重度升一级，DI 取升级后级别
   的基础值。重复升级的粒度以当期发布说明为准，最高不超过 critical。
3. **invalid 恒为 0**：不参与剩余 DI 汇总。

### 升级示例

medium 级 issue（基础 DI = 1，SLO = 10 天）在第 10 天仍未关闭时升为 high，DI 取 3；若继续
跨过 high 的 SLO，则再升为 critical，DI 取 10。

通式：`DI = 基础 DI(当前级别)`，`当前级别 = 初始级别 + 已跨过的 SLO 次数`（上限 critical，
`invalid` 始终为 0）。
