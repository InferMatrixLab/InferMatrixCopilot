# 文档地图

一行一条，按你要做的事挑一篇。**这份索引是唯一的目录**——每篇文档只负责自己那
一类事实，别处只链接不复述（归属规则见
[`PLAN-doc-refactor.md`](PLAN-doc-refactor.md) §4）。

## 从这里开始

| 你要做什么 | 看这篇 |
|---|---|
| 先搞清楚这东西是什么、有哪些能力 | [`GUIDE.md`](GUIDE.md) —— 总入口：概览 / 功能 / 使用 / 开发 / playbook / step / tool / 性能 |
| 装上并开始用 | [`GUIDE.md §3`](GUIDE.md#3-使用指南操作者) |
| 改这个仓库的代码 | [`GUIDE.md §4`](GUIDE.md#4-开发指南维护者) → 再看下面的 architecture |

## guide/ —— 使用者

| 文档 | 内容 |
|---|---|
| [`guide/backends.md`](guide/backends.md) | **后端**：五个 provider 怎么选、怎么配、权限如何保持（Strict） |
| [`guide/knowledge-learning.md`](guide/knowledge-learning.md) | 任务完成时提炼正负经验、自动知识 draft PR 与实际使用质量评分 |
| [`guide/hosts/`](guide/hosts/README.md) | **宿主端**：在 Codex / Claude Code / Cursor 里使用 copilot（Direct） |
| [`guide/mcp.md`](guide/mcp.md) | MCP 安装与七个工具接口 |
| [`guide/self-evolution.md`](guide/self-evolution.md) | 用自进化引擎改进评审、知识摄取与引擎自身 |
| [`guide/metrics.md`](guide/metrics.md) | 每次运行的 `metrics.json` 消费契约（看板/统计用） |
| [`guide/autonomous-workflow.md`](guide/autonomous-workflow.md) | 独立执行器（默认不注册） |
| [`guide/knowledge-maintainer.md`](knowledge/maintainer-walkthrough.md) | 知识维护实操 |
| [`guide/samples/`](knowledge/samples) | 三个知识贡献示例 |

> **宿主 ≠ 后端。** `claude-code` / `codex` / `cursor` 三个名字两边都出现，方向
> 相反：**宿主**提供模型给你用（Direct），**后端**是 copilot 拿去用的模型
> （Strict）。对照表见 [`guide/hosts/README.md`](guide/hosts/README.md)。

## features/ —— 特性（做什么 / 怎么开关 / 实测如何）

| 文档 | 状态 |
|---|---|
| [`features/provider-registry.md`](features/provider-registry.md) | 已实现——五种后端的统一注册表 |
| [`features/strict-review-deep-engine.md`](features/strict-review-deep-engine.md) | 已实现（默认开） |
| [`features/review-recall.md`](features/review-recall.md) | 已实现（默认开）——v14/v15 召回攻坚 |
| [`features/auto-run.md`](features/auto-run.md) | **draft，未实现**——GitHub 事件触发 |
| [`RFC-knowledge-intake.md`](RFC-knowledge-intake.md) | 已实现（默认关，shadow 起步）——Copilot typed SDK 唯一拥有知识规则，ReviewBot 只编排 evidence/model/Git/fork PR，人审合并即晋升 |
| [`RFC-pr-state-machine.md`](RFC-pr-state-machine.md) | **proposed，未实现**——PR 单一状态机（10 态 + closed）、04:00 夜间自动评审、ready 门槛只在“几乎可合入”时呼叫维护者、7/14/30 天 stale→draft→close；两项越过 #116 边界需仓库 owner 签字 |

## architecture/ —— 维护者

| 文档 | 内容 |
|---|---|
| [`architecture/CODE_TOUR.md`](architecture/CODE_TOUR.md) | 按**数据流**讲每个源文件在哪一环 |
| [`architecture/DESIGN.md`](architecture/DESIGN.md) | **为什么**这么设计（含被否决的选项） |
| [`architecture/KNOWLEDGE.md`](architecture/KNOWLEDGE.md) | 知识来源与同步边界 |
| [`architecture/SPEC/`](architecture/SPEC/README.md) | 逐文件契约：这个文件**不能破坏什么** |

## contributing/ —— 参与进来

| 文档 | 内容 |
|---|---|
| [`contributing/DOCSTRING_STYLE.md`](contributing/DOCSTRING_STYLE.md) | docstring 约定 |
| [`knowledge/writing.md`](knowledge/writing.md) | **怎么写和更新知识库**——落盘位置、页面类型、copilot 怎么消费、两道门禁 |
| [`contributing/release-maintenance.md`](contributing/release-maintenance.md) | vLLM-Omni 发版漂移审计（`imupdate` 底层） |
| [`knowledge/templates/`](knowledge/templates/README.md) | 可直接复制的知识页模板（7 种页型，各自对照一篇 vLLM-Omni 实页） |

## evaluation/ —— 测量（冻结记录）

索引与当前结论：[`evaluation/README.md`](evaluation/README.md)。
数据与脚本在顶层 [`eval/`](../eval/README.md)；这里只放叙述性报告。

| 文档 | 结论 |
|---|---|
| [`evaluation/EVAL-v14-v16-recall-campaign.md`](evaluation/EVAL-v14-v16-recall-campaign.md) | **最新**——Δrecall −.049，precision 打平，成本约 1/3 |
| [`evaluation/EVAL-goal-strict-vs-opus5.md`](evaluation/EVAL-goal-strict-vs-opus5.md) | v7→v13：combined 15—15 |
| [`evaluation/EVAL-goal-report.md`](evaluation/EVAL-goal-report.md) | 早期一键 NL copilot 对比 |
| [`evaluation/EVAL-PR20-report.md`](evaluation/EVAL-PR20-report.md) | 20 例 PR 评审全量重跑 |
| [`evaluation/RESEARCH-reference-agents.md`](evaluation/RESEARCH-reference-agents.md) | 参考 agent 机制调研 |

## archive/ —— 冻结，不再维护

读它们要知道：**内容停在各自的日期上，不代表现状**。

| 文档 | 为什么留着 |
|---|---|
| [`archive/IMPLEMENTATION_STATUS.md`](archive/IMPLEMENTATION_STATUS.md) | 设计 15 项任务的历史交付记录；"现状"职能已由 `GUIDE.md` 承担 |
| [`archive/PLAN-knowledge-reorg.md`](archive/PLAN-knowledge-reorg.md) | 知识重组的历史迁移计划，仍是溯源规则的出处 |
| [`archive/PLAN-mcp-plugin-and-community-merge.md`](archive/PLAN-mcp-plugin-and-community-merge.md) | PROPOSED rev 6，从未执行 |
| [`archive/reorg-audit/`](archive/reorg-audit/) | 知识重组的**证据基线**：`knowledge/SCHEMA.md` 与 rebase workflow 页引用它做溯源 |

## 文档自身的闸门

三个校验器，都在 CI 里跑：

```bash
python tools/check_doc_links.py        # 所有 .md 相对链接可解析
python tools/check_doc_citations.py    # 代码里引用的 doc/ 路径都存在
python tools/check_spec_freshness.py   # SPEC 页有 verified-against 且不落后于源码
```

进行中的重构：[`PLAN-doc-refactor.md`](PLAN-doc-refactor.md)
（P0–P2 完成；P3 features / P4 architecture / P5 SPEC 62 页 / P6 根目录收口待做）。
