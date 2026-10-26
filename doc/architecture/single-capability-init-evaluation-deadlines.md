# Init 质量评估整工作流超时门禁

新增派发前门禁，复用原 arm_runtime._workflow_timeout／_host_timeout，不新建预算或超时解释。薄 init 入口识别也收敛为同一静态 helper，供依赖绑定与时间校验使用。

- phased init：phase 时间必须覆盖显式 workflow wait 和一次 host preparation；workflow wait 至少为声明调用上限×host timeout＋30秒。
- standalone init quality：入口自身执行同样检查。
- 多phase外层：至少覆盖所有串行 phase 时间之和＋30秒，防止phase各自合法但外层提前终止。

所有新检查只在首次派发准入运行。旧冻结记录的只读恢复检查不套用新准入规则；原helper和manifest未更新，未知调用不重放。Direct／Strict原检查保留，7200秒上限与原206调用协议均未扩大。声明额度是否覆盖实际工作流仍需独立call-shape证明，不能通过把上限写小绕过真实评估要求。

覆盖七种init case、直接入口及外层多phase的拒绝与可接受边界。原helper上9项反例均失败；当前147项相关评估／组合测试通过。拒绝发生在执行目录、stage账本和模型派发前。最初standalone测试草稿误用了90而非该fixture实际70秒边界，草稿失败保留，修正测试后重新验证旧／新实现。

仅修改评估helper、对应测试及说明；Provider6629与Bot3bbf71df生产输入／wheel字节不变，无需重建产品wheel。补丁从前一完整交付的3个原文件重建并逐字节验证；README追加说明单独保留。证据目录：`output/single-capability-completion-20261012/init-evaluation-deadlines-20261017`。真实质量、独立新source／labels、额度准入、原unknown与发布验收仍开放。
