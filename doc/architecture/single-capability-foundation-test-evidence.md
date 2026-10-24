# Foundation测试证据的单一组装机制

当前Copilot wheel `93753974f8549c0a1e674e844f4de1948a73c9a77d43e93b1f4338755084d166`，Bot3bbf不变。框架保持封存。本轮修复一个导致capped Foundation缺少validation输入的明确差异，不宣称原生模型质量已通过。

## 反例和归属

旧Foundation真实比较中，两臂都缺少两个owner的validation内容，配置等其他facet也有遗漏。源码核对发现：相关测试已在共享源码索引中，但owner／feature两条路径只在unlimited模式才取相关测试，capped模式直接给空列表。不能因此把所有模型遗漏都归咎于这个差异。

`kb_service/init_knowledge_parallel.py:foundation_source_packet`现唯一负责测试片段选择、实际UTF-8字节预留及源码／测试拼装。`init_knowledge.py`的owner和feature调用都迁入并删除各自旧组装主体及unlimited条件。调用方通过read_sources保留自己的生产源码排序／发现范围策略，不合并owner与feature业务政策。两种计费模式共用同一证据机制。

测试仍由原related_test_sources从直接import、symbol和真实assertions定位，最多20000文本字节，与生产源码共享既有100000文本字节上限；没有增加上下文文本cap或模型调用／费用额度。所有片段保留编号／pin，并明确tests have not been executed。不把缺失片段当测试不存在，不把提供测试自动当成语义覆盖。

## 恢复与兼容

capped输入身份新增foundation_test_evidence_version=1，避免把新证据协议冒充旧付费请求。模拟旧版本的真实dispatch intent且worker响应丢失后，新版本在任何新派发前拒绝，原检查点文件字节不变；旧执行应在其原环境按原身份对账，不能通过新协议制造重试。unlimited的原输入版本／prompt和证据策略保持，published／prepared记录不被迁写；完整跨版本恢复矩阵仍属于后续验收。

## 验证范围

- 当前正常安装版110项全部通过：真实capped owner／feature工作流（模型边界受控）、单一UTF-8预算、特定相关测试优先级、并行／未知执行恢复、coverage policy及便携validator／资源／读取containment。
- 父／子进程正常安装，无产品source-path注入。测试在独立副本运行，所需knowledge/tools fixture数据从精确已安装资源复制；产品2334个payload逐字节核对。依赖复用，不称fresh最小消费者。没有本轮Bot／完整源码／新Docker或真实质量验收。
- 原源码选择77项含1个新增fixture失败：测试添加新源码后未传新pin，因沿用前置stage pin正确未读取未来测试。修正fixture为显式新pin后，单项以及最终owner／feature／旧unknown三个新用例均通过。原失败不回写；最终安装110覆盖当前全部相关用例，未排除原失败项。
- 相对b7e6仅两个生产文件变化，+14行／+11 AST节点，Bot未改、其余payload字节不变。没有新增模型调用、预算修订、旧结果重算、commit／push／pin／发布或部署。

## 交付和余项

`foundation-test-evidence-20261017`提供相对b7e6源码overlay、before／after、精确wheel、原日志／JUnit和重建证明。原b7e6 Direct比较仍只属于其原release，不能重绑定为9375原生质量验收。

本次关闭capped缺少测试输入及重复证据组装的实现缺口。配置／其他facet遗漏、unsupported claims、真实Foundation质量和其余knowledge strata仍保留，后续需冻结独立协议再比较；不得修改原样本或标签来过验收。深层恢复、其他后端与最终发布要求继续开放，完整目标active。
