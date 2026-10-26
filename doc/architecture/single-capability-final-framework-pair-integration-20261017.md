# 最终框架配对的确定性整合 · 6a8e / 2db4

后续补充：[当前同一镜像local／SSH执行控制已验证](single-capability-current-pair-runtime-controls-20261017.md)。下文镜像过程矩阵“未执行”描述保留本整合包封存时的范围；新补充包单独记录实际结果和原始上传错误。

框架保持[已交付](single-capability-framework-handoff-20261017.md)。当前精确配对的完整源码／评测套件、安装主流程及环境补验已取得终态；完整模型质量、深层恢复及发布目标仍开放。

## 当前整合结论

Copilot原PID3044153／ReviewBot原PID3044154／评测原PID3044155全部由原coordinator session37954收取exit0。原JUnit分别5974／2862／417项，无失败／错误；Copilot原始4个环境skip保留，Bot／评测无skip。对应同名、同源码用例已在当前精确wheels正常安装、Personal-Agent真实namespace／资源检查补验通过，raw_source_no_skips仍为false。数量只界定实际验证范围，不表示全部目标完成。

逐项结论 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/deterministic-closure.json`)／原命令与进程终态 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/process-results.json`)／完整当前代码与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/final-pair-integration-review.tar.gz`)。

所有输入从封存完整补丁重建，原Git tree一致。运行前后391／178个产品与release输入与当前工作树逐字节匹配，suite snapshot的tracked输入无变化。本轮生产代码／AST改动为零；全套启动后仅改架构文档／索引，不重建产品来回避原结果。

## 安装、namespace与镜像身份

当前wheels：Copilot6a8e3339ba48cce5337b7e2ff9a497ed39bbd0420a534d773bbee4bb293ffc16；Bot2db4f086ebc5549ff9ad4c11e6d9402679f7524e0171c8dee775d69fe21d8175。此前当前安装主流程138通过＋16个Strict fixture补齐后通过、Bot205通过，保留原错误和明确补验范围；本次全套另行执行，不替代或改写旧安装日志。

全新最小安装 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/minimal/result.json`)通过：无MCP、无源码注入，依赖从保留缓存按hash锁安装，pip check通过，2337／129个产品成员与wheels精确匹配，安装site-packages无软链。初版新增SDK探针漏设受管knowledge fixture，原错误保留；补齐fixture后在同一新安装环境通过，无重装／产品修改／provider调用。不是含全部依赖的离线installer。

Personal-Agent当前安装及真实namespace (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/namespace/result.json`)5项无skip，验证原始四个source skip及实际producer namespace，未挂载凭据或调用模型。远端依赖复用，不称fresh安装。三个wheel-only用例亦在控制机当前正常安装环境单独通过。

当前独立测试镜像`sha256:c66c425a539cdb1977c75f9ca36284e8b048ac2e4ed6e19a7f4c6a0252dc8196`已离线构建。远端正常安装和容器各2337成员匹配当前wheel；CLI版本／hash、source／controller／remote／container policy digest核对通过。没有账号／proxy挂载或模型调用。**本轮镜像检查只证明包、CLI和policy身份，没有执行当前镜像的完整local／SSH流式、取消与deadline矩阵。** 上轮749a／41cc的原Docker raw absolute失败和原容器receipt对账仍按原范围保留，不冒充当前镜像全套。新live manifest准入前仍需当前镜像控制检查。

## 归属、恢复和质量范围

[机器清单](single-capability-framework-inventory.json)仍为70能力／37删除记录、无登记中的框架待接连接。Copilot拥有provider／review／knowledge／共享质量机制；ReviewBot拥有调度、GitHub业务、发布／回执及宿主账本。各init共享机制，保留不同验收策略。

当前恢复合同 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/recovery-contract-current-evidence.json`)18项均匹配冻结test字节、实际参数实例与当前完整JUnit。新增host范围覆盖真实子进程退出后的生成intent保留／结果复用、已知未派发的有界重试、输入漂移拒绝、Git事务交接及最终stop barrier；不把fixture当真实GitHub一致性或全部深层恢复证明。旧真实unknown没有新证据，不重复旧Strict lookup，不触发替代模型调用。

十层质量核对 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/remaining-quality-acceptance.json`)保留历史证据hash与范围；当前artifact十层整例native质量仍未完成。Foundation／History原失败、独立家族要求、其他后端后验、原未知结果与499及更早全部分配保持。本轮模型调用与预算变更均为零。

## 可审阅代码和后续

完整补丁／代码身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-framework-pair-integration-20261017T085721Z/code-identity.json`)分别对原647e6e02／1e70e983应用一次，不叠加旧overlay。原source suite输入tree和最终报告tree分别记录；差异仅架构文档。真实Git index保持不变。生产行数／AST统计沿用逐字节相同的当前框架代码范围，见包内production-delta.json；本轮代码净变化为零，减行不作为门槛。

[主计划](single-capability-completion-plan.md)保留全部要求，旧977行时间线在[历史文件](single-capability-completion-history-through-framework-20261017.md)完整保留。本阶段关闭当前精确配对的上述确定性整合缺口。下一阶段继续受控原生质量、深层恢复／预算回执、后端认证及最后release／在途保留／rollout验收。未commit、push、创建PR、部署或真实发布；完整目标未完成。
