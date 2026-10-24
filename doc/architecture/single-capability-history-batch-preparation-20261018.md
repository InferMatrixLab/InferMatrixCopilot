# 当前比较的PR-history源批次与显式留存准备 · 2026-10-18

框架保持收口；产品仍为Copilot a10c3484…／ReviewBot24ac7cfb…。本轮没有生产代码／AST变更，没有构造vendor transport或调用真实模型。

## 应用入口实际冻结的内容

分别使用原647与当前a10安装SDK，经现有prepare_world／run_stage进入pr-history。前置stage使用既有scripted gateway；被测history的ModelGateway transport_factory明确拒绝构造任何transport，错误携带native_call_sent=false、native_outcome_pending=false，不产生新的未知付费结果。新测试目录与原运行完全分离，没有重试或改写旧paid unknown。

两侧应用均冻结PR101／102的选择和相同upstream pin f7992baf…；选定source／完整PR evidence内容的共同hash为32fb96745ddb9ea8c685c94b989a2e9844a46698e51a5ba86dfbb1bd48aba315。每条真实应用选择对应的fixture source evidence均保存，未来主manifest可绑定其原始bytes。

这两条PR是已有source-authored合成case，不是新真实GitHub采样或盲测。两个知识base SHA不同（各自validator wrapper在初始clone内不同），完整请求的渲染／scope policy也不同；没有把它们规范化后宣称整个model input相同。

## 默认拒绝与显式留存

当前a10在mode=omit时，canonical completion guard以“durable native accounting requires retained request/result evidence”拒绝；transport_factory调用0，应用model_call trace0。负向控制的审阅包仅保留请求hash，不能将控制宿主的采集等同于生产留存。

mode=retain-private时，当前SDK与baseline各到达一次已知的factory拒绝，未构造transport。当前SDK向ModelGateway传record_payload=true；旧SDK仍传false，这个历史差异保留。baseline需要现有legacy host的显式request/result留存适配；本轮只在宿主明确私有采集该边界的请求，不修改旧SDK，不宣称已执行真实reply留存或恢复。

控制使用注入的零价格测试值以避免模拟域预算消耗；它不是Codex真实价格／费用声明。真实transport从未构造，原519／spare及旧账本未修改；测试trace中的未知usage不改写成真实零账单。

## 留存策略的可复用原型

已生成shadow-retained-inputs/1清单，绑定18个源批次／阶段／fixture与validator输入。retention-control根由当前用户拥有、mode0700，prototype config为execution_kind=fixture、publication=disabled。它们不是live run_paired manifest，未创建native ledger或stage派发。

对两臂配置调用同一个arm_runtime.validate_payload_retention：显式策略通过；缺省策略返回None、不会推断授权；另存的篡改input被拒绝。没有修改原冻结文件来取得负向结果。该完整性清单包含宿主helper依赖，不等于把这些文件全部授权mount给模型。

## 后续连接

源批次与留存原型已准备，但仍需纳入新配对主manifest，并绑定实际worker source／knowledge／schema roots、私有标签隔离、实际profile／proxy／credential context及新预算。后续完整模型请求／回复的持久保存、实际account admission和整例质量尚未运行。baseline的legacy host留存、完整batch恢复也仍按其既有路径验证。

冻结批次、应用checkpoint、策略原型与验证归档 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-history-batch-20261018/a10-history-batch-review.tar.gz`)只供宿主审阅；不要把整个控制根或其private子目录转发给worker。模型输入只能显式选择冻结源packet，不能包括checkpoint／控制结果／私有标签。

[当前完整确定性配对](single-capability-a10-full-paired-integration-20261018.md)保持有效；182调用仍为未分配提案，完整十层、深层恢复、其他vendor与最终发布目标不删减。没有GitHub写入、commit／push或部署。
