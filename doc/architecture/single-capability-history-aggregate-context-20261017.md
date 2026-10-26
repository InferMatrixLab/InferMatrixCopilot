# 聚合审查的固定源码上下文：已实现，未重新派发模型

519真实结果定位了提取→聚合证据断连。本切片修复该连接：新History v5保留每个已采纳PR的可见源码路径／范围／hash，聚合从原pin物化相同上下文，包括context-only tests。上下文单独标记不得作为规则引用、未声明执行测试；原CitationPolicy和owner门槛不放宽，未添加模型或宿主repair循环。

## 唯一owner与迁移

Copilot knowledge_service.pinned_claims.evidence_text／_evidence_chunk统一范围／hash校验和文本物化。check_evidence、History规则证据、History review_context和Harvest excerpt调用它；删除这三处旧独立主体。Harvest保留“读取新pin内容、不要求旧hash相同”的比较策略；缺文件／越界与底层Git读取失败仍区别处理。新聚合用context摘要绑定审批和shared checkpoint，policy version2，损坏hash、越界、缺context或超限在judge之前阻断，不截断完整审查。

只新增新批次v5输入合同，提取system仍用v4。保留0–4渲染／请求及unknown身份；新上下文metadata不保存raw文本。原6a8e／C66／519运行环境保持，不在原环境原位安装新代码，不改原结果或账本。

## 验证与代码

完整双仓补丁、当前wheel与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-aggregate-context-20261017T100446Z/history-aggregate-context-review.tar.gz`)／验证范围 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-aggregate-context-20261017T100446Z/validation.json`)／owner／迁移／删除 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-aggregate-context-20261017T100446Z/ownership-deletion.json`)／代码身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-aggregate-context-20261017T100446Z/code-identity.json`)。

源码定向／边界108＋46通过；当前正常安装211通过。Bot安装首次90通过，7个fixture／源码检查文件缺失；初次补验在fixture拷贝完成前执行也失败。静态确认文件齐全后同名7项通过，原错误全部保留，未以改产品修fixture。旧请求比较初版也漏带validator fixture，补齐后原6a8e和新wheel各实际执行0–4，每版本3提取＋1aggregate系统／prompt／record_payload字节完全一致。以上为脚本模型边界，不是真实质量证明。

测试覆盖上下文到达聚合、不能进入rule_evidence、仅保存范围hash、完成后无调用恢复、坏hash／路径／范围／缺失、大小上限、脏工作区不能替换pin、Git读取错误传播、legacy未知不重派及冷导入／依赖边界。wheel正常安装，无产品src注入；依赖复用，未称fresh安装。产品成员集合不变，仅3个Python成员变化，均匹配当前源码；Bot wheel未改变。当前Copilot SHA256`17a2f24efd2dfe5b28aaf5f3e042b470a2b36914a01aaf67440292b41ce83f0b`。

生产3文件净+55行／+415 AST节点，见repair-production-delta.json；完整补丁含全部此前保留工作，各对原647e6e02／1e70e983应用一次，不能叠加旧overlay。没有commit／push／真实发布或部署。

## 仍开放

519原失败保留：本次只证明上下文连接／行为合同，尚未证明真实aggregate会接受新输入。快速入口lead仍待集中修复；基线外层stage ack仍pending，未伪造成功或重跑原工作。其他质量／恢复／后端及最终发布门禁继续保留。框架交付保持封存；当前新candidate尚未执行完整配对／实际远端或新模型比较，旧全套不重绑定新wheel。模型调用0，不复用519闲置或自动追加同例比较。
