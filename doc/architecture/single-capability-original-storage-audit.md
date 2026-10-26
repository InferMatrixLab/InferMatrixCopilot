# 原未知执行的存储证据检查（2026-10-17）

只读检查 Discovery 原容器的 Docker mount、tmpfs、rootfs diff 与原远端执行目录。容器根文件系统只读，唯一 tmpfs 为 /tmp，两个主机 bind 均为只读模型套接字；没有 host-backed 会话或缓存挂载。rootfs diff 只有套接字挂载点。原远端目录只有 request、execution-profile 与 cancel-intent，没有终态响应文件。未找到此前设想的可持久读取的 native session/cache 来源。

Strict 原未决 native identity 的本地目录存在但为空；只读打开原 ledger 前后数据库与现存 WAL 哈希一致。该检查没有提供新终态。没有读取其他账户全局缓存、调用 result/recovery RPC、重放模型请求、启动容器、改动原目录、取消原进程或修改结算。

这不证明从未派发、未扣费、已成功取消或模型完成。两笔原 native outcome 继续 unknown；不能用容器 exit137、目录为空或缺响应把它们结算为成功或零用量。SDK 层 Strict interrupted 与 native outcome unknown 是不同层次事实，继续分别保留。后续只有出现可信原调用终态／计费证据或明确人工处置，才能改变该结论；重复轮询相同目录没有新增价值。

证据目录：`output/single-capability-completion-20261012/original-storage-audit-20261017`。完整重构目标仍开放；此检查不批准新质量派发或扩大冻结额度，也不阻止准备独立的新评估协议及其调用上限。
