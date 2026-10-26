# 共享快速入口导语：后续 P1 实现与验证

框架交付已收口，本切片不重开框架验收。修复实际 History 结果中的导航缺口：表格已有 VersionedMap 行，owner 导语仍只有 engine／tick。唯一 renderer 的新策略2把 active 规则标题和触发条件纳入有界导语；保留 owner 触发词，不从目录猜测规则或证据。最多最近12条规则、单项40字符，原表格／总长度限制继续生效。

## 归属与调用方

唯一 owner 是 `kb_service.init_quick_maps.render_quick_map`。共享 Stage 的刷新、owner容量准入、init_content 的规则放置容量模拟均传递同一策略；History通过同一Stage路径刷新，不增加专用renderer、模型或repair循环。本切片没有新增删除记录；原40条删除继续有效，旧owner-only渲染作为同一函数的兼容政策保留。

新init批次绑定策略2，并在输入指纹与coverage记录。旧通用记录缺少字段时用策略1，非法版本在外部工作前拒绝。History新批次为v6，保留v0–5的原system／渲染／请求；不是把旧批次迁移到新模型请求。Discovery和depth原本不写规则地图的边界不变。

## 验证与可审阅代码

验证记录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/quick-map-lead-20261017T102801Z/validation.json`)、代码身份 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/quick-map-lead-20261017T102801Z/code-identity.json`)、完整双仓补丁／wheel及证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/quick-map-lead-20261017T102801Z/quick-map-lead-review.tar.gz`)。当前Copilot wheel SHA256 `2df94d6ab5773a6a95369579357052edaf0dea913a2dfdcccf53fecfb033ddc5`，Bot wheel未变。补丁各对原647e6e02／1e70e983应用一次，不叠加旧overlay；无commit、push或部署。

源码定向43／边界63通过；正常安装172通过，无skip。最后常量提取后另行完成源码绑定／依赖边界31项复验；安装验证绑定最终源码。安装使用隔离目录、`-I`、清除PYTHONPATH／PYTHONHOME，依赖复用不称fresh。旧17a与当前wheel实际运行History0–5，每版本3提取＋1聚合的system／prompt／record_payload完全一致；两端前置知识树显式固定旧source／navigation渲染以比较相同输入。

初次回归运行误用了旧安装包，失败JUnit保留；首次无build isolation构建缺hatchling，随后正常隔离构建成功。原失败不擦除。当前源Python5文件净+38行／+328 AST节点，逐文件见repair-production-delta.json。新wheel产品成员集合不变，仅5个Python成员变化，安装成员及源码匹配。

## 仍开放

没有新增真实模型调用，未声明新aggregate质量通过；原519失败与账本保持。外层stage ack恢复、真实整例质量、深层恢复、其他后端及最终配对发布验收继续按原优先级推进。此前6a8的全套／远端证据不绑定本wheel。完整目标未完成。
