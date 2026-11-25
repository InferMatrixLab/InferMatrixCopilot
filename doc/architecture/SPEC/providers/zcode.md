# providers/zcode.py —— 规范

<!-- verified-against: 2026-11-25 -->

`harness transport（Z.AI / GLM 订阅） · refactor-status: ok`

## 职责
在 Z.AI OAuth 订阅认证下，通过 `zcode` CLI 跑完**一整个** agent step。

## 功能
已识别的模型失败事件映射为只包含固定安全消息、稳定 code 和 retryable 的
`ZCodeFailure`；额度、限流、认证和暂时不可用分别表达。未知失败保留现有 RuntimeError
行为；分类器不把提示、原始事件或提供方错误正文暴露给 RFC 服务。

headless `zcode --prompt=... --output-format stream-json --mode <mode> --cwd <临时目录>
--disallowed-tools=<非读工具>`；工具桥写进临时目录的 `.zcode/config.json`；从事件流解析
`result.response`、`result.usage`、`session.updated.modelId` 和 `tool.updated`（`kind:
scheduled`）。prompt 超过 100,000 字节时写成文件并 `--attach`（zcode 没有 stdin 通道，
argv 单参数上限 128KiB）。`--mode` 按调用选：一次性调用用 `plan`（不带桥）；agent 会话里
只读作用域用 `guarded`、会改动的作用域用 `yolo`（0.18 起，见下文）。

## 公开契约
`ZCodeTransport`（`auth_gap`、`run_session`、`complete`）、`spec = PROVIDERS["zcode"]`。
`complete(..., native_event_sink=None)` 保留无工具、隔离临时目录调用；显式传入 sink 时
实时交付原生事件，`supports_native_events=True`。`native_snapshot()` 只返回提供方实际报告的
用量与模型；缺失的 token、缓存和 USD 字段保持未知，不补造零值。

RFC 的错误分类和无工具调用可在缺少 `fcntl` 的平台上导入。写入工作区 MCP
信任记录前必须取得 POSIX 文件锁；缺少锁支持时抛出 `FileLockingUnavailable`。

## 不变量（**C1**、**C2**）
- **会话 cwd 永远是自有临时目录，不是 PR worktree。** zcode 从 cwd 加载 `.env`，并从 cwd
  往上到 git 根发现 `zcode.json` / `.zcode/config.json`，项目配置里的 stdio MCP server 会被
  直接连接。PR checkout 不能成为 cwd；仓库按绝对路径读取。
- **非读工具靠剥离，不靠信任。** `--mode plan` 下仍有 node REPL MCP、子 agent（能拿到
  shell）、workflow、联网抓取；`_DISALLOWED` 把它们全部去掉（实测 28 → 11 个工具，全是读）。
  每次会话事后对照 `_READ_TOOLS` + 工具桥前缀审计，越界记为 `audit:` refusal——zcode 新版本
  加了而拒绝列表漏掉的工具会被**侦测到，不会静默**。保留下来的原生读工具
  （`Read`/`Grep`/`Glob`，读超长 prompt 附件要用）还要做容纳审计：路径（含绝对 glob 的根）
  落在 checkout、run 目录、会话临时目录之外即违规（复用 `audit.contained_in`；glob 的字面前缀
  按搜索目录解析，`../../etc/*` 这类穿越也会被抓到）。这是侦测型；预防型的读取走工具桥。
- **`complete()` 连原生读工具也去掉**（输入可能是不可信文本）；只有 prompt 超长、必须靠
  `Read` 读附件时才保留 `Read`，且任何读到临时目录之外的调用都让这次调用失败。
  one-shot 的局部配置关闭 memory/use、skills、plugins、subagent 和 MCP，并把 CLI storage
  放入会话临时目录；避免保留下来的 Read 接触宿主自动记忆。工具桥会话同样关闭 memory/use。
- **工具桥的 MCP 超时跟随会话预算。** 会话配置里写
  `mcp.servers.<bridge>.timeoutMs = timeout_s * 1000`；zcode 默认 30s，
  会在桥里仍在跑的测试或嵌套计划评审完成前放弃该工具调用，之后每次桥调用
  都像挂死。会话 deadline 仍是执行的外层上界（2026-10-27）。
- **失败的运行不能读成空评审。** 非零退出或流里没有 `result` 事件（未超时时），普通调用
  和原生事件调用先将已识别失败转换为固定安全消息的 `ZCodeFailure`；未知失败仍抛
  `RuntimeError`，保留 zcode 自己的 stderr 诊断。
- **原生日志的持久化不能截断 CLI 输出。** 原生事件模式将 stdout/stderr 接到每次调用自有的
  临时普通文件，宿主按独立偏移实时读取并逐事件留痕；不对共享写描述符 seek。
  ZCode 的退出 watchdog 不等待管道缓冲排空，慢速留痕时普通文件避免丢失已完成回答。
  进程结束后仍排空所有输出及末尾非换行片段，再严格核验 `result`；超时只约束仍在运行的
  CLI，已退出进程的日志排空不误判为模型超时。异常／中断仍回收该调用的进程组和临时文件。
- **模型按次钉死，再断言。** zcode 没有 `--model`；一个会话取其个人 provider 配置里的
  `defaultModelSelection`，且仅当该条目**可选**（0.16.9 的 `resolveInitialModelSelection` 要求
  `options.reasoningLevel` 存在，否则静默回退到目录里第一个模型——这就是过去手写条目"不起作用"的原因），
  并按**区分大小写**的相等匹配模型 id（`glm-5.3-flash` 选不中，同样回退）。传输层因此在会话临时目录写一份
  **最小**个人配置：空的 provider/model 规则集 + `defaultModelSelection`（provider id 取 `zcode_provider_id`，
  缺省读宿主条目的 id，再缺省为 Z.AI 个人 coding plan；模型 id 经 `canonical_model_id` 按目录拼写规范化——
  宿主 `runtime/provider/**/zcode-builtin.json` 里的 id 加 0.16.9 的内置清单，未知 id 原样写入；
  `options.reasoningLevel` = `zcode_reasoning_level`，low/high/max，缺省 max），用
  `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` 指给子进程。**宿主文件除 provider id 外一概不读入**：会话目录是
  zcode 会话的读根，宿主 provider 规则可能带 API key，不能落在里面；宿主文件从不改写。请求的模型
  （`req.model` / `STRICT_BACKEND_MODEL`）仍与流里的 `modelId` 不区分大小写比对，不一致时按
  `MODEL_MISMATCH_POLICY` 处理（缺省 `fail`）；未请求模型时不写配置，服务宿主缺省。注册表仍不声明 `default_model`。
- `auth_gap()` 只检查 `~/.zcode/v2/credentials.json` 是否存在（zcode 没有登录状态命令）；
  存在不代表 token 仍有效，过期会在第一次运行时大声失败。子进程保留 `ZCODE_DATA_BASE_DIR`，
  保证检查的目录与运行登录用的是同一个。

## 边界 —— 不属于这里
不改 zcode 的全局配置（`~/.zcode/v2/provider_config.json` 属于宿主）；不编造用量或模型。

## 依赖（允许）
stdlib + `.base` + `..agent_loop.AgentOutcome` + `..llm` 的类型与 `ModelMismatchError`。

## 测试
`test_provider_zcode.py`（离线，事件形状取自 zcode 0.16.9 实跑）与
`test_zcode_stream_spool.py` 覆盖无工具隔离、模型钉住、两种调用路径的安全失败分类、
慢速 sink 的完整输出、真实超时和中断回收；2026-09-30 用真实 zcode 做过
一次端到端冒烟（工具桥 `read_file` 被调用，`bridge_trace.jsonl` 有记录）。

`complete()` 接受并忽略 `max_budget_usd`；`stops_at_spend` 为 False（CLI 无花费阈值，`ModelGateway` 请求阈值时在派发前拒绝）。

`subscription_billing` 仅在实际选中的 provider 为已知 OAuth coding plan 时为 True；自定义 provider
或 settings 中的 API provider override 返回 False。它描述计费模式，不声称调用花费为零。
`kb init --subscription-generator` 通过此声明显式使用订阅生成器，保留未报告的 USD 与 token 用量。

## zcode 0.17 与 0.18（2026-11-11、2026-11-24）

- **项目级 MCP server 的信任登记（0.17）。** 0.17 给项目作用域的 MCP server 加了
  工作区信任门（`createWorkspaceMcpTrust`），无头 prompt 过不去：工具桥被静默留成
  未连接，每个模块 agent 都只能读、随后阻塞（run-20261111-160314）。`_write_mcp_config`
  写完会话配置后调用 `_seed_workspace_mcp_trust`，把本会话的信任记录追加进宿主的
  `~/.zcode/security/workspace-mcp-trust-v1.json`（flock 串行、幂等，并清掉会话目录
  已不存在的记录）。记录的 `declarationDigest` 是对规范化后的
  `{version, workspaceIdentity, workingDirectory, serverName, sourcePaths, config}`
  做 sha256——键排序、紧凑分隔符、非 ASCII 原样；其中 `config` 是该 server 去掉
  `enabled`/`source` 后的执行字段，`env` 缺省为 `{}`、`cwd` 缺省为工作目录。摘要
  的算法若在将来的版本里变化，就**失败关闭**：桥保持未连接，引擎的桥存活检查拦住
  该模块。0.17.1 上实测过（桥被拉起、工具调用往返）。
- **只读作用域跑 `--mode guarded`，不再是 `plan`（0.18）。** 0.18 的 plan 模式要求
  每个 MCP 工具都经审批，而无头会话无人审批：只读作用域经工具桥的读取被拒
  （"Plan mode requires approval for MCP tools that are not read-only"）。拒绝列表
  剥掉之后，plan 与 guarded 留下同一批原生工具（全是读），guarded 只对危险命令
  保留询问。给桥工具标 `readOnlyHint` 在 plan 模式下不起作用：那里的 MCP 能力一律
  是 `needsApproval`。一次性调用（`complete()`）仍用 plan，它不带桥。
- **会话管理工具加入 `_DISALLOWED`（0.18）。** `CreateSession`、`ForkSession`、
  `InterruptSession`、`ListSessions`、`ReadSession`、`RenameSession`、`SendToSession`、
  `WaitSessions` 能触达宿主的其他会话。宿主安装的插件 server（`mcp__plugin_*`）在
  每种模式下都列在工具清单里，仍留在审计白名单之外，与之前相同。
- **桥进程的环境带上 `IMX_GPU_MODE` 与 `IMX_GPU_WAIT`。** 桥自己会启动 GPU 命令
  （agent 的 shell 与测试工具），campaign 按命令向调度器取卡时它需要知情。
  `IMX_GPU_HELD` 有意不传：它只在已持有卡的命令树内部为真。

0.18.0 上的实测（2026-11-24）：一次性调用服务于钉住的模型；只读会话经桥读文件；
  会改动的作用域经桥写文件。信任登记沿用 0.17 的算法，0.18 仍然连得上桥。
