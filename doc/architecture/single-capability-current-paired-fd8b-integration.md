# fd8b／Bot3bbf确定性整合闭环

> 原生比较已终态，完整案例均未通过；见[实际裁定](single-capability-history-citation-native-outcome.md)。下文原阶段记录不改写为质量通过。

> 后续[原生引用契约回归](single-capability-history-citation-native.md)已首次启动，质量未裁定；本页确定性证据保持原范围。

框架交付保持封存；当前候选的确定性整合通过，完整目标仍开放。原710d四PR实际失败不修改，本修复的真实模型效果尚未裁定。质量比较只准备、未准入；不从确定性测试推导质量改善。

当前Copilot wheel fd8b1ba970e87a70195512ff7856fef19440000b24a060193c6f1efe4e9a17fc，ReviewBot3bbf71df48a24eb05f6d07cb68fe145f8c2cd15b22f17a2f182dba4a7d001860。完整Copilot、Bot和原评测工具均由原进程终态零失败／错误；新revision479的完整评测工具补验另列。原始source4个环境skip保留，四个同名／同源码用例在Personal-Agent精确正常安装及Linux namespace补验通过，raw no-skip gate仍为false。

独立安装SDK／Direct／Strict／local／remote／发布准备和双仓依赖边界通过。fresh、hash-locked、无MCP、无产品源码注入和无安装依赖软链的最小消费者通过。冻结source manifest逐文件复核，运行后修改仅为明列的eval协议／上限及交付文档，产品和原测试输入不变。

当前正常安装应用完成原四PR的受控完整接线：4提取、1aggregate，采用201／202／203，解释不采用缺模型页204，完整diff/base/head绑定且完成后resume零调用。canonical citation descriptor在实际请求中区分context-only tests和归属实现。它是已知标签回归接线，不是新独立／盲测或原生质量证据。原source pin4209049848b715825987899c83e4875307dce03c及私有标签保持字节一致。

Personal-Agent新venv与Docker镜像40cdfe2d3298b08f562f71c6012800daf0ca6035a96124c24bd84cddd5b3c097均与fd8b wheel逐成员一致。Codex CLI0.162.1及原二进制SHA保持。控制机源码、安装、fresh消费者、远程安装和Docker的init policy_digest均为59de931ea4b8d697b8e93b9da709d4f368a5581c0c1a82c50062ffe8f59d5dd3。旧环境保留。

实际本地／SSH Docker stdio与进程控制通过：知识manifest传递、篡改拒绝、streaming、idle／absolute timeout、cancel、原结果读取、绑定不变和重复执行拒绝。凭据source明确移除，无模型调用或GitHub发布。

能力主清单已将框架后的generation检查点及citation契约纳入主数组：68项能力、32项删除条目。实际委托入口的定向AST核对见inventory-audit.json，限于命名事实，不是全仓无重复的通用证明。原66／30封存报告保持原范围。

生产变化按cumulative-production-delta.json和precompletion-production-delta.json独立计算。相对原始Git基线：Copilot244文件净+12907行／+95880 AST节点，Bot37文件-1583行／-3206 AST。相对保留的重构前源码：Copilot172文件+9656行／+75722 AST节点，Bot12文件+36行／+490 AST。含此前保留改动，不全部归于本轮；净减行不是门槛。引用契约这一切片相对710d为4生产文件+65行／+317 AST。

当前代码和验证目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-paired-fd8b-integration-20261017`)包含双仓完整binary patch、wheel、私有index重建、源码与安装身份、原始命令／结果、metrics和实际runtime控制。完整patch分别相对Copilot647e6e02／ReviewBot1e70e983应用一次，不能叠加到已有变更的工作树。框架架构／能力／迁移和删除清单仍见[封存交付](single-capability-framework-delivery-20261017-final.md)。

[引用契约真实比较准备](single-capability-history-citation-comparison-preparation.md)保留全部旧459分配，新479仅为显式新上限，未复用闲置或替代unknown。模型比较正式准入、十层质量、深层恢复／预算／回执、其他vendor及最终immutable releases／pins／在途环境保留／回滚／rollout继续开放。无commit、push、发布或部署，完整目标未标记完成。
