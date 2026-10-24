# 修复 completed attempt 与 unfinished job 间的重复执行

真实进程退出测试发现：共享 publisher 已发布 review，router 已将 attempt 记为 completed，但 workflow job 尚未写入终态。重启重排 job 后，已完成 attempt 不再被当作活动执行，路由会创建新 attempt 并再次付费计算，直到 publisher 才挡住重复 review。旧B939安装包的反例保留在 before-fix.xml，两种模式都观察到第二次模拟模型执行。

修复在业务宿主 WorkflowExecution 中：路由前检查 job 所附原 completed attempt 与 exact head，并查询该 attempt 的远端发布标记。标记存在直接恢复 already_posted；查询异常，或已有 github_review_url 而标记暂未可见，返回 deferred，保留原 attempt／provider_run_id。shadow 无实际发布回执且没有远端标记时仍允许新发布。归属仍为 ReviewBot；未把 GitHub 状态移入 Copilot。

新增测试对 Direct／Strict 各覆盖远端接收但响应丢失、attempt 已完成但 metrics 未写、metrics 已写但 job 未终结；恢复两次均一条远端 review、一次模拟模型执行及同一 attempt／provider_run_id。后两种场景额外注入标记查询异常和暂不可见，均保持 queued，禁止替代调用。模型和 GitHub 使用持久化测试适配器；实际 job、attempt、router、publisher 和子进程退出／重启均为产品路径。

新 Bot wheel 为 `3bbf71df48a24eb05f6d07cb68fe145f8c2cd15b22f17a2f182dba4a7d001860`，只改变 workflow_execution.py 和 RECORD；Copilot6629未变。全新离线环境安装并复核包内 RECORD，定向工作流、恢复、维护和入口边界测试通过。原安装测试驱动缺 scripts/deploy 两个只读 fixture 导致的失败保留，补入原字节后通过。先前B939全量报告不重绑定新 wheel，新 wheel 尚无完整配对全量证明。

交付：`output/single-capability-completion-20261012/publication-workflow-recovery-20261016/publication-workflow-fix-review.tar.gz`。reviewbot-overlay.patch 应用于上一份 current-paired-review.tar.gz 重建结果；包含最后生产修复和最近两份新增崩溃测试，已独立重建核对。历史包不改写。没有真实模型调用、原未知执行重放、commit、push、pin 或部署。完整质量、并发远端一致性、未知执行及发布验收继续开放。

## 当前新 Bot 完整回归补验

新Bot3bbf71df／Provider6629已完成完整 Bot suite、配对公共 SDK smoke、依赖检查及每个 wheel／安装字节核对。Bot测试使用安装包作为默认产品导入；源码、launcher 等仅作为测试显式读取的冻结 fixture，pytest 未配置 src 注入。Provider未变化，沿用原6629全量证据，本次不冒称重新跑完整Provider suite。细节见 publication-workflow-full-20261016/closure.json。初次严格 provenance audit 因 provider 的 hash-lock 命名安装没有 direct_url.json 失败；原失败保留，随后显式安装同一个本地wheel由pip正常生成元数据，逐字节确认产品未变，再通过完整audit。不存在伪造元数据或改写失败。
