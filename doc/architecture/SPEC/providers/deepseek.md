# providers/deepseek.py —— 规范

<!-- verified-against: 2026-09-30 -->

`LOC ~502 · harness transport（dsh，API-keyed） · refactor-status: oversized`

## 职责
通过 DeepSeek 自己的 agent harness（`dsh`）的 Python SDK 跑完**一整个** agent step
—— 这是**打破了注册表两条 harness 假设**的后端，两处都是刻意为之且都已声明。

## 功能
驱动 `deepseek-harness-sdk`（它本身是个子进程 SDK，拉起捆绑的 `dsh-jsonrpc-agent`
并通过 JSON-RPC/stdio 通信），**逐会话生成 composition**，以便按 scope 钉住沙箱模式。

## 公开契约
<!-- 模型：dsh 没有内建缺省，空 model 会让每一轮以 "has no provider/model" 失败。
     缺省值 deepseek-v4-pro 声明在 ProviderSpec.default_model，由 tier_target 解析；
     本 transport 只对直接调用方做同源的防御性兜底。 -->
`DeepSeekHarnessTransport`（`cli_path`、`require_cli`、`auth_gap`、`run_session`、
`complete`）、`spec = PROVIDERS["deepseek"]`。

## 不变量（**C1**、**C2**、**E2**）
- **它是 API-keyed，不是订阅认证的。** `base.md` 写明 harness 自己持有认证，
  `Settings.tier_target` 对 harness 返回空 key 正是出于这个理由。dsh 是例外：
  它需要一个 DeepSeek 凭据，经 `DeepSeekHarnessConfig.api_key` 交进去。
  对 cursor/claude-code/codex 而言，注入 key 会是一个**缺陷**；在这里它却是 harness
  能跑起来的唯一方式。注册表用 `api_keyed` 标出这一点。
- **它用不了我们的工具桥 —— 这是实测事实，不是选择。** 捆绑的运行时编译进了 122 个
  插件，`@deepseek-ai/dsh-mcp-client` 不在其中（扫描可执行文件验证过）。因此会话跑在
  harness 原生的 `bash` + `str_replace_editor` 上，我们的 scoped 工具（含考古工具组）
  **作为具名工具不可达**。请求工具桥的会话在执行前拒绝；显式使用原生工具的
  会话仍报告 `mcp_bridged=False`。
  **注册表不得为这个 provider 声明 `mcp_tools`**（在 2026-08-17 之前它声明了；
  没有任何分支读它，所以这条假声明只起到了误导读者的作用）。
- **跑在原生 bash 上的 arm，绝不能被标成 "tools bridged"。** 本轮战役已经测到过三个
  与其标签不符的 arm。
- **沙箱模式是被钉住的，绝不继承。** 上游 minimal 出厂即 `mode: danger-full-access`，
  它自己的 README 也说只应对一次性 checkout 使用。而这台机器是共享的、`.env` 里放着
  真实凭据，所以生成的 composition 会把模式钉到该会话的 scope 上。
- **step 上限是我们进程内预算的 6 倍**（`_STEP_CAP_FACTOR`），不是 1:1：实测健康的
  lens 在预算 14 的情况下用了约 40 步，1:1 会截断正常会话；6× 仍然能在离实测到的
  558 步一个数量级之外拦住失控。
- 一旦运行时的插件集合与 composition 的假设不再匹配，`_assert_plugins_bundled` 会
  **大声失败**。

## 边界 —— 不属于这里
不含桥实现；不含评测标注策略（但上面那条标注不变量**约束调用方**）。

## 依赖（允许）
stdlib + `.base` + `.registry` + `..agent_loop` + `..llm` 的类型 + dsh SDK（惰性 import）。

## 测试
`test_provider_deepseek.py`。

## 重构备注
约 502 行，是目前**最大**的 transport，因为它要自己生成 composition —— 而基于 CLI 的
那几个是从厂商那里拿到的。如果继续增长，composition 构建部分（`_composition`、`_env`）
是天然的拆分点。

## 2026-09-28
`complete()` 接受并忽略 `effort`（与 base 契约一致）。

## 2026-09-30
`complete()` 接受并忽略 `max_budget_usd`；`stops_at_spend` 为 False。

## 2026-10-12

SDK execution now runs in `providers.deepseek_worker` under the shared process
supervisor. Its filtered parent environment, absolute deadline and process group
cover SDK startup, the turn, and shutdown. The adapter no longer owns a Timer.
The worker carries SDK configuration over stdin and separates SDK logs from its
result protocol. Unknown completion failures propagate to shared accounting.

Install the optional `infermatrix-copilot[deepseek]` extra for the composition
API supported here (`deepseek-harness-sdk==0.1.0rc6`). SDKs lacking `cordis` or
`session_root` fail before runtime construction. Constructor compatibility and
offline lifecycle tests do not establish live model-quality parity.
