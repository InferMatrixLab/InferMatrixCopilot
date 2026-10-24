# 当前 Codex 原生沙箱控制 — 2026-10-16

精确2d27候选在真实 Docker 上通过本地与 SSH 两种 location，使用同一 registry／Codex adapter／sandbox backend。基于保留的 digest 镜像只安装当前 wheel，生成两个新镜像；原镜像、环境、缓存、proxy、凭据和付费／未知账本不修改。

每种位置均验证容器内当前 package 全部文件、Codex0.162.1 binary identity、Git/source 快照、非 root UID65534 和无 IPv4 routes。灵活策略镜像既支持保留 shell 的 review 配置，也支持 canonical restricted completion 配置；带 shell 强制要求的镜像可通过正向配置控制，但与 restricted policy 冲突时被 canonical 校验拒绝。使用真实配置 RPC，只有 initialize、config/read、configRequirements/read，没有创建模型线程／turn。

独立新解释器还在移走并备份本轮四个 SSH 控制结果缓存后，使用原 location／release／profile／binding 取回相同结果。恢复后的缓存字节与原备份一致，所有原 worker 文件字节不变，托管冲突仍拒绝。没有重新执行这些控制任务，更没有重放原付费／未知调用。

证据目录为 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/codex-current-controls-20261016/`，包含 Dockerfile、固定 catalog／source inputs、精确 wheel、构建／原生配置报告、实际恢复与原 worker 稳定性报告。所有测试使用新目录、新镜像及独立状态。没有产品／测试改动；现有2d27全量确定性整合证明保持原身份。

此补充证明当前原生配置、身份、容器隔离及零模型结果恢复，不证明模型推理质量、账户认证、真实模型工具调用行为或完整 cancel／resume／partial-output 矩阵。未挂载账户凭据，served model／invoice cost 均保持 unknown，没有额外模型费用。完整目标及其他原生验收仍开放；其他厂商按用户决定后验，没有 commit、push、改 pin、发布或部署。
