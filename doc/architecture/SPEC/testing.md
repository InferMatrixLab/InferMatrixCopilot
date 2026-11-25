# testing/ —— 规范

<!-- verified-against: 2026-11-25 -->

`LOC ~2160（9 个文件） · 测试执行底座（rebase agent shell 测试层的 Python 移植） · refactor-status: ok`

## 职责
仓库中立的测试执行底座：gpu_lock.sh / kill_test_tree.sh / test_watchdog.sh /
test_runner.sh 的 Python 移植。仓库专属数据（watchdog 模式、artifact glob、
测试 manifest）全部住在 adapter 侧，本包只**接收**它们作为输入。

## 功能（一行一模块）
- `env_plan.py` —— 子进程环境构造：inherit-plus-overlay（`build_subprocess_env`）
  + agent-shell 的**allowlist 式**凭据洗刷（`scrub_agent_shell_env`）。
- `gpu_lock.py` —— 跨进程 GPU 互斥（磁盘协议与 shell 字节兼容）+ nvidia-smi
  孤儿清理 + VRAM 空闲等待。
- `process_tree.py` —— 进程树终结：BFS 收集后代、自身祖先排除、
  TERM → 宽限 → KILL、(pid, starttime) 身份校验防 PID 复用。
- `runner.py` —— 单测试执行器：GPU 锁、watchdog、分层超时、silent-exit 尸检
  footer、cov-strip 回退、pass marker、artifact 清理、后代快照线程。
- `watchdog.py` —— 双档日志看门狗：Tier 1 灾难模式即杀；Tier 2 去噪后交
  廉价 reviewer 裁决（KILL/CONTINUE）。
- `watchdog_learn.py` —— Tier-2 裁决的记录/收割/晋升：一贯良性的模式
  自动晋升进噪声 overlay YAML（数据，而非 shell 时代的改代码）。
- `cpu_ci_resources.py` —— 目标仓库 Simple CPU job 的单卡对齐：pytest 的 marker
  表达式恰为 `core_model and cpu` 时，把 `CUDA_VISIBLE_DEVICES` 收到第一张。
- `gpu_demand.py` —— 按需 GPU：向 canhazgpu 调度器**按命令**申请卡的模式开关、
  一条命令的卡数、`gpu run` 包装程序，以及交给会话配置与提示词的卡数。

## 公开契约
- `env_plan`：`build_subprocess_env(...)`、`scrub_agent_shell_env(env, keep_hf_token, extra_safe_prefixes)`，
  以及 `AGENT_SHELL_SAFE_EXACT` / `AGENT_SHELL_SAFE_PREFIXES` / `AGENT_SHELL_CRED_SUFFIXES` 常量。
- `gpu_lock`：`GpuLock`（context manager）、`GpuLockTimeout`、`visible_devices`、
  `cleanup_orphan_gpu_procs`、`wait_gpu_memory_idle`。
- `process_tree`：`collect_descendants`、`kill_tree(pids, identity=...)`、`kill_by_pattern`。
- `runner`：`TestRunner.run(job, env, baseline, dry_run) -> TestOutcome`、
  `TestRunner.terminate_active() -> bool`（对正在跑的 attempt 的进程组 + 快照
  后代 TERM→宽限→KILL；无 attempt 在跑返回 False；可自信号处理器/他线程调用，
  `_spawn` 的 `proc.wait()` 随后返回子进程的信号 rc）、`TestJob`（含 `gpus`）、
  `RunPlan`、`TestOutcome`、`strip_cov_flags`、`cleanup_test_artifacts`、
  `log_file_for`/`pass_marker_for`、`TIMEOUT_RC=124`、`PY_TIMEOUT_MARGIN_SEC`。
- `gpu_demand`：`GpuDemand`（`.from_env`）、`command_gpus`、`shell_needs_gpu`、
  `scheduler_wrap`、`granted_gpus`、`scheduler_error`、`duration_seconds`、
  `session_devices`、`prompt_devices`；常量 `MODE_ENV`、`HELD_ENV`、`WAIT_ENV`、
  `NO_GPU_RC`、`SCHEDULER_TIMEOUT_MARGIN_SEC`。
- `watchdog`：`WatchdogPatterns(.from_yaml)`、`LogWatchdog(.start/.stop/.check_once/.result)`、`WatchdogResult`。
- `watchdog_learn`：`record`、`harvest`、`eligible_patterns`、`promote`、
  `read_decisions`、`normalize_pattern`、`repair_tail`。

## 不变量
- **E2** —— 硬件门槛产生**显式的 `skipped` 结果**，绝不是 shell 时代那种
  虚增通过数的静默 rc=0；skip 前先撕掉旧 pass marker。
- 超时分层：**primary** 定时器在 `timeout_sec` 杀整个进程组 + 逐 pid 后代；
  **safety** 在 `+PY_TIMEOUT_MARGIN_SEC` **严格更晚** SIGKILL。fired 的定时器
  被 join 后才返回 —— 下一个 job 不会在后代仍占 GPU 时启动。
- PID 复用安全：快照 map **只增不减**，身份绑定在**发现时**的 (pid, starttime)，
  且记录只接受**已验证祖先链**上的 pid；`kill_tree` 丢弃 starttime 不再匹配的
  pid —— 升级 SIGKILL 绝不打中无关进程。
- GPU 锁绝不 unlink 活主的锁：偷锁在 flock side file 的临界区内**重新判定**
  staleness；release 只删除仍属于自己的锁文件。
- 锁主存活判定走 `process_tree._alive`：`kill(pid, 0)` 成功但
  `/proc/<pid>/stat` 为 `Z`/`X`（未被收割的僵尸）算**死**，否则一个已退出
  但父进程未 wait 的 pytest 包装器会无限期占住互斥锁（2026-10-27，
  `test_rebase_process_lifecycle` 用真实僵尸进程钉住）。
- **E2** —— Tier-2 无 reviewer / reviewer 异常 / 无法解析的回复一律 CONTINUE，
  **绝不 wedge 一次 run**；Tier 1 不经评审即杀。
- **E1（精神）** —— 每次 Tier-2 裁决落 decision log（`record_fn`/`result.decisions`），
  kill **先于**遥测执行 —— 满盘的日志绝不留下一个坏引擎继续跑。
- watchdog 的每次扫描以 `start_offset` 限定在**本次尝试自己的字节**内 ——
  setup 输出或上次尝试的报错杀不掉一次通过的主运行。
- **D4** —— 学习只做加法：overlay 只 append `noise`（可静音、**绝不锐化**）；
  `harvest` 以 state log 自身为去重权威，flock + 撕尾修复 + fsync，exactly-once。
- 凭据洗刷是 allowlist、fail-closed：未知名字直接丢弃，凭据后缀一票否决，
  HF token 仅显式 opt-in 重进；纯函数，**绝不改写自身进程环境**。
- artifact 清理只在 repo 根的**深度 1**，含路径分隔符/`..`/`**` 的 pattern 被拒。

## 边界 —— 不属于这里
watchdog 模式、artifact glob、manifest（adapter 数据）；测试循环/重试编排
（`rebase_engine/module_pytest.py`）；远程执行（随 shell 层退役）；不含 LLM
（reviewer 是注入的 callable）。

## 依赖（允许）
stdlib（`fcntl` 带 POSIX 守卫）；`pyyaml`（patterns/overlay）；包内互引
（runner → gpu_lock/watchdog/process_tree）；`watchdog_learn` →
`memory/skills._fsync_dir`（overlay 落盘持久化）。**绝不** import `engine/`。

## 扩展点
新的 watchdog 模式/噪声 → adapter 的 seed YAML 或 learned overlay；额外安全
env 前缀 → `scrub_agent_shell_env(extra_safe_prefixes=…)`（manifest 数据），
绝不放宽默认表；runner 的协作者（`review_fn`/`record_fn`/`available_gpus`…）
全部可注入。

## 测试
`test_testing_substrate.py`（77 个，覆盖全部六模块）、`test_shell_golden.py`
（TestRunner 对 shell 的 command-echo 金样）、`test_module_pytest_signals.py`
（经包装器：`terminate_active` 停树、stdin=/dev/null）；`watchdog_learn` 另由
`test_curator.py`（harvest exactly-once）与 `test_knowledge_foundation.py`
（record 身份字段、撕尾修复）钉住。`test_gpu_demand.py`（50 个）以一个替身 `gpu`
可执行文件覆盖按需模式：卡数、shell 命令的判定、包装程序的输出路由与等待到期、
`TestRunner` 的 skip，以及 agent shell 与模块测试包装器。

## 重构备注
`runner.py`（643L）是包内最重的一块：`_spawn` 把快照线程、双定时器、watchdog
接线挤在一个方法里，密度高但每行都背着一条已记录的竞态教训 —— 拆分前先读
注释里的事故史。身份校验逻辑分居 `_record_walk`（记录侧）与 `kill_tree`
（消费侧），契约靠注释维系；若再演化，考虑一个共享的 identity 小模块。

## 子进程 stdin 与可被叫停的 attempt（2026-10-28）

- `_spawn` 以 `stdin=subprocess.DEVNULL` 拉起 `bash --norc -c`：测试绝不继承
  包装器的 stdin（harness 工具在截止时关闭的管道、或会把挂断传给交互式子进程
  的终端）。
- `_spawn` 期间 `self._active_kill` 指向本 attempt 的树杀手（进程组 + 身份
  快照），`terminate_active()` 调它；`proc.wait()` 被 BaseException（包装器被
  KeyboardInterrupt/SystemExit 展开）打断时先刷新快照、杀树，再让异常传播——
  包装器被展开时测试树不得继续占 GPU。消费方是
  `rebase_engine/module_pytest.py::WrapperProcess` 的 SIGTERM/SIGINT 处理器。

## 按命令向调度器申请 GPU（2026-11-25）

在此之前，一次 rebase 从头到尾持有一份外层 `gpu run` 预约，每条 GPU 命令继承
它的 `CUDA_VISIBLE_DEVICES`。v0.35.0 campaign 在 2026-11-25 连续五小时占着 4 张
卡，其中最多只有 130 MB 在用；manifest 的 71 个 job 全部声明 `min_gpus=1`，在
GPU 锁下一次只跑一个，52 个只用 1 张卡。负责人决定：不再有任何预约横跨整个
campaign，改为每条 GPU 命令向调度器申请自己需要的卡、只在运行期间持有。

- **开关。** `IMX_GPU_MODE=demand` 选择按需模式；未设置时行为不变（调用方已
  持有卡，命令继承）。`IMX_GPU_WAIT`（缺省 `30m`）是命令在调度器队列里的等待
  上限。`IMX_GPU_HELD=1` 表示所在命令树已持有卡，其下任何 `TestRunner` 都不再
  申请。`TestRunner` 缺省从环境读取（`GpuDemand.from_env`），测试可显式传
  `gpu_demand=`。
- **卡数。** `TestJob.gpus` 显式给出；缺省取三者最大值：`min_gpus`、命令里最大
  的 `cards_N` pytest marker（manifest 的每个 job 都是 `hw: any`，卡数只在 marker
  里）、以及"取 GPU 锁的运行算 1 张"。0 表示不申请（`run_precommit`）。agent 的
  `run_pytest`/`reproduce` 与 `imx-omni-pytest` 包装器各要 1 张。
- **包装程序。** `scheduler_wrap(command, gpus, timeout_sec, wait, note, side_log)`
  返回一段给 `bash --norc -c` 的 shell：命令的 stdout/stderr 留在调用方给的描述符
  （fd 3/4），调度器自己的输出写进 `<日志>.scheduler`。实测调度器把进度打到
  stdout、把 `Error: wait timeout exceeded` 打到 stderr，等待到期时退出码为 1；
  包装程序把它换成 `NO_GPU_RC`（97），其余失败保留调度器的退出码。调度器的超时
  与空闲回收都设为 `timeout_sec + SCHEDULER_TIMEOUT_MARGIN_SEC`，让运行器自己的
  定时器先触发。
- **调度器的输出绝不进测试日志。** 本机的 `gpu run` 会打印一条 "failed to
  activate booking" 警告，命中看门狗 Tier-2 的 `failed to`，会让每个 job 多一次
  LLM 评审。日志里只留运行器自己写的一行 `[gpu] …`（拿到的卡，或没跑成的原因）；
  命令失败且调度器报了错时才附上它的错误文本。
- **排队到期是一次显式 skip。** `TestRunner.run` 返回 `rc=0, skipped=True`，
  `skip_reason` 写明要几张、等了多久；命令没有运行，也不写 pass marker。等待
  上限不超过 `timeout_sec` 的一半：运行器的定时器从 spawn 起算，排队时间也在内。
- **按需模式下由调度器决定有没有卡**：不做硬件门槛，丢掉继承的
  `CUDA_VISIBLE_DEVICES`（调度器授权时自己命名），并给子进程设 `IMX_GPU_HELD=1`。
- **命令结束后不在已释放的卡上清理进程。** 外层预约时代
  `cleanup_orphan_gpu_procs`/`wait_gpu_memory_idle` 是安全的，卡始终是自己的；
  调度器授予的卡在命令结束时已经释放，可能已经分给了别人。逃逸的后代由调度器
  自己的 guard 负责杀。
- **agent 在 shell 里起的测试。** `shell_needs_gpu(command)` 把命令按引号之外的
  列表分隔符与括号拆成简单命令，剥掉可能位于命令字之前的东西（`then`/`do`/
  `else`、`env`、`VAR=value`、`sudo`、`timeout`、`nohup`、`uv run`），再看命令字：
  `pytest` 或 `python -m pytest` 算测试；`python` 且同一条简单命令里有 CUDA 用法
  （`torch.cuda`、`cuda.is_available(`、`device="cuda"`、`--device cuda`）算 GPU
  代码；`python -` 从 stdin 读脚本时，脚本就是命令的其余部分。只在命令字上判断：
  对整行用一个正则时，`grep -rn pytest`、`echo pytest` 也会命中。提到
  `imx-omni-pytest` 或 `gpu run` 的命令原样放过，它们自己申请。
- **交给会话配置与提示词的卡数。** `session_devices(env)`：有
  `CUDA_VISIBLE_DEVICES` 就用它，否则是外层预约时代的历史缺省 `0,1`；按需模式
  下会话不持有卡，取空串。`prompt_devices("")` 在按需模式下返回一句"由调度器按
  命令设置"，提示词不再把 `0,1` 当作会话持有的卡交给 agent。

实测（2026-11-25）：一条 `TestRunner` 命令在真实调度器下恰好持有 1 张卡 35 秒，
前后都是 0；torch 看到 1 张卡；import vllm 及其编译扩展不建立 CUDA 上下文。
