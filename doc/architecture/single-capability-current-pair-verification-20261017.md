# 当前配对发布前准备：完整源码验收仍待结束

本报告冻结于2026-10-17T11:03:15.851099+00:00。框架阶段已收口；完整目标仍开放。本轮没有生产代码／AST变化、模型调用、commit、push、release或部署。

## 当前已完成

Copilot wheel `1b36ed44ab9fc649ec364aa51c0747f25aeda4e9c07cf024366db721910309a9`与ReviewBot `2db4f086ebc5549ff9ad4c11e6d9402679f7524e0171c8dee775d69fe21d8175`已在源码外正常重装。SDK／Direct／Strict／宿主恢复及发布准备的必要安装主流程通过，产品2337／129成员与wheel逐字节匹配。

全新venv的最小消费者安装通过：从离线cache解析依赖，生成hash lock后实际安装，pip check通过，无MCP、无site-packages依赖软链、无产品src注入。public SDK schema与curation checkpoint预检通过，未预留模型工作。实际运行消费者的verify-release-sdk脚本通过，包含签名containment策略和公共API能力协商；不是伪造release manifest或修改部署pin。

Personal-Agent安装同一wheels，实际namespace／资源检查通过。镜像 `sha256:7656aac2801a341b150646a2629e7cddcbf5d681aca9afeb1ecb7fc750ac027e` 的包成员、原生CLI0.162.1／binary hash、source／controller／remote／container工作流策略摘要一致。[同一镜像的local／SSH执行控制](single-capability-1b36-runtime-controls-20261017.md)通过，原执行的pending只按原worker凭证读取。

完整评估434项的原成功证据复用自上个切片；本轮逐文件比较当前eval源码和当前已安装产品，确认同一输入。没有为凑数量重复运行。

## 完整源码仍待验收

最初整合启动会话30670终止，工具返回143；核实原Copilot／Bot PID均已消失。Copilot没有完整JUnit；Bot原JUnit2862项无失败／错误／skip，但外层退出记录缺失，不把它当成已观察exit0。原因不推断，原日志保留。

确认原进程已停止后，用同一不可变快照重新执行纯fixture套件：独立session的监督进程复用Copilot run_process／file_lock／immutable writer，实际子进程退出后才写terminal receipt。它们不运行真实模型，不涉及旧付费unknown的重派。监督PID为Copilot4022895／Bot4022896，原spec、started记录、日志及最终receipt都在durable-source-attempt (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-integration-20261017T105136Z/durable-source-attempt`)。本报告观察时尚未声明完整双仓通过；后续只观察这些原监督进程，不能因观察超时重新启动。

准备结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-integration-20261017T105136Z/preparation-result.json`)、本轮准备证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-pair-integration-20261017T105136Z/current-pair-preparation-review.tar.gz`)。完整可审阅代码仍是本版本双仓补丁 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/stage-completion-20261017T103645Z/stage-completion-review.tar.gz`)，各对原647e6e02／1e70e983应用一次，不叠加旧overlay；本轮补充的是验证材料。

## 后续门禁

先等待上述原监督进程的terminal receipt和完整JUnit，校验快照未漂移。真实模型整例质量、原519外层ack及此前unknown、深层恢复／预算、其他后端、最终不可变发布与pin／在途保留／rollout仍分别开放。当前准备通过不等于发布批准；原519分配与闲置均保持。
