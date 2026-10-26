# 工作流恢复遇到 head 漂移（2026-10-17）

框架交付保持完成。本次关闭一个后续 P1 验证维度，没有新增生产实现：Direct／Strict 的真实 WatchService、router、publisher 和 SQLite job／attempt 在三个崩溃点恢复时遇到新 head。模型结果和 GitHub 存储为持久化测试适配器；进程退出／重启走实际产品路径。

| 崩溃点 | 恢复结果 | 不变量 |
| --- | --- | --- |
| 远端已接收、响应尚未返回 | stale，拒绝向旧 head 再发布 | 原 attempt／provider_run_id／head_sha 保留，一次模型和一次远端 effect |
| attempt 已完成，job 未终结 | already_posted | 原 completed receipt 与 reviewed head 保留，不重新计算 |
| attempt 已完成，metrics 未写 | already_posted | 同上，未把缺失 metrics 当成未执行 |

每种情况覆盖两个模式及两次恢复。后两种情况在 head 已漂移后额外经历远端标记查询异常和暂不可见：job 保持 queued，保留原身份，没有替代模型或第二条 review，待标记恢复可见才结算。没有修改 stale-head 规则：already_posted 是确认旧 effect，不是向旧 head 发布新结果。

最终测试复制到独立目录，使用已安装 Provider6629／Bot3bbf、Python -I、无 pytest source-path 注入；当前全部包资源、RECORD 和 pip provenance 再核验通过。最后修改前后的生产 wheel 不变。新组合检查与原 publisher／workflow 恢复回归通过；这不能代替真实 GitHub 一致性、并发发布或所有深层恢复验收。

首轮新增断言误以为 stale attempt 也有 reviewed_head_sha，观察到原 router 只在 completed 时写入该字段，因此修正测试为检查原 head_sha 并保留 stale 的 null；原两项断言失败报告留在 draft-reviewed-head-expectation.xml，没有修改产品语义。最终模型调用和发布 effect 计数均来自测试 SQLite，不涉及真实服务。

证据：output/single-capability-completion-20261012/workflow-head-drift-recovery-20261017。补丁 reviewbot-tests-only.patch 应用于框架最终包还原结果，仅更新恢复测试；历史框架包和报告保持不变。完整目标继续开放，下一阶段仍包括未知执行对账、质量比较、其他后端及配对发布验收。
