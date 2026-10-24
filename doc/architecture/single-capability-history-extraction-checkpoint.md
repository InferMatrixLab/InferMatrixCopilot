# P0：History提取防重修复与其余generation连接 · 2026-10-17

历史框架交付归档不改；本轮发现新的防重缺口，按用户“可能重复派发立即修复”的要求处理。**History修复已正常安装验证；Skeleton同类缺口已复现，公共generation迁移仍未完成。** 不把本切片称为全部防重或最终框架／发布验收通过。

## 复现与修复

在真实安装的7095 run_stage和ModelGateway中，只有transport边界模拟：第一次调用明确native_call_sent=true、completed=false、pending=true。History恢复后再次进入相同PR，并继续后续PR，模拟调用从1增到4；旧reserved额度仍在。没有真实厂商调用或实际新增付费。

History的_extract_one原来直接generate，只有成功返回后才保存pending。现已删除这条无检查点的生成→pending直通路径，改为委托既有quality_review.checkpoint_review。按PR保存提取状态，绑定批次digest、number、merge、pin、generator、原system／prompt哈希及payload策略；意图在调用前保存，结果在返回业务逻辑前保存。共享knowledge_dispatch上下文记录原生归档身份，不增加provider／预算／重试实现。

新未知调用保持原意图，恢复不再调用transport；已证实的调用前拒绝仍可重试同一单位。旧批次若没有提取意图、尚未完成且已有reserved额度或订阅派发歧义，在读取／模型工作前拒绝，原checkpoint字节不变；须保留原环境并对账，不能臆造原意图。已有解析pending继续消费、完成批次缓存与常规预算上调路径保留。没有修改SYSTEM_HISTORY或模型归属规则；scope文字修复暂后置。

## 当前验证与代码

代码／报告目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-extraction-checkpoint-20261017`)包含changes.patch、before、私有索引重建、wheel、可复现命令和原失败日志。patch仅对7095的两个变动文件应用一次，含新增测试。生产变化1文件、+26行／+315 AST节点；其他wheel payload逐字节未变，Bot3bbf未改。

当前wheel SHA256 `b3337bfd0b82c687d581715048184c7d31ffa9e51e08be846eff3bca60cbb845`。独立正常安装275项零失败／错误／skip，含API／subscription未知结果不得重派、旧歧义状态原样拒绝、真实调用前拒绝重试、History／共享质量、预算原记录恢复、portable、SDK Direct／Strict及发布资格。精确验证Copilot2335与Bot128成员，无相邻产品src／产品源码路径注入；依赖复用，不称fresh最小安装。

源码原选择85通过、1项测试模拟错误：所谓“调用前失败”放在transport.complete内部，规范机制保守认定可能已派发，正确保持pending。原报告及测试保留；把拒绝放在transport构造前，当前5项回归加24边界共29通过；最终安装选择覆盖当前完整相关选择。第一次无隔离build因测试env无hatchling失败，正常隔离build成功；不改原环境补装、不将失败日志改绿。

## 尚未完成的公共连接

同一原始7095中，Skeleton未知调用恢复后模拟调用从1增到2，仍会替换原未知调用。这是已确认P0，不延期为普通深层恢复细节。History wheel只改了History，不能宣称也修复Skeleton。复现脚本／结果在未知调用复现目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-unknown-reproduction-20261017`)。

下一项将Skeleton及其他尚未保护的init generation调用收敛到同一应用层检查点入口，删除无检查点路径和各调用点重复包裹；已由Foundation／Discovery等任务journal拥有的派发须先核对scope，保持其原身份和恢复权威，不叠加第二套业务循环。已枚举call_discovery_model、_Knowledge._sections、_KnowledgeDepth._attempt、_Modules._card、_Stage._rules_call、_Skeleton._map_call；只有Skeleton已在本轮另行证明缺陷，其余逐项确认已有意图后迁移。

完整目标、原unknown和全部439额度含闲置保留；本修复没有新增真实模型调用、commit、push、部署或release。真实质量仍保留7095 Foundation／History失败样本，不把本次确定性防重通过当成模型质量通过。
