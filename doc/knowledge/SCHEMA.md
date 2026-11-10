# SCHEMA — 页面元数据与生命周期

规定 `general/` 与 `repos/` 下**沉淀层**页面的 YAML frontmatter、标签分类法和生命周期
规则（LLMWiki 机制）。目录归属与页面写法仍以 [贡献规范](contributing/_index.md)
为准；本文件只补充元数据机制，不重复目录规范。`repos/jianghan-roleplay-data-pipeline/`
整棵子树暂不适用（保持原样）。

## 分层

- **证据层（默认不新增）**：现存 `incidents/`、`history/`、`results/` 是旧树留下的
  材料。PR/review 学习**一律不写**这一层：新的 raw diff、评论、thread、回放和 case 只在
  整理临时目录存在，规则验收后必须删除。非 PR 事故也默认不新增，只有同时满足
  [复盘与规则摄取](contributing/incidents.md#incident-准入门禁) 的三项准入门禁才创建
  `incidents/` 页面；`history/`、`results/` 不再新增。
- **沉淀层**：`rules.md`、`guides/`、`architecture.md`、`overview.md`、`_index.md`
  等 —— 携带 frontmatter，结论通过 `sources:` 引用外部 PR/代码或既有历史页。
- **PR/review 学习产物**：只允许写最近 owner 的 `rules.md` 与必要 `_index.md`；用
  `sources:` 和段尾 PR 引用提供溯源，不保存原始证据副本。

## Frontmatter（沉淀层必填）

知识不限于规则。稳定职责、边界、数据流与 API 契约使用 `type: architecture`；配置解释、
设计取舍、功能关系和验证方法按最近 owner 使用 architecture 或 `type: guide`。只有明确
必须执行的约束才进入 rule 页。源码与文档初始化可形成这些解释性知识；PR/review 学习
继续遵循只提炼 owner 规则的摄取契约。

`kb init --stage knowledge` 分别报告 architecture、api、configuration、tradeoffs、features、
validation 六个维度。它生成的非规则段使用 `kb:knowledge` HTML 注释标明 owner、facet 和
完整上游 pin；正文提供固定源码链接，页面 `sources:` 记录出处。注释只用于识别已生成的
维度，不能把页面存在、路由命中或某条规则存在解释为全部行为已覆盖。设计推断在正文中
明确标出，缺少证据的维度保留为缺口；已有另一 pin 的段落需人工更新，不追加一份矛盾正文。

完整功能与生产文件覆盖由 adapter 的 `knowledge-coverage.yaml` 明确定义。`kb:file` 段包含
路径、完整 pin 和源码文本哈希，正文是可复核的静态接口/依赖记录；它不是原始代码副本，
不产生规则，也不承诺整文件行为或测试覆盖。功能页和源码记录按最近代码 owner 组织；
达标报告属于 `eval/`，不能作为产品知识正文保存或通过新增索引增加覆盖率。

`kb deepen`（`knowledge-deepen`）的 `kb:depth` 标记另绑定 feature、七维 facet、完整 pin
与正文哈希，`kb:depth-proof` 保存精确证据区间及其内容哈希、调用 trace。只用于复核代表性
实现知识；旧标记、静态接口卡、规则和索引不计入此深度。正文或证据改变、pin 不符或重复
facet 均失效。页面仍使用既有 architecture/guide 类型，设计推断与未验证测试明确标出。

深读 proof 的可选 `basis` 缺省为 `supported`，旧区块原文和哈希无需迁移。
`verified_absent` 当前仅用于已核验的验证入口缺失：由版本化检查器按 feature policy
推导完整范围，并绑定固定 pin、范围和文件内容摘要；读取、解析、动态关联或范围不完整
均保留未知。缺失正文必须限定范围，不能把缺失说成测试通过。缺失区块同样要求三个
独立认可维度均为 yes。认可指标合计 supported 与 verified_absent；原正向指标保留，
分别列出正向知识、已核验缺失和 unknown，不删除分母。

功能解释页可声明 `feature`（功能 ID）、`entry_points`（生产入口路径列表）和
`source_globs`（所属源码 glob 列表）。`knowledge`/`knowledge-deepen` 从同一份已审阅
coverage policy 生成这些匹配提示，刷新时保留正文、来源和深读证明。提示只选择审查背景，
不增加文件或行为覆盖率；旧页仍可通过 `sources` 中的固定源码路径匹配。
Direct 的 `related_knowledge` 与 Agent 的 `doc_related` 共用确定性检索，最多提供两页、
共 6,000 字符，附来源 pin、已有/缺失 facet 和截断状态；它不把推断变成硬规则。
检索另返回 facet basis、已核验缺失和未注入维度；可用知识不等于已注入全部正文。

`check_wiki_lint.py` 强制的只有前五项（`title`、`created`、`updated`、`type`、
非空 `tags`）；其余为约定字段，写了就必须合法（`confidence` 只能取三值）。

```yaml
---
title: 页面标题
created: YYYY-MM-DD      # 上游首次提交日期（来源 doc/archive/reorg-audit/baseline/dates.tsv）
updated: YYYY-MM-DD      # 最近实质更新日期
type: rule | guide | architecture | index
tags: [来自下方分类法]
sources: []              # 约定字段：incidents/... 相对路径、PR 号、file:line、上游源码路径
confidence: high | medium | low   # 可选：结论的证据强度（单一来源/有争议时标注）
contested: true                   # 可选：存在未解决矛盾时
contradictions: [相对路径]         # 可选：与本页冲突的页面
---
```

`repos/vllm-omni/` 的 `sources:` 另有机器用途：以 `vllm_omni/`、`tests/`、
`benchmarks/`、`docs/`、`.buildkite/` 开头的条目会被发版审计与上游文件列表对账，
上游删除或重命名后报 `stale_knowledge_source`。见
[同步与校验 §发版审计](contributing/validation.md#vllm-omni-页面还有一道发版审计)。

## 标签分类法

新标签必须先加入此表再使用（防止标签蔓延）：

- 归属：`general`、`vllm-omni`、`afd-plugin`、`vllm-gr`、`jiuwenswarm`、`vllm-router`
- 工作主题：`review`、`ci`、`docs`、`git`、`debug`、`bug`、`benchmark`、`environment`、
  `remote`、`agents`、`planning`、`dev`、`rebase`
- 代码/模型轴：`components`、`models`、`diffusion`、`model-executor`、`serving`、
  `scheduler`、`distributed`、`config`、`hunyuan-image3`、`ltx2`、`qwen-omni`、
  `plugin-boundary`、`attention-runtime`、`ffn-runtime`、`connectors`、
  `model-integration`、`execution-platforms`、`compatibility`

## 溯源标记

沉淀层段落引用具体事故/PR 时，段落末尾加 `^[incidents/...]` 形式的来源标记；
页面级证据在 frontmatter `sources:` 列出。每个沉淀层页面至少链接 2 个相关页面
（相对 Markdown 链接，不用 wikilink）。

## 被取代页面

先把仍然有效的独有结论合入最近 owner 的幸存页面，再删除被取代或重复页面并修复
入链。Git 历史负责追溯旧版本；`knowledge/` 内不再维护 `_archive/` 副本。

## 规则生命周期尾注

规则段（`## <ID> — <标题>`）可以以一行机器可读尾注结束，渲染时不可见：

```
<!-- kb:rule status=retired since=v0.30.0 retired_at=v0.31.0 reason=upstream-removed evidence="PR #8201" -->
<!-- kb:rule status=active since=v0.31.0 supersedes=BENCH-1b -->
```

- 没有尾注 = `status=active`（兼容全部既有规则）。
- `status=retired` 必须带 `retired_at`、`reason`（`upstream-removed` / `superseded` /
  `incorrect` / `duplicate`）与 `evidence`；`superseded` 还必须带 `superseded_by`，且
  后继规则带对应的 `supersedes`。
- 退役规则的正文在磁盘上保留一个发版周期供复核与撤销，但**立即停止对外提供**：
  Direct 文档读取、`doc_read`/`doc_search`、briefing 都会去掉它。
- `protected=true` 的规则只能经人工路径修改、退役或删除。
- 物理删除（purge）只针对已退役满一个发版周期的规则；被删除的 ID 记入同仓库的
  `_tombstones.yaml`，永不复用。
- 尾注、`updated:`、`sources:`（只追加）、新页面的 `_index.md` 行与 tombstones 由知识服务
  按 Knowledge Ops API 2.0 写入，并由 L1 门禁重算核对；手工改动也必须符合同一规则。

## Direct 路由表

每个仓库的 Direct owner 路由在 `repos/<repo>/_routes.yaml`（`schema_version: 1`，
`owners[{owner, path, signals, scope_prefixes}]`，可选 `models{dir, page}`）。路由引用的页面
必须存在；新增仓库只需提供它和 adapter，不改代码。

## 校验

```bash
python knowledge/tools/check_knowledge_tree.py   # 目录/索引/链接/错题/敏感信息
python knowledge/tools/check_wiki_lint.py        # frontmatter/标签/孤页/陈旧度
```
