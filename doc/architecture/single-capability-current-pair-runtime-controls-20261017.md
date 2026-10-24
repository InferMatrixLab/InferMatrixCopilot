# 当前最终配对的真实local／SSH执行控制

当前Copilot wheel6a8e／Bot2db4及已封存的[完整配对整合](single-capability-final-framework-pair-integration-20261017.md)保持不变。本次独立补验镜像`sha256:c66c425a539cdb1977c75f9ca36284e8b048ac2e4ed6e19a7f4c6a0252dc8196`，没有生产代码／AST变化、模型调用、凭据或proxy挂载。

## 实际结果

同一Copilot通用sandbox profile与Docker backend分别选择local和SSH位置，真实执行Python子进程，不运行vendor模型。两侧原始probe均通过：stdio bridge有效／篡改manifest、流式stdin／stdout／stderr、保留伪receipt文本、idle／absolute deadline、cancel、清理终止、原结果读取、防重复execute，以及reservation前取消不产生绑定。

SSH absolute／cancel初次返回SandboxPending，随后在同一原执行对象读取可信worker receipt，两个原结果均已校验恢复，原binding hashes不变，重复execute被拒绝。本地四种process情形初始均已知。不是替代执行，也没有用这些合成进程结算旧真实模型unknown。

原始结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-pair-native-controls-20261017T092152Z/result.json`)／原pending与原结果对账 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-pair-native-controls-20261017T092152Z/original-pending-observations.json`)／完整证据补充包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-pair-native-controls-20261017T092152Z/current-pair-controls-review.tar.gz`)。

## 运行器错误保留

初始driver在SSH检查之后上传local fixture失败：SSH worker已创建root-owned状态根，普通用户SCP无权写入，local probe尚未启动。原driver exit1和SCP错误日志保留。后续只把fixture放入独立的用户目录，未chown／改写原执行状态，未重跑SSH；随后原local检查首次执行通过。initial_driver_all_passed仍false，all_executed_process_probes_passed为true。

镜像包身份、CLI0.162.1／binary hash和source／controller／remote／container policy digest沿用当前整合内的精确证明；这里实际使用同一C66镜像补上过程控制。旧749a／41cc raw absolute错误及原容器对账不改，不把旧结果改名为当前版本。

## 对完整目标的影响

关闭的是当前镜像的上述真实执行控制缺口。当前框架及全套交付继续封存。全新最小安装、完整套件、实际namespace和资源补验见原精确配对记录；本次391／178产品／release文件hash重新核对未变，无需因报告变化重复全套。

真实模型质量、独立标签／模型家族要求、深层恢复／精细预算回执、其他vendor认证、最终不可变release／pins、原生产在途保留及rollout仍开放。499及此前所有分配、闲置和旧unknown保持。本次不准入新模型比较，不发布或部署。
