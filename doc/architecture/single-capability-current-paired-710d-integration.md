# 710d／Bot3bbf确定性整合闭环

> 后续原生比较已终态，完整案例未通过；见[四PR实际裁定](single-capability-history-scope-native-outcome.md)。本页确定性范围保持。

当前精确配对的确定性范围已通过，完整目标尚未完成。1160原完整套件的跨策略generation缓存失败保持原样；710d修复后的独立完整套件通过，未把旧失败改写为成功。

原完整Copilot5934项零失败／错误，原始4个环境skip保持；四个同名、同源码用例在Personal-Agent当前精确wheel／namespace／安装资源补验通过。Bot2805与原评测工具414通过；新增预算revision的当前评测工具415全套另行通过。评测测试证明harness机制，不是模型质量。

独立安装的SDK／Direct／Strict／local／remote／发布准备和依赖边界通过。fresh、hash-locked、无MCP／无产品源码注入／无安装依赖软链的最小消费者通过，包括新的初始化policy_digest调用。当前源文件9342项按冻结manifest复验无改动；运行后非生产更新为两个已全测eval预算文件及交付文档／inventory，生产源码和测试输入保持冻结版本。

实际Docker本地与SSH控制在当前镜像上通过：stdio调用、原知识manifest传递、篡改拒绝、streaming、idle／absolute timeout、取消、原结果读取、绑定不变和重复派发拒绝。控制明确移除credential source，不调用模型。新venv／Docker均逐成员匹配710d wheel；镜像78b7548e0f7060ac3ea49ad2d37bfbd3a20304578499dd2f6f8f122b549c39eb、Codex0.162.1和原CLI SHA已核对。源码、fresh消费者、远程安装、Docker的初始化策略身份一致。

四PR输入在当前安装完成受控完整提取／aggregate／恢复接线。新的真实比较已正式准入并首次启动，manifest 9dbd670d00e73a11f3db1c6d3b84bd12acdc34fd1ebc00d260466b5b8cb87bcc；全部439旧分配保留，新20／每臂10，累计459。显式请求／回复留存、发布禁用，未知不得换ID重派。实际质量仍按原coordinator终态及冻结标签另验，不从这里的确定性通过推导。

生产统计见cumulative-production-delta.json与precompletion-production-delta.json：相对原始Git基线，Copilot242文件净+12842行／+95563 AST节点，Bot37文件净-1583行／-3206 AST；相对保留的pre-completion源码，Copilot170文件+9591行／+75405 AST，Bot12文件+36行／+490 AST。含此前保留改动，不全部归于本轮，净减行不是门槛。

完整代码和验证目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-paired-710d-integration-20261017`)含双仓单次完整patch、wheel、重建身份、原结果与命令、上述资源和runtime控制。原测试进程清单曾并发写入损坏，原文件保留；观察依据原进程／精确命令与最终suite结果恢复，没有重启原测试。

全部十层质量、深层恢复／预算／发布对账、后验其他vendor、最终immutable releases／pins／在途保留／回滚／rollout继续开放。无commit、push、发布或部署。
