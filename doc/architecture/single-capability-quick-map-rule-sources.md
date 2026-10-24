# 新规则的持久源码入口

原fd8b真实比较的候选因quick-map缺源码路径被aggregate阻止。record.evidence中已有pin校验通过的pkg/revisions.py，但共享renderer只扫描正文反引号。原真实结果保持blocked／整例未通过；这是后续P1数据连接修复，框架封存不重开。

唯一格式化实现是kb_service.init_quick_maps.with_rule_sources。共同Stage._screen在_evidence成功后、事实检查／质量审查／放置前，为新候选补充缺失的`- 源码：`引用。已有行内路径保持原字节，不用owner前缀猜来源；重复来源只写一次。引用进入新规则正文，因此后续阶段无原init记录时也能重建索引；同一正文同时进入placement容量／spill和最终render，无需维护第二套来源或映射循环。原range/hash仍留在证据记录中，不把展示引用当新的信任凭证。

原parser扩展为识别明确源码行中的root文件，复用知识格式的CodeSpan与safe_source_path约束，不能把不可表示路径静默显示为另一个路径；table中的pipe做Markdown转义。已有无证据规则仍显示缺省空来源，历史规则不回填，手写map不改，owner routing／signals不因模型建议自动修改。普通cards／解释性facets／Discovery不写这些新规则，保留原验收及渲染政策。v1 curator的append-only实现未改。

History新批次显式绑定输出策略v3，提取system及citation descriptor沿用v2。保留v0／v1／v2批次选择旧渲染；兼容选择只开关同一formatter，不保留独立业务循环。旧unknown和原运行环境不能被新策略改写或替代。其他规则写入阶段共用Stage入口。

## 验证范围

最终Copilot wheel fe4fb40994f6e8374e51f134fd8ec6b89fb7b5e828501ab391ff82749caeb624，Bot仍3bbf71df48a24eb05f6d07cb68fe145f8c2cd15b22f17a2f182dba4a7d001860。final安装payload逐成员及源码Python字节一致；正常安装不注入产品src，依赖复用，不称fresh最小消费者。

- 最终源码渲染／生命周期／容量与spill／History2和3主流程通过，最终源码依赖边界通过。
- 初次a52a版本的全部受影响安装选择270项通过。随后只增加拒绝会被code-span trim改名的边缘路径检查；逐payload比较确认最终wheel仅init_quick_maps.py不同，最终安装对渲染、placement、legacy2／新3流程及原始系统兼容再次验证通过。不是最终新全量配对验收。
- 冻结旧前置渲染后，v0／v1／v2的三提取＋aggregate实际请求与fd8b逐字节一致；这不等于声称新默认重新生成的前置页面也与旧版本相同。
- 最终安装代码回放上一轮实际三条提案，逐项重验pin证据，TOY-I3的`—`变为`pkg/revisions.py`；已有规则字节、footer、路由及原实际artifact保持。此为确定性render回放，不是v3原生质量验收，未复用paid账本或旧闲置。

原始失败日志保留：最初测试新夹具过短和追加分隔空行断言不当、legacy测试把前置页面的来源行也视为History改写，均调整为实际合同；最终安装初次误选需要src目录的源码AST检查，保留missing-file失败原报告，源码检查在真实checkout运行，安装行为独立重验，不添加src注入。精确命令、JUnit和更正范围见validation.json及原日志。

生产增量3文件，净42行／317个AST节点，相对fd8b，含formatter／共同调用点／History兼容策略；不以净减行为门槛。没有新增model、vendor、repair循环，Bot未改。

代码与验证目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/quick-map-rule-sources-20261017`)包含当前双仓完整patch、wheels-final、源码／安装身份、实际回放和旧请求证明。wheels目录中的a52a仅为前一轮验证证据；当前应使用wheels-final与installed-final-result.json列出的精确hash。完整patch各自相对原始647e6e02／1e70e983应用一次，不能叠加到已有变更工作树。

新版本尚未执行真实aggregate／完整原生比较；原479额度含闲置和unknown全部保留，不重跑旧coordinator。完整十层质量、深层恢复／预算／回执、其他vendor及最终immutable pair／pins／在途环境／回滚／发布仍开放。无commit、push、发布或部署，完整目标不标记完成。
