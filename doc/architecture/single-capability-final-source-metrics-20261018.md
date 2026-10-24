# 最终冻结配对源码行数与AST · 2026-10-18

统计精确冻结树：Copilotec81d927（wheel98554010）／ReviewBot841e653e（wheel24ac7cfb）。源代码及wheel的绑定证据见最终代码身份，统计不读取变动中的工作树。

| 范围 | Copilot：变更文件／净行／净Python AST | ReviewBot：变更文件／净行／净Python AST |
| --- | --- | --- |
| 相对原upstream647e6e02／1e70e983 | 245／＋13338／＋99233 | 45／－1084／＋541 |
| 相对本任务开始收尾前的冻结快照 | 179／＋10087／＋79042 | 26／＋535／＋4238 |

“开始收尾前”指output/single-capability-completion-20261012/baseline中的copilot-source.tar.gz／reviewbot-source.tar.gz，已经包含此前原型变更，不是原upstream；两份archive的hash记录在报告中。

范围严格为src/下UTF-8生产文本文件，不含tests、eval、docs、外置knowledge/playbooks资源和开发tools；排除bytecode。AST由ast.walk计算全部Python节点；内嵌JavaScript按Python字符串节点计，不是JS AST。文件名、前后hash、每项净行／AST及三版本总量保留于机器报告 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-pair-source-metrics-20261018/source-metrics.json`)。这不是整个仓库行数。

新增执行／沙箱及恢复机制使Copilot总体增量为正；Bot删除vendor／curation主体后的新增host协议与状态处理也增加AST。不能把历史某切片减少的数字冒称当前累计减少。单独最后Direct修复相对a10为8个产品Python成员、净＋60行／＋624 AST，与此累计统计基准不同。

净减行不是验收门槛。唯一归属、迁移调用方、删除具体主体、依赖边界与行为合同继续由[能力清单](single-capability-framework-inventory.json)及对应测试证明。
