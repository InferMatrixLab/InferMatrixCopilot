# Session完整输入准备的截止时间修复 · 2026-10-17

框架保持收口。本轮关闭一个真实主流程问题：带完整PR／知识输入的native配置和bridge准备被固定15／30秒窗口提前截断。没有缩小输入来取得成功。

## 当前配对与唯一实现

Copilot wheel `a10c3484d7b0ddaf26f17164af8accc53e0c96740564eb1f4664a87f7efd3379`，ReviewBot仍为`24ac7cfb…`；独立镜像`sha256:03dccf465b79ea99e1eeb8833478e738e3c1fabdd3fd5677d55f44d447af9317`。旧e98e／0085及更早付费环境未升级。

仅两个产品成员改变：providers/codex.py与providers/completion_profiles.py。相对e98e净+8行／+47 AST节点。configuration_probe接受可选absolute_deadline；应用组合传入同一个session绝对截止时间，bridge_probe也使用该截止时间。原生配置RPC仍保留13秒内部超时，非session省略新参数时仍为原15秒整体窗口。有限时间校验、能力／工具检查、原schema及原结果读取路径保留，没有新provider、工作流循环或自动重试。

## 原失败与修复验证

旧e98e使用真实PR5636的完整visibility-only冻结包时，起初发现其不含session bridge；两次控制脚本准备错误（缺bridge、缺required sealed_dirs）均发生在trace／模型派发前，原脚本与错误保留。随后用生产writer／freeze_session构造当前bridge并合并原完整输入，没有另写业务解析器。

正确组合的旧e98e在local配置准备阶段报execution cancelled before container start；SSH报响应未知／cleanup待对账。SSH按原执行读取后仍为pending且allow_fallback=false，原记录未改写或重发。两个路径均没有进入模型调用，控制pending不冒充新的付费模型unknown。

新a10使用同一原冻结包（155MiB量级JSON，原hash cb1680bb…），仅添加canonical只读bridge；完整PR source／host evidence及原1702份知识文件保留。生产prepare_completion_transport在local／SSH均通过：

- 实际Codex initialize／config/read／configRequirements/read接受gpt-6-sol／high与工具关闭策略，唯一允许MCP为infermatrix-tools。
- 真正bridge --check确认read_file／grep／list_dir及doc_read／doc_search可用；配置与bridge证明共同绑定同一release／image context。
- 最终session inputs哈希一致；session文件4471，连policy catalog共4472。具体sha见session-inputs-identity.json，不能把原包hash当成添加bridge后的hash。
- 两条configuration_probe trace记录，model_call为0，没有预留模型execution。控制专用attempt明确以canceled_before_dispatch结束，而不是声明review complete。

源码63项相关测试、额外bridge预算回归1项通过；agent预算断言修改后另重验2项。它们合计覆盖64项不同相关测试，不拿重复测试数量表示整体完成。安装后的controller、remote、container各2337个产品成员匹配当前wheel；依赖复用，**不是fresh最小安装验收**。新候选未挂账号／proxy、未执行模型；此前0085的离线login status保留原版本范围，不自动算新版本认证。

## 交付与未完成项

完整双仓补丁、精确wheels、增量patch、测试快照与证据 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/session-deadline-delivery-20261017/session-deadline-review.tar.gz`)。完整patch分别对Copilot647e6e02／ReviewBot1e70e983应用一次，不叠加旧full patch；另有明确标名的from-e98e-product.patch只用于与前候选比较。真实git index未变，private-index重建验证树一致。

大型原native-policy-request与worker记录保留在各自原执行根；归档保存绑定hash、proof和原路径，不重复打包其完整源输入。原包及独立labels可见范围保持；本轮不是整例模型质量比较。

下一步为新不可变配对准备profile／proxy与服务端连接准入、完整独立labels及预算后执行真实比较。十层质量、旧unknown／History外层ack、深层恢复、其他vendor及最终pins／shadow／rollout保留。新增真实模型调用0，原519分配及spare未动，未commit／push或部署，完整目标未完成。
