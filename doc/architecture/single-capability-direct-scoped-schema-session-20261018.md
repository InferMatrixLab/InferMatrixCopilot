# Direct 沙箱的 scoped bridge 与结构化结果 · 2026-10-18

这是框架交付后的主流程修复。原 a10／24ac 交付保持其历史验证范围；新工作候选为 Copilot de065255…／ReviewBot24ac7cfb…，尚未完成新版本 full paired／native cutover／质量验收。此前的 a10 完整整合和 native controls 不自动绑定到新候选。

## 发现与唯一连接

核对真实比较前的账号配置时，发现 Direct 的 execution_config 分支只将 snapshot_source 交给禁用工具的结构化 completion。源码虽存在于沙箱，模型没有受限工具去读取邻接实现；之前真实 PR 控制验证的是底层 session 准备，并未证明 Direct 入口选择了该路径。

现在 Direct 在应用组合处创建已有 read-only scope／write_bridge_spec，再通过 run_readonly_json 的显式 session_request 进入现有 freeze_session、complete_native 和 provider transport。scope 只允许现有 READ_ONLY_BRIDGE_TOOLS，strict_extras 开启，read_roots 仅含冻结 source、专用 session frame 和已签发的 knowledge。

- 删除 Direct 沙箱分支原来的 source-only 拼装，改用 canonical session snapshot；没有新 vendor CLI runner、修复循环或新预算账户。
- 结构化执行继续由原 execute_readonly／run_original_native 持有唯一 intent／result／accounting。相同结果复读不派发；schema、scope 或原身份漂移拒绝复用，未知结果保留原 attempt。
- AgentSessionRequest 增加可选 output_schema；仅 Codex 显式声明 session_schema，其他后端在派发前拒绝。Codex 将 schema 交给原 _run／schema_projection 编码器及 --output-schema，MCP 仍走原受限 bridge。工具关闭策略未放宽为原生 shell 或任意写权限。
- 结构化 session 超时即使返回合法 JSON 也不能完成；初次验证和回执恢复都调用同一 validator。
- DirectClient 的 issued-context lookup 合并为一处，供文档读取、完成校验和应用绑定复用。bridge 取本 review 已签发的 knowledge_view，避免在 plan 后重新解析 active snapshot；旧完成／读取的语义保留。
- 未请求 schema 的旧 session 不增加 identity 字段，旧 schema-free native intent 保持原形。已在途的旧 release／profile／环境保留；新代码不替换旧 paid pending。

## 已执行验证

源代码相关检查覆盖 SDK Direct 入口、原始回执复读／unknown、schema／scope 绑定、工具只读、schema 能力拒绝、provider／JSON／usage／边界、SDK／知识上下文和 pinned snapshot。初次无 SDK 的系统 Python 只产生 import refusal；切换明确 source 环境后验证。新增测试最初插入位置错误导致三个 fixture IndexError，原日志保留，恢复原测试主体后通过；没有更改产品验收预期。

最终 scoped 29 项与安装后 31 项通过，无 fail／error／skip。已有 51 项 source 主流程、186 项 provider／边界、44 项 SDK／context、17 项 pinned-view 证据保留各自范围；最后重新执行实际 Direct 入口，验证最终 issued-view 绑定。重复项不累计当整体进度。

新候选 wheel SHA256：de0652555d7b6ac81edfbcd3131c071208f81bfef2386617ea54c0c1d57b9179。与 Bot24ac 在独立新环境安装，2337／130 产品成员逐字节核对；安装测试以 -I 从复制的测试目录运行，没有产品 source 路径注入。依赖复用，不称 fresh 安装。初版9cdb候选及其安装结果保留，不能冒称为最终 wheel 的证据。

## 可审阅代码与剩余连接

相对 a10 的生产增量 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-scoped-schema-session-20261018/from-a10-product.patch`)；新 wheels 与安装身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-scoped-schema-session-20261018/final/candidate-identity.json`)；生产变更清单／AST (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-scoped-schema-session-20261018/product-delta.json`)。共 8 个产品成员，相对 a10 净＋60 行／＋612 AST；测试另列，不以净减行为验收门槛。

接下来冻结新代码并完成新候选的完整 paired deterministic、installed 及 local／SSH runtime 验证；将真实比较 caller 显式迁移到新配置并重新冻结 account／proxy／release/profile，随后才进行模型质量比较。当前 eval Direct driver 的旧 shim 方案不能冒称已经验证这个新 SDK sandbox 路径，仍需迁移。

本轮没有模型调用、账号探针或 proxy 新进程，没有修改519、spare、旧 unknown／History ack 或原执行环境；新182仍仅提案。新源候选不是上线版本，未commit／push／GitHub写入／部署。完整目标保留；框架既有交付范围与新修复验收分开记录。
