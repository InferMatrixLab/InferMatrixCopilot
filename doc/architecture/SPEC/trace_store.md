# trace_store.py —— 规范

<!-- verified-against: 2026-11-04 -->

`LOC ~500 · trace/1：Copilot 与 RB 共用的模型调用/工具调用/判定/结果记录 · refactor-status: stable`

## 职责
- `TraceStore(root)`：追加式 `records/<日期>.jsonl`（事实来源）+ 内容寻址 blob
  （`blobs/<aa>/<sha256>.gz`，gzip，读取时复核哈希）+ SQLite 索引 `index.db`
  （按 kind、run、仓库、变更集、PR、规则 ID、角色、模型、结果查询；`rebuild_index` 可从 JSONL 重建）。
- 记录（`schema: trace/1`）：`kind` ∈ `model_call`/`tool_call`/`decision`/`outcome`/`replay`/`knowledge_event`；`context`（run_id、
  playbook、step、repo、changeset_id、pr、rule_ids、workflow、unit_id、item、fingerprint、of…）、`model`（role、provider、model、effort、served_model）、
  `usage`、`seconds`、`inputs`/`outputs`（只存 blob 引用，从不内联正文）、`result`、`error`、`env`
  （copilot 版本与 SHA）。`outcome` 是事后金标（merged、closed_unmerged、head_changed、rule_retired）。
- `trace_context(**fields)`：用 contextvars 绑定上下文，嵌套叠加，线程/协程安全。
- `begin_call(entry)`：在模型派发前保存输入 blob 及独立的
  `attempts/<UUID>/attempt.json`。`NativeCallArchive.event` 逐条脱敏、追加并 fsync
  `native-events.jsonl`，保留原生会话、输出、错误与调度等待事件。结束时绑定唯一的
  `model_call` 回执及输出、用量、耗时与状态；journal 不是另一笔调用，不能重复计费。
  中断未完成的 journal 保留输入和已收到的事件，未报告用量及费用保持未知。
  对 Zcode，网关调用的 model 与 attempt.model 额外记录 `native_reasoning_level`，
  表示实际写入会话配置的 low/high/max；role.effort 不是该 transport 的有效推理设置。

- `bind_store(store)` / `current_store()`：把一个 store 绑定为当前进程/任务的采集汇（`tools.dispatch` 与
  `LLM.create` 两个 choke point 据此写 `tool_call` / `model_call`，失败调用记 `error` 且无 `outputs`）；
  未绑定时不采集。执行器在 `settings.trace_store_root` 非空时为整个 run 绑定，并给每个步骤绑定单元上下文
  （run_id、playbook、step、unit_id；已登记的工作流再加 workflow、item、fingerprint）。

## 索引 schema 与迁移（changelog）
- v0（隐含 `user_version` 0）：13 列，位置式插入。v2（`PRAGMA user_version = 2`）：加 `workflow`、`unit_id`、
  `fingerprint` 三列与索引。JSONL 是唯一事实来源，索引是派生物。
- **打开从不迁移**：全新库直接建为 v2；已有 v0 库以兼容模式使用（命名列写入省略新列，按新列查询抛
  `IndexNotMigrated`）。
- `migrate_index(writers_paused=…)`：`index.migrate.lock` 上的 `flock`（持有者退出即由内核释放，无陈旧锁、无接管路径；
  文件永不 unlink）、写者静默门（知识服务账本全部暂停且无活租约 + RB 非空排空标记，或 `offline_confirmed`）、备份 `index.db.bak-<ts>`、单个 `BEGIN IMMEDIATE` 事务内 ALTER +
  从 `record` 列 JSON 回填全部旧行 + 建索引 + `user_version`，之后核验（无"JSON 有值而列为 NULL"的行；
  每个 workflow 的索引 id 集合等于 JSONL 扫描）；失败整体回滚。
- `rebuild_index(to_schema=0|2)`：同一门；写入临时文件后 `os.replace` 原子替换；`to_schema=0` 是降 pin 前的
  回退路径（旧代码的位置式插入必须能写）。空根（无 index.db）的恢复不需要暂停门。
- `compare_index()` / `verify_index()`：只读核验，可在写者运行时执行。
- CLI：`infermatrix-copilot improve migrate-index|rebuild-index|rollback-index|compare-index|verify-index`。

## 不变量
- 写入前统一脱敏：已知令牌形态（GitHub、Anthropic、OpenAI、AWS、Slack、Bearer、私钥块）以及名字表明是
  密钥的环境变量的值（路径形式的值除外）。脱敏发生在哈希之前，blob 与 JSONL 中都不会出现密钥。
- 只依赖标准库；经 `sdk.v1` 导出（`TraceStore`、`TRACE_SCHEMA`、`trace_context`、`redact`）时不加载私有模块。

## 测试
`test_improve_p0.py`（tool_call、版本化索引与兼容模式、门控迁移与回滚、锁互斥与陈旧接管、回填与查询等价、
重建到 schema 0 + 旧代码位置式插入、只读比较、两处 choke point 的采集、声明与指纹、执行器上下文、CLI 门）；
`test_trace_store.py`（schema 与去重 blob、索引与重建、脱敏、网关记录成功与失败调用、回放、导出的判定/结果
关联与校准集泄漏过滤、SDK 导出、知识服务的 decision/outcome 记录、派发前输入留档、原生事件及中断保留）。

## 2026-11-04 knowledge_event

`trace/1` 允许 `kind=knowledge_event`，`result` 包含
`schema=knowledge-use/1` 的共享知识使用协议。session、retrieval、content、
evidence、assessment、gap 和 finish 事件沿用脱敏 blob、追加 JSONL 与派生索引；
run/step/repository/rule 等现有查询列保持可用，不触发索引迁移。会话级排序、
引用与幂等校验属于 `knowledge_trace/`，TraceStore 只拥有通用持久化。
新 kind 不得被当作模型调用、事后 outcome 或训练样本。协议与验证见
[knowledge_trace.md](knowledge_trace.md) 及 `test/test_knowledge_trace.py`。
