# memory/paths.py —— 规范

<!-- verified-against: 2026-10-30 -->

`LOC ~262 · 记忆（可变知识位置的唯一解析器 + run/迁移锁） · refactor-status: ok`

## 职责
**唯一**解析所有可变知识位置的地方：把五处散落的路径表达式收敛成一个
frozen dataclass，给 PR4d 的 runtime-dir 切换留一个开关。

## 功能
`KnowledgePaths.resolve(settings, repo, adapter_root)` 产出该仓库的全部知识
位置（debug DB 三成员、skill seed/runtime、watchdog overlay/decisions/
checkpoint、backups、两把锁）。切换未激活 → 逐消费者的历史位置；激活（repo ∈
`settings.knowledge_runtime_repos`）→ 收敛到 `<memory_db 目录>/state/<repo>/`，但先验证迁移完成 marker。
`KnowledgeRunLock` 是 `state/<repo>/locks/knowledge.lock` 上的共享/独占 flock。

## 公开契约
`KnowledgePaths`（字段见类定义）+ `resolve()` + `MIGRATION_MARKER`；
`KnowledgeStateError`；`KnowledgeLockHeld`；
`KnowledgeRunLock(.acquire_shared/.acquire_exclusive/.release)`。

## 不变量（**D4/E2**）
- **未托管且切换未激活 = 字节恒等**：未列名仓库解析出的每个位置都与本模块存在前
  各消费者用的完全一致（含每个 no-adapter 回退）—— 纯重构，由测试钉死。
  debug 三成员（`rebase_backend_db`/`shared_write_db`/`debug_read_layers`）
  刻意镜像历史的**逐消费者**接线，切换前必须继续互不相同。
- **E2（fail-closed）** —— 激活了却没有有效 marker 的仓库 →
  `KnowledgeStateError`，**绝不**以空库静默起跑。marker 验证是证据式的：
  repo 必须匹配（抄来的 marker 永不激活）、schema 声明 v2 之外**库本身**
  必须带 v2 列 + 全索引 FTS5 镜像 + 一次 MATCH 可用性探针（假冒的普通表
  列名全对却 MATCH 全失败 —— F8/F9、round-2 F5、round-3 F4）。
  digest **刻意不**要求与活库相等 —— 激活后追加写入是设计内的。
- 锁协议：每次 run 全程持**共享**（互不争用）；迁移取**独占** ——
  迁移不能在任何潜在写者存活时开始，run 也不能在迁移中途开始
  （关掉 lock-census TOCTOU）。两侧都非阻塞、失败即 `KnowledgeLockHeld`；
  无 fcntl 的平台上**两侧一起**降级为不强制。
- **D4** —— watchdog decisions/checkpoint/backups 等巩固层位置由此统一
  发放，消费者不得自拼 state 路径。

## 边界 —— 不属于这里
不做迁移本身（`rebase_engine/knowledge_migrate.py`）；除 marker 验证探针外
不做任何库 I/O；除锁文件外不创建/写任何文件；激活开关的判定数据
（`knowledge_runtime_repos`）属于 `config`。

## 依赖（允许）
stdlib（`os`/`dataclasses`/`pathlib`，`fcntl` 带 POSIX 守卫）；marker 验证时
惰性 import `.debug_memory`。**绝不** import `engine/`。

## 测试
`test_knowledge_foundation.py`（`test_paths_byte_identity_with/without_adapter`、
`test_knowledge_lock_shared_and_exclusive`）；
`test_knowledge_migrate.py`（marker/FTS 假冒的十余个 activation-refuses 用例、
`test_flag_on_requires_marker_and_is_repo_scoped`、
`test_activation_error_exits_blocked_and_releases_run_lock`）。

## 重构备注
`_require_migration_marker` 是浓缩的 FTS 取证（每个分支对应一条评审发现）——
保持"marker 的声明不是证据"这个立场，别为省事削弱任何一个探针。
PR4d 全量切换落地后，逐消费者的三个 debug 成员应收敛并删掉分叉注释。

## Managed installation paths (2026-10-30)

When `COPILOT_RELEASE_ROOT` identifies a managed installation, immutable adapter
stores remain read fallbacks. Learned debug-memory writes and repository-map
caches use the stable per-repository state directory outside the release.
Explicit knowledge-runtime cutover still validates its migration marker.
Unmanaged installations retain the existing legacy-path contract.
