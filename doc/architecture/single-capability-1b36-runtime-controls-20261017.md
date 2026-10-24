# 当前1b36配对：实际 local／SSH Docker 控制

框架阶段保持收口。本次只验证当前Copilot `1b36ed44…`／ReviewBot `2db4f086…`的运行环境，不改生产代码／AST，不调用模型，不挂载账户凭据或proxy。当前镜像为 `sha256:7656aac2801a341b150646a2629e7cddcbf5d681aca9afeb1ecb7fc750ac027e`。

同一通用sandbox profile／Docker backend分别选local和SSH，实际执行Python子进程。stdio bridge有效／篡改manifest检查、流式stdin／stdout／stderr及伪receipt文本保留均通过；两侧idle、absolute、cancel、进程树清理、原结果读取、防重复execute、reservation前取消无binding均通过。

初始pending观察有2项，只读取并校验同一原worker结果；最终结果已知、原binding不变，未替代执行。这些合成进程凭证不用于结算旧真实模型unknown。原结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-native-controls-20261017T105623Z/result.json`)、原pending及后续读取 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-native-controls-20261017T105623Z/original-pending-observations.json`)、证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-native-controls-20261017T105623Z/current-pair-controls-review.tar.gz`)。

本轮首次即使用独立用户目录保存local fixtures，与root-owned execution state分开；原始probe未修改，所有原执行进程均终态exit0。此前6a8上传失败及原始错误仍在历史档案，不改写成当前结果。

当前镜像内2337个产品成员、原生CLI0.162.1／binary hash，以及source／controller／remote／container策略摘要一致的证明位于当前整合目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-integration-20261017T105136Z`)。namespace实际补验、全新最小安装、public SDK／消费者契约及安装主流程已通过；双仓完整源码套件仍在原进程运行，本报告不声明全套通过。此前6a8全量结果不绑定本版本。

完整双仓代码和wheels参见本版本原交付 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/stage-completion-20261017T103645Z/stage-completion-review.tar.gz`)，各补丁对原647e6e02／1e70e983应用一次，不叠加旧overlay。本补充没有commit／push／发布或部署。完整目标和质量、其他后端、深层恢复、最终release／pins门禁仍开放；原519全部分配、闲置和unknown保持。
