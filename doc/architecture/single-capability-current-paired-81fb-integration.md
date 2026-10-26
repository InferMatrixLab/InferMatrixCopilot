# 当前81fb／Bot3bbf确定性配对整合 · 2026-10-17

**这一精确版本的确定性整合已闭环；框架保持已交付，完整目标仍开放。** 本阶段未修改生产代码，验证的是已封存的预览兼容性修复版本，而不是重新绑定旧候选结果。

完整Copilot源码5896项零失败／错误，原报告保留4个环境skip：3个安装资源检查及1个实际namespace检查。同一81fb／Bot3bbf正常安装包在Personal-Agent执行了相同测试和额外producer-scope控制，5项通过；源码、包字节和测试身份已核对。raw_source_no_skip_gate仍为false，不改写原报告。namespace原报告的人类scope文字误留59a5，原字节保留；package-proof及current-scope-proof通过实际81fb wheel哈希明确身份，不把旧结果当作新结果。

完整Bot2805和评测工具412项通过。Bot完整组的父进程使用wheel，部分源码检查与子进程源码组合明确保留；另在没有相邻产品src的正常venv中，SDK／Direct／Strict主流程87项、Bot本地／远程／恢复主流程190项通过，并独立确认父进程和代表性子进程从site-packages导入。评测工具回归不是实际模型质量比较。

全新、依赖哈希锁定且无MCP的最小消费者通过，venv中无依赖软链、无产品源码注入。常规验证env复用依赖，不与fresh证明混用。2463个配对安装包成员与wheel、冻结源码及资源逐字节一致。当前镜像sha256:35d730a6c3dd84436c1ca703f93eb370519aecd1b3bbcfd5a612963b0a6dd5e7的本地／SSH stdio知识读取、篡改拒绝、流式输出、idle／absolute／cancel、原结果回读及禁止重派均通过；零模型调用、无凭据挂载。

代码可从原始Copilot647e6e02／ReviewBot1e70e983重建：完整补丁包含新增／删除及之前保留改动，私有索引和独立checkout核对9321个冻结文件。Copilot wheel81fb58963641d76ab41e4379b023321f9fba394f1f72fe584a4ef4cd06836a7e；Bot3bbf71df48a24eb05f6d07cb68fe145f8c2cd15b22f17a2f182dba4a7d001860。生产变化相对原始基线：Copilot241文件、+12598行／+94205 AST，Bot37文件、−1583／−3206；相对完整重构前归档的Python源码：Copilot167文件、+9347／+74047，Bot12文件、+36／+490。基线与范围不同，计数不替代能力归属和行为验证。

精确结果与范围 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-paired-81fb-integration-20261017/deterministic-closure.json`)、当前完整代码及证据 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-paired-81fb-integration-20261017/current-paired-integration-review.tar.gz`)。能力／调用方／删除清单仍见[机器清单](single-capability-framework-inventory.json)，当前65能力、26删除条目、框架待接连接为空。最终报告／计划／清单的追加说明发生在冻结验证后；产品、测试及打包资源字节未变，代码补丁保留冻结文档版本，外层docs提供本次状态。

后续P1保留：Foundation可复用解释的完整性与provenance边界、各要求场景的冻结真实Codex质量比较、深层恢复和原unknown按原身份对账。P2：精细费用／served model、认证、其他vendor原生矩阵。最终：质量通过后的不可变release／pin、在途原环境保留／排空、回滚和部署验收。本阶段没有模型调用、旧账本／旧样本改写、commit、push、pin更新或部署；确定性通过不代替质量或发布批准。
