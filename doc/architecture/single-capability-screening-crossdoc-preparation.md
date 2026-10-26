# Screening 跨文档原生正例准备 — 2026-10-16

旧 screening 入口确实执行 Skeleton._doc_rules／_screen，没有选错阶段。旧两臂生成提示与当前提示完全一致：不要复述单篇文档已明确写出的要求，没有合格隐含／跨文档不变量时可返回空。旧 supported 标签主要标识显式 contributor 要求，适合作为 judgment supplement，却没有定义必然非空的生成机会。因此旧 rules=[] 本身不是生成策略错误的证据；候选 README 链接失败及 native screening 未覆盖仍原样保留，不把它们改判为质量通过。

新增独立 source case 将客户端重试、行容量和配额守恒分散在三份文档，源码实现相同约束。host-only 标签描述可接受的语义机会，不要求特定规则ID或措辞，也不改产品规则生成策略。独立oracle验证原源码，并拒绝重复扣额、拒绝入队扣额、先检查容量再处理幂等请求三个mutant。源码公开测试也通过。oracle由主机执行，不是模型执行测试。

新的最小公共fixture继续调用现有 init_native_arm 准备／应用组合，没有新增stage、provider或筛选循环。原baseline和当前6629安装包均以明确脚本化生成／judge完成连通检查，候选进入canonical筛选，完成恢复无新增调用。这个检查不能当作原生模型生成／judge质量证据。两臂stage源pin相同；host oracle副本因commit元数据不同pin不同，但Git tree与全部文件内容相同，均显式绑定。

labels／oracle／脚本化golden控制只在private目录，位于提议worker roots之外；整个case根目录和评审包不能整体挂载到模型。尚未建立实际native mount或准入。下一步需沿用all-ten phased plan，保留旧失败／未知／deferred样本并冻结controller、worker、profile及账本身份。预算只是预测：保留原197 call allowance，提议两臂各4次，共205，不超过原206上限；未释放或预留任何额度，实际准入前必须重新核验。额度单位是native调用，不是美元。

证据目录 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/screening-crossdoc-preparation-20261016/` 含manifest、源码、私有标签／oracle、输入等价证明、两安装包连通报告、旧prompt契约核对和preparation-result.json。live_admitted=false、native_dispatches=0、quality_acceptance=false。没有修改产品、原native执行／账本／缓存或发布，框架交付保持收口，完整目标仍开放。
