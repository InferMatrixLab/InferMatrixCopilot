# 当前沙箱真实进程控制 — 2026-10-16

当前2d27精确包通过真实 Docker 的 local／SSH 同一接口，执行不调用厂商模型的 Python fixture。镜像复用上一独立 Codex 控制阶段的新 digest；原生产环境、付费／未知执行和账本不修改。

流式 stdout／stderr、stdin 和伪造的 receipt 前缀验证通过：伪造文本仍保留为 native stderr，不能冒充 worker 终态。空闲超时保留部分输出，并清理忽略 SIGTERM 的子进程；绝对超时及收到输出后的取消均终止原容器。

local 直接观察到停止结果；SSH 在控制连接超时／取消后先返回明确 pending（禁止 fallback），再从原 worker 取回结果。最终两种位置的返回码／reason 一致，streaming／idle 的全部输出一致。只有原停止结果被恢复，没有新执行授权。每个已绑定 case 再执行均拒绝，binding 字节不变；两个位置各只有四个 immutable start-intent。派发前取消没有 binding。

Docker terminal receipt 证明容器停止；它不是模型回复成功、费用结算或发布资格。此阶段没有发起模型调用、挂载凭据或使用原未知调用。实际厂商推理／认证／取消／resume／usage 以及完整崩溃／发布回执和质量验收仍开放。

证据位于 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/sandbox-process-conformance-20261016/`：同一 probe、两位置原始结果、typed parity、start authority、controller／worker 原始回执及精确 image／wheel 绑定。没有产品或测试修改，既有2d27完整确定性整合证明保持原身份，没有 commit／push／pin／发布／部署。
