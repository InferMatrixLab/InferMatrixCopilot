# 发布边界的进程崩溃恢复补验

当前6629/B939安装包通过实际子进程退出与重启验证。Direct／Strict各覆盖：保存 artifact 后远端 effect 前退出；模拟 GitHub 事务提交但响应返回前退出；publisher 返回后本地测试回执未写入；本地测试回执已提交但确认未返回。每种场景恢复两次，模拟远端最终只有一条 review；数据库未设置唯一约束，重复 effect 会被直接观察。额外覆盖远端已接收、响应丢失后 head 前进，恢复返回 stale 且不重发。

产品代码来自当前安装包，子进程使用 -I 并删除 PYTHONPATH；包内 RECORD 字节逐项复核。仅新增回归测试，未修改生产代码或 wheels。最初测试草稿漏传必需 review_checks，保留草稿失败 XML，修正后精确最终测试10项通过。

此证明只覆盖共享 publisher 的顺序重启及一致可读的远端标记。GitHub 使用持久化 SQLite 替身；本地回执是测试宿主表，并非完整 Bot job／attempt 回执结算。尚不证明真实 GitHub 可见性延迟、并发发布、完整工作流崩溃、实际模型质量或未知原执行结算。完整目标不因这项补验而通过。

证据目录：`output/single-capability-completion-20261012/publication-process-recovery-20261016`。新增测试：ReviewBot `tests/test_publication_process_recovery.py`。本测试在上一份 current-paired-review.tar.gz 交付后增加；旧包保持原身份，补验包单独提供测试源码与原始结果。
