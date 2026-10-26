# Bridge知识快照校验统一修复

旧21ab安装包已复现同manifest文档变更后bridge继续返回内容、进程内拒绝的差异。已迁移到KnowledgeView.for_root/path唯一选择／校验实现；engine内联selector删除。Provider仅复制opaque manifest sidecar，不解析领域规则；读／搜索继续复用KnowledgeDocs，bridge正常文本格式保留。冻结后重建视图及tamper拒绝、无关host activation路径隔离已验证。

新候选292e37b6，四个生产文件变化（19行／158 AST净增量），source及installed各92项通过。当前主工作树已同步，隔离树 `/var/tmp/copilot-bridge-integrity-20261017` 可审阅。Bot3bbf不变。只声称hash-manifest完整性，不声称密码学签名或adaptive预算策略完整桥接。

独立交付 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/bridge-knowledge-integrity-fix-20261017/bridge-knowledge-integrity-focused-review.tar.gz`，SHA256 `60c720d58bdfa943d9126d324a1c61964e470fd35f9ed6f2fabfa51b75a4d8bd`。旧21ab完整整合保持其身份；新候选尚需配对全量及实际新image整合，不借用旧通过结果。原真实运行／协议不动，零模型。


## 同候选整合终态

292e／3bbf的源码test/5812项零失败／错误，3个环境skip保留并用相同源测试的实际namespace／安装资源结果补验；Bot2805与安装版evaluation408项完整通过。全新无MCP消费者、SDK smoke、实际namespace四项及2433产品文件逐字节核对通过；测试资产与主／隔离树保持一致，386项生产Python与同一wheel匹配。这里只声明记录范围的确定性整合（含精确补验），不改写原始no-skip gate。

新image `sha256:749790c6005b4ca786ee6f4e6393ce4847af9940f6e1d35166f8707707e1b689` 实际local/SSH：有效页面读取成功，tampered页面在真实MCP stdio initialize/list_tools/call_tool后返回isError，未返回坏正文；原结果回读、binding保持、重复执行拒绝均通过。早期readiness探针误以为--check会读取所有页面，观察到tampered就绪rc0；这是检查层次不匹配，原结果保留。真实逐次读取及stdio错误封装已另验证，不宣称全部页面模型前预校验。全部零模型，原运行／镜像保留。

真实模型质量、完整backend原生／深层恢复矩阵、paired发布／pin与部署仍按完整计划保留。下一步为当前候选冻结Codex质量比较的协议及准入，原未知付费运行不替换。

最终确定性整合归档：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/bridge-knowledge-integrity-fix-20261017/bridge-knowledge-integrity-integration-review.tar.gz`，SHA256 `7ce8800b8acfba156f8db63232b181ca616a0a019ffa0764129c669c544739c4`，129个成员逐项核验。重建顺序／新旧候选范围／原始skip与探针失败均保留；不把当前整合通过解释成完整目标或真实模型质量完成。
