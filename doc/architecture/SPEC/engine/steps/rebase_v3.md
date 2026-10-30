# engine/steps/rebase_v3.py —— 规范

<!-- verified-against: 2026-10-30 -->

`LOC ~2204 · step 库（v3 rebase 装配层） · refactor-status: oversized`

## 职责
把 `rebase_engine` 包接进 executor：locked playbook `repo-rebase-v3` 的全部
step —— 薄的受治理 wrapper，substate-first、类型化失败、发布被消费的键。

## Steps（12 个）
| step | kind/risk | 发布（`state_updates`）/ 裁决 |
|---|---|---|
| `rebase.v3_prelude` | deterministic/read | `mode_*` 标志、`run_id`、`upstream_origin_path`、`last_rebase_upstream_commit`、(remote_ci) `upstream_commit`、(full) `upstream_path`；注册终局报告 finalizer；知识开账 attest |
| `rebase.v3_guard` | deterministic/write_workspace | 无发布 —— 幂等重取 checkout flock（首取在 prelude），再注入 adapter `rebase.guard` 策略委托 `workspace._guard_clean_rebase` |
| `rebase.v3_scan` | deterministic/read | `manifest_jobs`；`test_manifest.json` 产物 |
| `rebase.v3_wheel` | deterministic/write_workspace | `upstream_commit`（选 commit → 装进目标 venv → **最后**才 pin Dockerfile：装失败绝不留脏树） |
| `rebase.v3_assign` | deterministic/read | `active_modules`、`wave1_modules`、`wave2_modules`（path-sync 后按 wave 分派） |
| `rebase.v3_wave_gate` | deterministic/read | wave-1 失败**或 interrupted** ⇒ `wave2_modules=[]`；`halt_on_module_failure` ⇒ ESCALATE |
| `rebase.v3_module_rebase` | script/write_workspace | `module_<name>_status`（foreach；同 run 串行锁 —— 同一 checkout；substate done/skipped 短路防重入二次执行；outcome `interrupted` ⇒ BLOCKED，可 `--resume`） |
| `rebase.v3_test_loop` | script/write_workspace | `phase3_failed`；substate `tests.pipeline`/`infra_failures`；空 manifest ⇒ `manifest_empty` |
| `rebase.v3_precommit` | script/write_workspace | 无发布 —— substate `tests.precommit`（passed/failed/failed_preexisting/not_declared） |
| `rebase.v3_push_gate` | deterministic/read | `push_gate_flagged`、`push_gate_overrides`；不许 ⇒ FORBIDDEN；override 记 trace |
| `rebase.v3_ci` | script/push | `ci_result`、`ci_build_urls`；substate `ci`（rounds/adopted/unfixed，以及逐 job 分类证据与 monitor 时长） |
| `rebase.v3_finalize` | deterministic/read | 无发布 —— substate `phase=done|needs_human`；有失败 ⇒ BLOCKED（复用 exit 3） |

## 公开契约（注册的 step 之外）
`_adapter_manifest/_substate/_task_params`（被 `rebase_knowledge` import）、
`manifest_job_to_test_job`（golden 测试）、`_build_backends`（含 `doc_search`/`doc_read`：复用 review 流程同一套 `KnowledgeDocs` 视图，按 adapter manifest 的 `knowledge.repo_subdir` 限定仓库）（read-compat
测试）、`_make_ci_client`（模块级工厂，测试注入 fake 客户端）。

## 不变量
- **A4**：12 个 step 全部 `@step(...)` 就地自注册；`when:` 门**只**读
  prelude 发布的 `mode_*` 标志（**B3** 已知键，复合标志预算于
  `rebase_engine/modes`）。**B2**：被消费的键都经 `state_updates` 发布
  （上表），substate 同时落盘 —— 两份都写。
- **substate 裁决**：模块/测试/CI 失败是 substate **数据**，携带它们的 step
  返回 ok —— 终局裁决属于 `v3_finalize`（全 ok + substate 有失败 ⇒ BLOCKED
  needs-human），且它在 report 之后运行，RUN_REPORT 必已存在（**B1**）。
- **B1**：结构性拒绝全程类型化 —— mode 缺失/未知、adapter 非 active、声明的
  venv 展不开、无 CI token/provider/签名身份 → BLOCKED；绝不猜权限。
- **C4**（`v3_ci`）：只许推 adapter 声明的 `push.rebase_branch`（且
  `rebase_branch_allowed`，绝非受保护分支）；`ALLOW_PUSH` 未设 ⇒
  push_dry_run ⇒ **FORBIDDEN**；推送走 `push_to_ci` WAL，op id 对 CI 台账与
  push WAL **双清点**后起编（resume 绝不复用已指名的 op id）。
- 预推送采纳豁免必须**被证明**：remote ref 已等于本地 HEAD 才把 head 传给
  `ci_loop.run_ci_rounds`（同头 schedule 构建可采纳）；ls-remote 失败或
  不一致即禁用豁免（安全默认）。
- **checkout flock 先于任何 mutator**：prelude 取锁，每个 mutating step 经
  `_ensure_checkout_locks` 幂等重取（resume 重放 prelude 不执行它）；释放是
  lifecycle finalizer，每条退出路径都放。可变操作只碰 run_dir 内的
  `git clone --shared` **scratch** upstream；canonical 路径绝不被采纳为可变
  树；resume 采纳幸存 scratch 时重新注册 teardown。
- full 模式的模块与 debug 入口也重新确保 scratch：wheel/assign 已 checkpoint
  时仍按 `upstream_commit` 恢复 checkout；不得改用 canonical HEAD，checkout
  失败返回 BLOCKED（debug 返回结构性 error）。
- **C3**（`_module_scope`）：repo 树 + run dir 是硬可写墙；模块
  `local_paths` + `plans/` 是 primary；未知模块得到永不匹配的 primary ——
  它的每次仓库写都被记录 out-of-scope，而非静默在圈内。
- **E1/E2**：能力缺失记 `capability_gap` 并声明式降级（无 tier key/CI
  token/客户端、debug backend 缺失 ⇒ 回归记为结构性失败），绝不静默跳过。
- debug 尝试**快照护栏**：snapshot → agent → 内容 digest 变更检查（staged+
  unstaged+untracked 字节+mode+symlink）→ 复跑/本地验证 → patch policy；自动
  patch 不得修改 assertion/tolerance，test 文件编辑必须有明确本地 passed。
  被拒、验证失败或策略违规的尝试**回滚**。
- debug agent 的 prompt 与 `rebase_module` 同源选择：`debug_prompt_template_live`
  优先，缺省回落 `debug_prompt_template`。
- 空/损坏 manifest ⇒ `manifest_empty`（push gate 阻塞，绝不空洞通过）；
  不可运行的命令归 STRUCTURAL —— 绝不借 bash rc=0 假通过。
- **A5**：一切仓库知识来自 adapter manifest；parity 词汇泄漏上限 14，
  只能变小（`test_repo_vocabulary.py`）。

## 边界 —— 不属于这里
不含 rebase 机制本体（`rebase_engine/{module_rebase,test_loop,ci_loop,
push_to_ci,wheel,push_gate,assign,…}`）；不含 provider HTTP（`ci/buildkite`）；
知识尾段在 `rebase_knowledge`；finalizer 机制在 `engine/lifecycle`。

## 依赖（允许）
`rebase_engine/*`、`engine/{step,lifecycle}`、`._common`、`scopes`、
`ci/buildkite`（经 `_make_ci_client`）、`adapters/base`、`memory/*`、
`testing/*`、`config`、anthropic SDK；另有对 `.workspace` 的 guard 委托
import（见重构备注）。

## 测试
`test_assembly.py`（mode 治理、push-gate、report_only e2e、终局行、同头采纳）、
`test_v3_complete_e2e.py`（local_ci/full/remote_ci 全程）、`test_ci_wiring.py`
（`v3_ci` 接线）、`test_shell_golden.py`、`test_knowledge_readcompat.py`、
`test_repo_vocabulary.py`（泄漏上限）。

## 重构备注
2204 行 —— 全库最大的 step 文件；知识尾段已拆出（`rebase_knowledge`），下一
刀的自然缝：① env/backends 构建（~83–465 行）；② 锁与 scratch 生命周期
（~496–635 行）；③ `v3_ci` 及其 helper（~1753–2204 行）。被
`rebase_knowledge` import 的三个 helper 应下沉 `._common`，一并消掉那条 A2
违例；对 `.workspace._guard_clean_rebase` 的直接 import 是另一条。拆分时
substate + `state_updates` 双写、模块短路/串行锁的 crash-window 契约**必须**
原样保留（resume 完整性测试护住）。

## harness 后端派发（2026-09-19）

module agent 可派发到 harness provider（`settings.rebase_backend`）。
harness 会话通过 MCP tool bridge 拿到同一套工具面，plan gate 在派发处强制。
`build_backends(repo=)` 收的是仓库 **ROOT 路径**（→ `TestRunner(repo_root=)`），
不是仓库名：传名字会让 run_pytest/run_precommit/reproduce 以
`FileNotFoundError` 静默失效，module 却报告完成。

## attempt 级进程回收与测试预算注入（2026-10-27）

- `_agent_shell_env` 把 adapter 的 `rebase.testing.module_timeout_sec`
  （缺省 7200）写成 `TEST_TIMEOUT_SEC`；`_v3_module_rebase` 与 `_run_debug_agent`
  据此填 `ModuleRunConfig.test_timeout_sec`，并用
  `settings.rebase_harness_timeout_sec` 代替硬编码的 harness 会话预算。
- `_ensure_checkout_locks` 取锁前、锁 finalizer 释放前，以及 `_run_debug_agent`
  的 `finally`，都调用 `rebase_engine.processes.cleanup_run_processes`：
  上一个 attempt 留下的后台 pytest 链不能把 GPU 锁带进下一个 step
  （run-20261024 的 `GpuLockTimeout` 阻断即由此而来）。

## harness tier 直通、评审路由与 scratch 生命周期（2026-10-27，自 feat/rebase-main-v0320 移植）

- `_tier_client`：`target.kind == "harness"` 时返回 `(transport, target)`，只检查
  CLI 可用与登录（`auth_gap`），不再因为空 api_key 而 BLOCKED——
  `STRICT_BACKEND=cursor` 的 run-20261027-173249 正是在此处的前身被挡。
- `_harness_completion_client(settings, backend)`：把 harness `complete()` 适配成
  `messages.create` 形状，供计划评审与 wave-gate 评审在 `rebase_backend != api`
  时使用；`stop_reason != end_turn` 抛错而不是给出裁决。评审模型 =
  `rebase_reviewer_model`，否则 `rebase_backend_model`/`strict_backend_model`。
- `_run_debug_agent`：harness tier 走 `_harness_attempt`（与模块 attempt 相同的
  budgets、CUDA/HF、`target_venv`、state slice），API tier 保持 `run_agent_loop`。
- remote_ci 的上游 commit 信号必须是 40 位 SHA，在边界拒绝。
- scratch：只有干净 `done` 才丢弃一次性上游 checkout；blocked/崩溃的 run 可恢复，
  其 venv 的 editable vllm 解析进 scratch。full 模式的模块/debug 入口按
  `upstream_commit` 重新确保 scratch。
- baseline venv（`ensure_baseline_venv`）：硬链接克隆 campaign venv 并换入
  baseline commit 的 wheel，让"omni main 对上次对齐的 wheel 是否也失败"可问；
  使用发布 wheel（若配置 URL）或非 editable 安装，删除临时源码后再验证导入；
  克隆的 console/activation 脚本通过原子替换改为 baseline 路径，不能写穿硬链接
  修改 campaign。缓存复用也必须通过导入检查。任何失败返回 None，保留保守的回归判定。

## 操作者重跑与本地超时预算（2026-10-29）

- `--resume --rerun-from wave1`：`_v3_module_rebase` 短路条件不变（done/skipped），
  所以 failed 模块重跑、done 模块直接返回；`_v3_wave_gate` 重新裁决 wave 2。
- `_v3_test_loop` 在 `state["rerun_from"]` 非空时以 `retry_infra=True` 调
  `run_test_loop`：记录过的 timeout/watchdog infra 再跑一次，已通过的 job 不重跑。
  普通 resume 下 infra 仍然粘滞。
- `rebase.testing.local_timeout_multiplier`（默认 1，须为 >= 1 的数，否则 BLOCKED）
  放大本地 `TestJob.timeout_sec` 和命令行首的直接 `timeout DURATION`；构建出的 manifest 保留 CI 的
  `timeout_in_minutes`（shell golden 不变）。vllm-omni 取 3：Ready 的 20 分钟 CPU
  Simple step 在共享 rebase 主机上通过 53–88% 用例、零失败时就被杀。
- 本地已配置虚拟环境时，将行首 `uv pip install --system` 改为
  `uv pip install --python "$VIRTUAL_ENV/bin/python"`。候选与基线各自使用
  `_target_test_env` 选定的虚拟环境；不修改 CI manifest，也不向系统 Python 安装依赖。
- API drift guard 覆盖输出处理器、请求状态及 profiler 基类；除参数名外还检查位置参数容量，
  防止将上游可按位置传递的可选参数改成仅关键字参数后仍误报兼容。

## 信号中断的模块：BLOCKED 收口、resume 重跑（2026-10-28）

`_v3_module_rebase` 在 `rebase_module` 返回 `status == "interrupted"`
（引擎收到 SIGINT/SIGTERM，见 `engine/lifecycle` 与 `rebase_engine` 规范）时
返回 `StepResult(False, BLOCKED, "<m>: interrupted by <SIG> — resumable with
--resume", state_updates={module_<m>_status: interrupted})`：executor 不再推进到
下一个 foreach 兄弟，run 由 `workflow_execution` 以 `interrupted` 收口。短路
条件**不变**（只有 done/skipped），因此 `--resume` 重跑 interrupted 模块、跳过
done；`_v3_wave_gate` 把 interrupted 与 failed 同等视为"wave-1 未完成"（半
rebase 的模块不是 wave 2 的基础）。测试：`test_assembly.py::
test_v3_module_rebase_reruns_interrupted_and_blocks_on_interrupt`、
`test_run_shutdown.py::test_wave_gate_treats_interrupted_as_not_done`。
