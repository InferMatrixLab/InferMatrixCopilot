# rebase_engine/ —— 规范

<!-- verified-against: 2026-10-29 -->

`LOC ~7500（26 个模块） · repo-rebase-v3 的原生 rebase 引擎 · refactor-status: ok`

## 职责
父级 rebase agent 机制的仓库中立移植（PR0..PR7 分片落地）：repo-rebase-v3
的全部**引擎原语**。仓库专属内容一律作为数据来自 `adapters/<repo>/`；
把原语接成 step 的装配在 `engine/steps/rebase_v3`（+`rebase_knowledge`），不在这里。

## 功能
四个阶段的原语 + 底座（phase-1 分析 → phase-2 模块 agent → phase-3 本地
测试环 → phase-4 push+CI；substate/runtime/锁/WAL）。逐模块图：

| 模块 | 独占的那件事 |
|---|---|
| `__init__.py` | 包 docstring（仓库中立宣言）；无逻辑 |
| `agent_loop.py` | rebase agent 循环（流式、`.decision.md` 计划闸、150 轮预算，cache-parity） |
| `assign.py` | commit→模块 确定性归类 + 路径漂移检查 + 报告渲染 |
| `ci_loop.py` | CI 构建生命周期：受守卫的创建/恢复、monitor、日志分类器、round 编排 |
| `debug_patch_policy.py` | 自动 CI debug 补丁策略：禁止 oracle 弱化，未本地验证时禁止 test 编辑 |
| `gitio.py` | git 机械层：暂存纪律、签名提交重试、token 头传输、执行已授权 decision |
| `hooks.py` | `RebaseHooks` —— adapter 可定制的窄行为面，fail-closed 加载 |
| `knowledge_attest.py` | 知识层的 WAL 安全逻辑摘要 + 快照/恢复（只读） |
| `knowledge_migrate.py` | PR4d 一次性知识迁移：全锁在手、逐 store 日志化事务 |
| `modes.py` | rebase 模式治理：唯一权威解析 + 写回、`mode_*` 旗标、闸冲突裁决 |
| `module_pytest.py` | `imx-omni-pytest` 受闸测试包装（GPU 互斥、看门狗、分层超时、自建会话、信号回执） |
| `module_rebase.py` | 单模块 rebase 单元：prompt → agent loop → substate 结果；桥存活判定；可中断 |
| `parent_compat.py` | 父知识库的只读兼容读取层（`mode=ro`，fail-closed 打开） |
| `path_sync.py` | 模块路径图同步 + manifest modules 段重写 + L2 决定应用 |
| `phase1_steps.py` | phase-1 组合（归类 + 路径同步），父级报告文件名不变 |
| `plan_review.py` | L4 计划评审后端（注入的 LLM client，父级形状的结果） |
| `processes.py` | 回收本 run 的后台测试进程（含 setsid 脱离的链）：按 `IMX_RUN_ID` + `IMX_LOG_DIR` 双标记 + 出生时间识别 |
| `prompt_builder.py` | 模块/调试 prompt 渲染 —— 等输入下与父级字节一致（golden 钉住）；`*_live` 变体不受 parity 钉 |
| `push_gate.py` | 推送闸裁决：结构性 vs 断言失败的确定性分类（Rev 8 §2.3） |
| `push_to_ci.py` | commit+push-to-CI 编排：preflight、WAL 卫生、C4 双闸、单一传输 |
| `push_wal.py` | 推送 WAL：先落盘的 intent、精确 OID 三分对账、回滚数据 |
| `remote_ci_owner.py` | 远端 CI 作业 → 模块归属解析（本地与远端作业名、pipeline 路由） |
| `rebase_tools.py` | 父级 20 工具 + `doc_search`/`doc_read`（追加在父级顺序之后）作为 `ToolDef`；未接线后端**可见地**失败 |
| `wheel.py` | 预编译 wheel 选取与安装：目标分支游走、baseline rev-parse、venv 持有者释放、EBUSY 重试、scratch 自清理 |
| `runctx.py` | `RebaseRuntime` + `CheckoutLock`（flock+卫生盾）+ 按事件循环的注册表 |
| `substate.py` | 可持久、单写者、merge-not-overwrite 的 `state.json`（run_id 戳） |
| `test_loop.py` | 本地测试环：逐测试恢复、baseline 复跑分流回归、类型化 skip |
| `test_manifest.py` | 动态测试清单：CI YAML + 活测试树 + diff 分类 → 模块计划 |
| `testing_env.py` | agent shell 子环境 scrub 接线（仅 agent shell，进程环境不动） |
| `worktree.py` | 工作树卫生：中止残留 in-flight 状态、丢弃未跟踪产物、L2 脏树决定 |

## 公开契约
按簇：`run_agent_loop` / `rebase_module(ModuleRunConfig)`；`run_test_loop`
/ `run_ci_rounds`（`CIClient` 协议 + 注入动作）；`commit_and_push`→
`PushOutcome`、`evaluate_push_gate`→`GateDecision`、`PushRecord` +
`record_intent`/`reconcile`/`resolve_pending`；`resolve_effective_mode`/
`mode_state_flags`；`Substate`、`REGISTRY`、`CheckoutLock`；
`build_manifest`、`build_module_prompt`、`build_rebase_tools`、
`ensure_wheel_installed`（`WheelSpec`/`PinSpec`）、`load_hooks`。
runner/LLM/CI client 全部可注入 —— 每个模块都能离线测试。

## 不变量
- **C4** —— `commit_and_push` **从不自我授权**：`allowed` 与 `allow_push`
  （ALLOW_PUSH）由调用方传入，`push.guard_push` 裁决；无 `allow_push` 在
  WAL 前就停为 dry-run；执行参数**只**从已授权的 `decision.command` 推导
  （`decision_push_args`），`execute_push` 对未允许的 decision 抛错。
- **C4** —— WAL 纪律：推送前先落**可持久** intent（tmp+fsync+replace+目录
  fsync，真实存储失败传播）；重入先 `resolve_pending`；对账三分
  intended⇒pushed / pre-push⇒retry / 其余⇒escalate，**绝不猜**，且先比
  canonical 远端身份；分支缺失时创建走 absence-pinned lease。token 只走
  `http.extraheader`，URL 进 argv 前去凭证，有 token 时 SSH 改写 HTTPS；
  probe/push/WAL 身份共用**同一次**解析的 URL。
- 模式与闸（Rev 8 §2.1/§2.3）：可变模式**只能显式选取**，`report_only=True`
  + 可变模式、strict+with-failures 都 BLOCKED（narrowing 胜，绝不猜）；
  结构性失败总是阻断（除显式 `push_with_failures`，被记录），断言失败
  FLAGGED 放行。**B3**：`when:` 只用 `mode_state_flags` 的旗标。
- **B1** —— 失败类型化且 fail-closed：`GitIOError`（坏索引绝不读作干净）、
  `PushWalError`（坏记录绝不静默跳过）、`PushPreflightError`（非 40-hex
  上游 commit / pin 不匹配拒推）、`SubstateError`（异 run substate 拒收）。
- **C3** —— agent 的每次工具调用都过 `tools.dispatch`（写工具经
  `write_path_arg` 路径 scoping）；计划闸通过前 edit/pytest/precommit
  工具**不可见**。
- **D2** —— hooks 人闸：manifest 显式声明 + adapter active 才加载，且
  manifest `rebase` 段高风险（agent 写被拒）—— agent 无法自装 hooks。
- **E2** —— 显式降级：未接线后端返回**可见错误**，绝不装作空搜索结果；
  `parent_compat` 声明了但坏 ⇒ 构造即抛，开后故障降级为显式 error dict。
- 检出锁：flock 拿到后必须锁内装好 `/locks/` 卫生盾（根锚定、原子写、
  外来行保留）—— **盾装不上等于锁没拿到**；注册表按（run_dir, 事件循环
  弱引用）发放，绝不复用死循环的原语；teardown 有界、绝不外抛。
- GPU 受闸：`IMX_GPU_MUTEX=1` 时每次调用都持 GPU 锁，否则仅 e2e/examples
  取锁（父级 parity）。
- wheel：每个 uv 调用 `--python` 显式指向**声明的目标 venv** —— 启动
  shell 的 venv 状态绝不影响装到哪；只有 import 验证失败才重试。
- CI 创建受守卫：durable op 记录先于 API 调用，恢复按 op id 精确匹配，
  **不确定时绝不重复创建**。
- 每个被 monitor 的 CI round 持久化实际 monitor 时长，供 run metrics 从 durable
  substate 计费；无法恢复的旧/中止 build 时长保持 unknown。
- CI baseline 使用最高可信来源（schedule > API > other）的最近窗口：最新
  构建失败立即生效，较旧失败只有在窗口内重复出现才保留；单次陈旧红构建不会
  越过更新的绿构建复活。
- baseline 分类以同名 job 的日志根因为身份，provider exit code 只作候选排序；
  支持 `FAILED`/`ERROR` pytest node 的原因相关子集，但缺坐标、日志、当前签名
  或共同异常证据时一律 fail-closed。每个 job 的分类理由与 baseline build/job
  坐标随 round 持久化，供恢复和审计使用。
- 自动 debug agent 不能修改 assertion/tolerance oracle；任何 test 文件编辑只有
  在对应本地验证明确 passed 时才可进入远端重试。拒绝的尝试由调用方恢复快照。
- 调试 prompt 优先用 adapter 的 `debug_prompt_template_live`，缺省才回落到父级
  `debug_prompt_template`；后者保持父级原文，作为 parity golden 的比较基准。
- **A5** —— 全包仓库中立：仓库值经 `WheelSpec`/`PinSpec`/`ManifestSpec`/
  `ModulePromptData`/`tool_schemas.json`/hooks 注入；`test_repo_neutral_core` 钉住。

## 边界 —— 不属于这里
推送授权规则（`push.py`）；工具 scope 强制（`tools`/`scopes`）；step 注册
与编排（装配 PR）；CI provider 的 HTTP 实现（adapter 接线）；测试进程底座
（`testing/`）；知识存储本体（`memory/`）。

## 依赖（允许）
`..push`、`..tools`、`..scopes`、`..run_trace`、`..memory.debug_memory`
（migrate 惰性用 `..memory.*`、`..adapters.base`、`..llm`）、
`..testing.{runner,watchdog,env_plan,process_tree}`；`yaml`；stdlib。
包内 import 单向。**绝不 import `engine/`**（A2 —— 叶子包）。

## 扩展点
adapter 数据（WheelSpec/PinSpec/ManifestSpec/prompt_data/tool_schemas/
watchdog 模式）；`RebaseHooks` 子类；注入可调用（`CIClient`、`RunFn`、
test loop 动作、`precommit_fix`、`RebaseBackends`、LLM client）。
新能力 = 新注入点或新 adapter 数据，不是仓库字面量。

## 测试
`test_push_cluster.py`（gitio/push_wal/push_to_ci/push_gate）、
`test_phase1_cluster.py`、`test_engine_core.py`（substate/runctx/modes/
worktree）、`test_assembly.py`（tools/loop/module/prompt/wheel）、
`test_ci_wiring.py`（ci_loop/test_loop/test_manifest）、
`test_ext1_checkout_guard.py`（锁+卫生盾）、`test_knowledge_migrate.py`、
`test_parent_compat.py`、`test_shell_golden.py`、`test_v3_complete_e2e.py`、
`test_module_pytest_outcomes.py`、`test_module_pytest_signals.py`（真包装进程：
自建会话、忽略 SIGHUP、SIGTERM 停树并记 `wrapper_signal`、外部 kill 判
`external` 并写诊断、进程内调用不改调用者会话、子进程 stdin=/dev/null）、
`test_run_shutdown.py`（模块 `interrupted` 两条路径、钩子幂等）、
`test_tool_bridge_lifecycle.py`（`bridge_health` 判定与 `bridge_unavailable`）。

## 重构备注
`ci_loop.py`（1156 行）最大：monitor/分类器/round 编排同居，再长就按
`engine/steps/pr` 先例拆包。`knowledge_migrate.py` 与 `parent_compat.py`
是 PR4d 执行后的退役候补（"无永久双 store 世界"）。docstring 里的
choke-point 编号（agent_loop/rebase_tools 写"C5"）落后于 `_CONSTRAINTS.md`
目录（工具 choke point = C3）—— 值得统一，改注释不改行为。

## module_rebase 的 harness 路径（2026-09-19）

`module_rebase` 既可跑进程内 agent 循环，也可把一个 module 交给 harness
provider；两条路径共用同一 prompt、同一工具面与同一 plan gate。

## module agent 轮次预算（2026-09-19）

`RebaseModuleConfig.max_turns` = 400。耗尽预算不致命：引擎会**重启** agent
循环而不是判 module 失败——但重启会丢掉已积累的上下文，所以预算过低时
agent 会反复从头探索。flash 级模型在同一 module 上约需 pro 级 2.3 倍轮次
（140+ vs 62），150 的旧上限会被截断三次仍未完成。

## 结构性失败仍要报告已产生的断言（2026-09-21）

`test_loop` 把 timeout / watchdog kill / harness crash 归为**结构性**失败：
不进 `failed_tests`、不做 baseline 对比、push gate 直接阻断——这部分不变。
但结构性结束**不等于**这次运行什么都没发现：一个 suite 可能先报了断言失败
再被杀掉。那些 node id 过去随 output 一起丢弃，报告只剩"1 个 infra 失败"。
现在 infra 记录里会带上已报告的断言 id（复用 `ci_loop.extract_failed_test_ids`）。
实例：2026-09-19 `simple_diffusion_test` 超时前已报 10 个量化断言失败，全部不可见，
直到 Buildkite #3055 重新发现。

## attempt 级进程回收与模块测试预算（2026-10-27）

module agent 曾用 `setsid` 启动 pytest 链并在会话结束后留下它们继续跑：
run-20261024 里这些链在 wave 1 完成后又占着 run 的 GPU 锁约 7 小时，
`tests` step 等锁 3600s 后以 `GpuLockTimeout` 阻断。现在：
- `processes.cleanup_run_processes(run_dir, run_id)` 在每次 module/debug attempt
  结束时（包括正常返回）调用。只回收**同用户**且**同时**携带 `IMX_RUN_ID`
  与 `IMX_LOG_DIR` 两个标记的进程；身份绑定出生时间，PID 复用不会误杀；
  其他 run 的进程不受影响。prompt 不再教 agent 留下脱离链。
- `ModuleRunConfig.test_timeout_sec` 来自 adapter 的
  `rebase.testing.module_timeout_sec`（vllm-omni 为 21600：offline inference
  整套约 4 小时，旧的 7200s 包装器永远到不了终态），渲染为 prompt 的
  `{MODULE_TEST_TIMEOUT_SEC}` 并写进每次 attempt 末尾的预算说明。
- `harness_timeout_s` 不再硬编码 7200，取 `settings.rebase_harness_timeout_sec`。

## harness 完成契约、读审计与宿主事实（2026-10-27，自 feat/rebase-main-v0320 移植）

- `_harness_attempt` 的完成不再等于"会话未截断"：需要**本次 attempt** 重写的
  `signals/module.<m>.done`（首行 `MODULE_DONE <m>`）、没有新的 `.fail`、未截断、
  没有 typed refusal、且本模块的计划闸在本次 attempt 内打开。零事件的 CLI 崩溃
  曾被读成 done 并通过 wave gate。
- typed refusal 中，`audit: <tool> outside session roots: ...`（读包含）**不**否决
  完成：那是原生读工具无法关闭的 harness 的侦测记录，写进 trace
  （`harness_read_audit`）与结果文本。写审计与会话失败仍然否决。
- 会话请求带 `read_roots=(vllm_path, target_venv)`；桥 spec 带
  `host_env={CUDA_VISIBLE_DEVICES, HF_HOME}`（见 `tool_bridge`）。
- `module_pytest.py`：`rebase.testing.module_max_timeout_sec` 是 `TEST_TIMEOUT_SEC`
  的上限（vllm-omni 与计划预算同为 21600），`module_test_platform` 过滤非本平台
  的 pytest 项。未显式给 `-m` 的模块跑同时带上与 Buildkite 相同的 run level：
  默认 `--run-level core_model`，标记为 `(<platform> or 平台中性) and core_model`。
  调用方已写 `--run-level` 时，标记改用那一档，不再另加 core_model。
- `wheel.py`：`release_venv_holders`（仅 venv 结构存在时、排除调用者祖先链）、
  EBUSY/ETXTBSY 重试、baseline rev-parse 前置校验、分支重置前 `git clean -ffd`。

## 包装器进程契约：信号来源可追溯、脱离 harness 工具会话（2026-10-28）

run-20261027-193251 的 `model_executor` 四次模块测试在约 30 s 处结束，回执
rc -15/-15/-9/-2、`timed_out=false`、`watchdog_triggered=false`——包装器自己
没有杀它，回执却说不出是谁杀的。证据链（agent 日志第三次 attempt 的说明、
campaign 日志几百条 "GPU 1 reservation lost"、宿主 root 运行的
`canhazgpu guard --enforce`）指向宿主 GPU reservation guard 对它视为未预约的
GPU 进程执行 SIGINT→SIGTERM→SIGKILL 阶梯。`module_pytest.py` 现在：

- `WrapperProcess.detach()`（**只在真实 CLI 入口**调用：`main(argv=None)`；
  进程内调用者传 argv 时 `process_setup` 默认 False，绝不改调用者的会话）：
  非会话领导者时 `os.setsid()`（`IMX_WRAPPER_FOREGROUND=1` 退出；进程组领导者
  的 EPERM 记入 `wrapper_detach_error`），并忽略 SIGHUP——harness 工具的前台
  截止杀进程组、关 stdin，都到不了它。
- `install_signal_handlers(runner)`：SIGTERM/SIGINT 记入 `received` 并经
  `TestRunner.terminate_active()` 对当前 attempt 的进程组 + 快照后代
  TERM→宽限→KILL；被叫停的包装器绝不把 pytest 留在 GPU 上。`proc.wait()`
  被任何 BaseException 打断时同样先杀树再传播。
- 回执 `schema_version` 2：新增 `signal`（结束子进程的信号名或 null）、
  `signal_origin` ∈ timeout / watchdog / wrapper_signal / **external** / null、
  `signal_diagnosis`（仅 external：外部监管者杀了测试进程，
  `CUDA_VISIBLE_DEVICES` 的 GPU 未为本进程树预约，不是测试失败）、
  `wrapper_pgid/sid/ppid`、`wrapper_session_leader`、`wrapper_detached`、
  `wrapper_detach_error`、`wrapper_parent_comm`、`wrapper_signals_ignored`、
  `wrapper_signals_received`。external 与 wrapper_signal 还在测试日志尾部追加
  "signal postmortem" 页脚（**先于**回执写入，回执的 `log_sha256` 覆盖它），
  并在 stderr 给出一行结论；stdout/stderr 已关闭时静默。
- `testing/runner.py::_spawn` 以 `stdin=/dev/null` 拉起测试。
- live prompt 的 detach 段改为可核验程序：一次工具调用内 `setsid -f` 启动 +
  `ps -o pid,sid,…` 证明 `sid == pid`，否则重启而非回退前台；轮询
  `kill -0 $(cat pidfile)`，结束后读 `tests/outcomes/wrap-*.json` 回执；
  `signal_origin: "external"` 的含义与处置（记 debug memory、报 incomplete、
  不循环重试）写进 prompt。

## 桥存活判定与可中断的模块循环（2026-10-28）

- `bridge_health(spec_path, *, since_ts, settle_s=BRIDGE_EXIT_SETTLE_S)` 读
  `tool_bridge` 的 `<stem>.lifecycle.jsonl`（只看本次会话开始之后的事件，
  start 有而 exit 未到时最多等 2 s——harness 先于其桥子进程返回）并给出
  `verdict` ∈ ok / never_started / exited_early（`detail` 带桥自己的
  `reason`/`rc`）/ no_exit_recorded / restarted，附 `slow_tools`、
  `slowest_tool`。`_harness_attempt` 在非 `ok` 时记 trace
  `bridge_unavailable`（step、spec、verdict、detail、计数；不含事件原文）并在
  结果文本追加一行，供 debug prompt 与 agent 日志读到。
- `rebase_module` 可中断：每次 attempt 后轮询 `engine.lifecycle.
  shutdown_requested()`——有请求则**不再**做 debug 重试，未 done 的模块写
  `{"status": "interrupted", "interrupted_by": <SIG>, …}`、记 trace
  `module_interrupted`、agent 日志加一行；attempt 返回 done 的模块**保持 done**。
  强制路径：attempt 在飞时 task 被取消（`CancelledError`）同样先标
  `interrupted` 再抛；此外在飞期间注册一个停机钩子（`in_flight` 守卫：attempt
  已返回则让位给协作式判定），工作线程调用它得到同一份痕迹。标记幂等：
  已是 `interrupted` 不重复写、不重复记事件。
- `rebase_v3._v3_module_rebase` 对 `interrupted` 返回 `StepResult(False,
  BLOCKED, …)`（状态更新 `module_<m>_status=interrupted`），run 以 interrupted
  收口而不是推进到下一个兄弟；substate 短路**只**接受 done/skipped，所以
  `--resume` 重跑 interrupted 模块、跳过 done；`_v3_wave_gate` 把 interrupted
  与 failed 同等视为未完成。
