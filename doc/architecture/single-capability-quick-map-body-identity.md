# 来源展示与规则行为身份

当前修复继续上一切片的[持久来源连接](single-capability-quick-map-rule-sources.md)，框架封存不重开。初次fe4候选解决了路径丢失，但审查发现来源行改变了History的原文去重键。实际离线回归已复现：两个PR给出相同、不含行内路径的规则，第二个也产生升级提交；修复前断言[101,102]而非[101]。原失败日志保留。

init_quick_maps现在用唯一_source_row_paths解析器识别formatter生成的、仅由路径组成的来源行，render和rule_body_without_sources共同委托；可表示路径约束也只有一处。History v3只在比较行为时去掉尾部这类展示来源，继续以owner＋正文去重。正文中的行内路径、symbol、大小写、内部空格、非标准来源行和封闭代码示例保留。不同来源证明不应单独制造同一owner／同一行为的第二条规则；实际规则引用仍持久保留，不从页面删除。只移除比较键中的展示元数据。

旧0／1／2仍选择原始字面比较，兼容包装只选择策略并委托。没有第二个formatter、来源解析器或付费重试循环。新策略仍为未发布v3，完整package／workflow身份随源码绑定变化；旧unknown不重派。

最终wheel 34aa835be8db6df75b4fc35f8572cde8452efd4dc806340124954acd77280dd7，Bot仍3bbf71df48a24eb05f6d07cb68fe145f8c2cd15b22f17a2f182dba4a7d001860。全部payload逐成员验证，Python源码字节一致；相对fe4只有init_history.py和init_quick_maps.py变化。当前源码回归、安装行为及源码依赖边界通过，安装不注入产品src，复用测试依赖，不称fresh最小环境。原270项与fe4证明保留原范围，当前不是新完整双仓或原生质量验收。

具体覆盖：带／不带行内引用的重复PR只产生一次升级；新来源行不会改变行为身份；旧策略字面比较、行内路径和literal空格保持；跨阶段来源持久性、容量／spill、手写map和History2／3输出策略通过。冻结旧前置渲染后，0／1／2三次提取＋aggregate实际请求与fd8b逐字节一致。最终安装代码回放原真实提案也同时验证render路径和行为键一致，原artifact、规则、footer与路由未改。这是受控回放，不是新的原生模型质量。

生产增量2文件，净33行／298 AST节点，相对fe4；前一来源连接切片为3文件+42行／+317 AST，累计及原始Git基线仍需在最终整合独立统计，净减行不是门槛。只修改共同格式化／解析和History比较的调用策略，未增加vendor或调度实现。

当前双仓完整patch和安装包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/quick-map-body-identity-20261017`)含before失败、after通过、正常安装命令、冻结旧请求、实际提案回放、源码／wheel和重建身份。完整patch分别相对647e6e02／1e70e983应用一次，不可叠加到已有修改树。此包替代fe4作为当前代码候选，fe4及a52a原证据不被改写。

原479预算与剩余额度／unknown全保留，本轮零新增模型调用，无commit、push、发布或部署。新版本真实aggregate／完整模型比较、十层质量、深层恢复、其他vendor及最终release／pins／在途保留／回滚仍开放。完整目标不标记完成。
