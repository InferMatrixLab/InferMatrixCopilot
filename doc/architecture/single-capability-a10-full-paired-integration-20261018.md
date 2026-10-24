# 当前a10／24ac完整配对确定性整合 · 2026-10-18

当前配对的完整确定性检查现已terminal；框架保持交付，真实模型质量、深层恢复及发布目标继续保留，没有因此重定义完整完成条件。

## 当前版本及结果

Copilot wheel a10c3484d7b0ddaf26f17164af8accc53e0c96740564eb1f4664a87f7efd3379；ReviewBot wheel24ac7cfba06139988757ee192179ef77f59346066a9e2917ab6102250e84989a。两仓full patch分别重建冻结tree1092d774…／841e653e…，完整源码测试不依赖变动中的工作树。

- Copilot原进程pid1587075：exit0，6017项中6013 pass＋4个环境skip，0 fail／error；原skip不改写。源码与wheel的377个对应Python成员事先逐字节核对。
- 当前a10／24ac在Personal-Agent的新安装环境：对应资源／真实Linux namespace／producer scope补验5项全部pass、0 skip；2467个双包payload成员匹配wheels，无源码路径注入、Python3.12.13。依赖复用，不称fresh安装。
- ReviewBot原进程pid2050133：exit0，2925项全部pass、0 skip。Bot源码来自冻结patch，SDK从新安装a10包导入；仅被测Bot源码进入PYTHONPATH。
- 完整Bot测试结束后再次以-I检查该环境：SDK2337、Bot130个产品成员仍逐字节匹配当前wheels，源码路径未注入安装核对。

原完整进程只启动一次，没有因观察中断／超时重跑。JUnit及stdout中的环境skip／DeprecationWarnings均原样保留。现有证据支持这些精确版本、已运行路径及测试范围，不等于所有native vendor／操作系统或真实模型质量验收。

## 工作流前置与质量准备

[五类阶段双臂前置准备](single-capability-a10-deterministic-readiness-20261018.md)共10个检查已通过。前置使用既有scripted gateway，真实ModelGateway.call_json禁止；没有启动被测native阶段。两侧upstream head／tree相同，初始knowledge clone仅两份各自版本validator wrapper不同；不将这一检查夸大为所有overlay或prompt一致。

[八层后续阶段文件](single-capability-eight-strata-preparation-20261018.md)及[标签／源码补证](single-capability-quality-input-evidence-20261017.md)保留各自范围。旧Foundation合同错误只在新输入修正，旧数据未动；原private标签未按模型输出改写。

## 交付和剩余目标

完整配对整合证据 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-full-paired-integration-20261018/a10-full-paired-integration.tar.gz`)含两仓JUnit／原进程、安装身份及namespace资源补验源文件。[可审阅的完整双仓代码与wheels](single-capability-session-deadline-delivery-20261017.md)不变，本轮生产代码行数／AST增量0；未commit／push、GitHub写入或部署。

接下来继续关闭新模型比较的source／knowledge／schema／labels／retention batch、当前profile／proxy／credential context和预算准入；完整fixture前置已补一部分，仍不等于可派发。182调用仅未分配提案，旧519／spare／paid unknown与原环境保持。

Discovery／Module-card遵循Codex-only后验安排和既有领域策略；十层真实质量、原unknown／History外层ack、深层恢复、非Codex后端及最终pins／shadow／rollout尚未全部完成。完整目标保持active，不能以本次完整确定性测试代替这些要求。
