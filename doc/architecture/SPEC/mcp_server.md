# mcp_server.py —— 规范

<!-- verified-against: 2026-11-25 -->

`MCP 协议适配器 · refactor-status: ok`

## 职责
把 MCP 工具请求映射到 `app.run_service.RunService`。预约、worker、执行与
轮询的应用实现由该服务持有；这里仅注册工具并投影协议错误。

## 功能
评审要跑 5–12 分钟，任何同步 MCP 调用都会超时，所以接口是**start/poll 工具对**：
`start_review` / `start_issue_answer` / `start_issue_triage` 预约后**立即**返回
`run_id`；`get_result`（经 `next_offset` 分页，受 `mcp_report_max_bytes` 封顶）
和 `get_status`（`run_status` + `progress`）负责轮询。

## 公开契约
`CopilotMCP` 是 `RunService` 的兼容别名；`build_mcp()` 注册 start/poll 工具，外加
`get_capabilities()`（包 `contract.capabilities`，含
`MAX_STRICT_WORKERS=1` 与文件锁能力上报）。`start_review` 接受
`expected_head_sha` 及默认关闭的 `include_score`；`start_strict_review` 接受 `idempotency_key` 并把
`(run_id, created)` 语义（见下）落到入队决定上。新增内部
`reserve_strict_review` 原样返回该 tuple，供 typed SDK 准确报告幂等复用；
`start_strict_review` 继续只返 `run_id`，保持 MCP/既有调用兼容；
`reserve_quality_review` 保留 `(run_id, created)` 供 SDK 维持真实幂等语义；
`start_quality_review` / `get_quality_result` 是专用的、同样钉 head 且
幂等的兼容工作流对，`quality_readiness` 在预约前验证模型、checkout 与
`pr-quality` playbook。

## 不变量（**C2**、**C3**、**E1**）
- `build_mcp()` 只注册协议工具和转换拒绝；不创建第二个执行器。
- `mcp` SDK 在 `build_mcp()` 内才加载；SDK 和核心包不依赖可选 MCP 安装。
- `CopilotMCP` 兼容别名指向 `RunService`，所以旧调用仍共享同一策略、
  幂等与轮询语义。队列、隔离子进程、双重策略复检及恢复规则详见
  [`app/run_service.md`](app/run_service.md)。

## 边界 —— 不属于这里
不含 Direct 模式逻辑（`thin_mcp_server.py`）；不定义策略（`mcp_policy.py`）；
不定义状态文件格式（`run_status.py`）。

## 依赖（允许）
stdlib + `mcp`（仅构造时可选加载）+ `.app.run_service` + `.mcp_policy`。

## 测试
`test_mcp.py`（篡改防御、单写者对账、分页、只读工具集、快照绑定转发、
子进程 post 闸恒关、未知 run_id 不抛、结构化 result 附带）；
`test_contract.py`（capabilities 上报、configure_strict_repo 已亡）；
`test_e2e_strict_mock.py`（离线端到端：钉 head 评审、idempotency 重试、
第二个子进程 no-op、post 拒绝）。

## 重构备注
预约形状（先建 run 目录，再规划）由 MCP 和 embedded Strict SDK 共用；CLI
主路径仍在建目录之前过门。不要把这两种不同的执行入口混为一谈。
