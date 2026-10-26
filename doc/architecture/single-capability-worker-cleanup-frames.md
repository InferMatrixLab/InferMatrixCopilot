# Worker cleanup frames (2026-10-16)

inner completion worker → controller 的诊断连接已补齐，复用既有 provider-neutral profile 和 process codec，没有 Docker/SSH 特化实现或新业务循环。

| 边界 | 唯一实现与兼容 |
| --- | --- |
| Worker producer | completion_worker.main 调用原 registry adapter；清理相关错误复用 process_cleanup_facts，输出一个 terminal cleanup 帧并以 1 退出；其他错误路径保留原行为 |
| Neutral decode | ProcessCleanupError.from_facts 调用同一诊断／ProcessOutcome validator；不重新采集 controller scope。无 native_process 时保留缺失，不把占位 outcome 伪造成原生完成 |
| 帧规则 | provider-completion-worker/1 的成功 event/result 格式不变，增加可选 cleanup 错误帧；只允许一个、必须最后、不能与 result 混用。未知／坏结构仍拒绝 |
| Receipt / reader | process_receipt 给 cleanup 明确 pending；零 CLI exit 也不能使其成功。执行与原结果读取共用 _raise_cleanup，传原 scope、部分输出并禁止 fallback |
| 账本 | 原生 completion、trace/attempt、应用状态继续使用前面已统一的机制；没有另一个诊断 schema、持久状态或模型实现 |

旧代码三个正向反例失败：本地／SSH 配置丢失 inner cleanup，以及有效 cleanup 帧被旧解析器拒绝。当前源码和精确 wheel 中三个均通过；重复、cleanup/result 混用、terminal 后追加事件、无效 PID 与多余 payload 均拒绝。成功/部分/旧错误 worker 用例保留通过。

新 Python producer 实际运行 main，registry adapter 为受控失败替身，不调用厂商 CLI。测试本地 BoundSandboxProfile 和真实 RemoteSandboxExecution / sandbox_agent 协议，SSH I/O 与 backend 用替身；远端读取先删除 controller 缓存，再从原 binding 执行 result 请求，实际 worker execute 总数仍为一。foreign boot/namespace、PID/birth、leader None、部分输出及报告用量在 controller 与 archive 中保持；只读原结果仍抛 typed pending，不重派。

当前 wheel 离线 hash-required 安装、pip check、RECORD／全部生产 Python 字节、公共 SDK smoke、Direct／Strict E2E 及以上 worker/profile/diagnostic 检查通过，无产品源路径注入。Copilot `3f272fc392f9df696699fc043f593d744af63e5e597743bdc3d59ac8f9649225`；Bot 仍为 `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。本切片生产两文件净 43 行／349 AST 节点。模型质量、真实 Docker/SSH 和厂商原生能力不能由这些替身测试推导。

证据：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/worker-cleanup-frames-20261016`。该目录 result.json / delta.json / 原始 XML 记录切片范围。整合代码包 consolidated-paired-review.tar.gz 包含相对最初 Copilot647e6e02／ReviewBot1e70e983 的完整双仓补丁和所有新增／删除；无需拼接旧 overlays，private Git index 重建证明见 patch-reconstruction.json。当前精确 wheels、离线依赖输入、架构／归属／迁移／删除清单及定向验证一并交付；历史包与原始报告不改写。

双仓完整源码核验已另启，结果目录 full-source-worker-cleanup-20261016；构建本交付时不预先声明其通过。当前安装验证是本切片定向集成，不冒充所有七组、真实远端隔离或最终发布验收。scope/PID/birth 是诊断，不是发信号或批准替代付费派发的权限。旧 worker 无诊断帧时仍按既有 incomplete/pending 路径处理，不能虚构缺失事实。

后续保留：完整当前整合与环境补验、worker 异常窗口/observer/stream-final 不一致、其他平台、精细预算/回执/恢复矩阵、原未知调用的原身份对账、真实质量、配对 release/pins 和部署。完整目标未完成；未 commit、push、发布或部署，无新模型费用。
