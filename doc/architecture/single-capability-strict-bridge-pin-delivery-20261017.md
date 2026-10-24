# Strict输入捕获与native bridge知识固定 · 2026-10-17

框架保持收口。当前本地候选配对为Copilot `e98e4c08…`／ReviewBot `24ac7cfb…`；相对2ddf只改三个Copilot产品成员，前次Direct修复保留，Bot未改。

## 捕获范围及原阻断

两组真实PR的原baseline durable workflow通过测试CLI保存了首次完整SDK交付文本、argv、bridge spec、输出合同和权限。当前默认本地Codex在模型前正确阻止：`required output-repair completion unavailable: codex does not support required capability builtin_tools_off`，原两次结果均blocked、捕获数0，未改写为成功。

为检查应用渲染，另建明确的独立测试composition：tool-free repair接口使用只捕获／拒绝的fixture client；primary仍经真实SDK的Codex transport走向capture-only CLI。没有修改Codex.validate_completion、registry capabilities或真实账号准入；这不是现实Codex profile已经可用的证明。fixture未生成模型回复；workflow的failed／interrupted状态原样保留，不能称为review done。原fixture没有作为新测试的恢复或替代运行。

新安装候选与原baseline的首step都为investigator，13个输出合同字段及其内容一致。raw首次文本分别保留：baseline 90950／68011字节，当前101698／73649字节；路径、status guidance及frozen PR上下文差异不强行归一。这是SDK到CLI的完整首次handoff，不含vendor内部system/tools，也不覆盖后续repair／完整workflow或真实模型质量。

## 从bridge spec发现的实际缺口

普通host native路径的bridge spec没有选定knowledge_dir。CLI会过滤KNOWLEDGE_ROOT／KNOWLEDGE_DIR，bridge子进程可能回退到打包树，丢失run已固定的快照。已配置sandbox的freeze_session原本会写映射路径，普通路径却没有同一绑定。

现在唯一tool_bridge.write_bridge_spec接受可选knowledge_dir，由应用传入并记录解析后的物理路径。所有两个生产调用方——providers.run_harness_step和内部rebase module——都已迁移。reader仍使用已有KnowledgeView.for_root和同一doc_read／manifest校验；没有新建知识解析器、provider或业务循环。旧调用省略字段时保持原wire；sandbox继续用原codec校验并映射该字段，writable/rebase sandbox限制不变。

## 已执行验证

命令、原结果、完整首请求与当前wheels (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/strict-first-request-capture-20261017`)。

- 88项相关源码测试通过，无fail／error／skip：producer／reader、原知识快照在环境清除／切换后仍固定、篡改拒绝、rebase／sandbox session及kernel边界、required-repair预检。
- 真实安装MCP stdio回归：使用真正providers.run_harness_step产生spec，在child的两个knowledge环境key均省略的情况下调用真实MCP doc_read。旧2ddf不能读到所选fixture页；新e98e返回正确固定内容。初版测试用空字符串而非省略的结果另行保留；最终omitted comparison是本结论依据。
- MCP对照环境复用依赖、不是fresh；两边安装的2337个产品成员均与相应wheel匹配，无源码路径注入。另有新venv的离线hash锁定最小安装，未装MCP，其membership／pip check通过；不把这两个安装范围混称。
- 当前真实两PR的首request与bridge再次捕获：readonly scope保留，knowledge_dir都绑定同一原snapshot/knowledge；旧baseline没有该字段。新probe的workflow仍为interrupted（capture CLI主动exit78），不是审查成功。runtime的unpriced fixture记录未手动改写为已知费用。

当前三个成员是tool_bridge.py、providers/__init__.py、rebase_engine/module_rebase.py。production-delta.json提供相对2ddf的行数／AST；相对原1b36共五个src成员变更。未以测试数量或净减行作为整体完成度。

## 交付与剩余目标

完整双仓补丁、当前配对wheels与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/strict-first-request-capture-20261017/strict-bridge-pin-review.tar.gz`)，各full patch对原647e6e02／1e70e983应用一次，不叠加旧full patch。代码tree、wheel／patch hash及private-index重建见code-identity.json；真实index未变。

没有真实provider调用、模型回答、GitHub写入、commit／push或部署。原519／spare、旧unknown和旧执行环境不变。2ddf及1b36原验证保留其确切范围，不能改名为e98e原生验收。

下一步仍须为当前release准备实际sandbox completion／session profile、image／认证／配置及预算准入，完成后续阶段输入、完整独立labels／容差和十层真实质量；深层恢复、其他vendor及最终release／rollout保留。未配置Codex的无工具completion能力限制仍然有效，不能用测试fixture绕过真实准入。完整目标未完成。
