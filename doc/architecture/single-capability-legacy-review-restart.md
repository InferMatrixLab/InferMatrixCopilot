# 旧无身份审查重启：防重复派发

修复前实际本地复现：旧Direct attempt没有provider key／run ID，watcher重启把它标为interrupted，随后同一job创建generation2并再次进入审查。模型边界为fixture，没有真实付费。修复后保留原attempt，零新增调用、零新generation；原失败结果不覆盖。

ReviewBot的业务恢复规则现在统一归属persistence/review_recovery.py：旧queued／running审查缺少明确新派发协议及原provider身份时，将原job记为待对账；watcher在本地／远端执行与路由之前检查相同repo／PR／head，新触发键或superseded状态不能绕过。旧attempt及中断回执形状保留，不虚构provider ID或原执行位置。

新协议仅由新producer在插入新job的同一事务中写入，comment、自动选择和两条业务重试入口调用同一函数。去重INSERT不为旧job补标，旧writer也不会被schema隐式标成新协议。新流程可证明尚未派发的中断继续恢复；已明确记录终态、结构化failure_class和finished_at的旧失败保留业务重试，未知与备注文字不能当作这个证明。原有provider key／run ID仍走原恢复流程，未知不变更地点／release／profile。

旧未执行队列若没有可信派发证明，同样不自动推定未付费；需要留在原环境处理，或取得原始结果／派发事实后明确对账。此修复不自动解除隔离或补发。这是新watcher恢复旧状态的证据，不是允许新旧watcher混跑同一账本：切换新admission前须停止旧watcher，原worker／release／profile及状态留到对账完成。

## 当前验证与代码

| 范围 | 结果 |
|---|---|
| 直接复现 | 修复前多一次调用及generation；修复后原attempt保留、零重派 |
| 故障与兼容 | Direct／Strict／Direct-only、不确定旧记录、新键绕过、旧writer／重复入队、新流程派发前恢复、明确失败业务重试通过 |
| ReviewBot完整源码suite | 原73587／PID1676799终态0，2811项零失败／错误／skip；CP为85ef安装包 |
| 当前正常安装流程 | provider恢复、Direct／Strict及发布job／attempt结算103项通过，无产品src注入；复用依赖，不称fresh最小环境 |
| 边界 | 当前public SDK／运行环境／命令边界15项通过，无vendor派发实现回迁 |

Bot wheel **a57ef58ac252a8464c2b785426ec933d6bd7fb9c97874106be5c05452b5c01f9**，配对Copilot **85efe57beee28c21d818a8bb3a05958edc679fcd5083c9515ff8ad515984abe4**。Bot所有payload核验一致，相对3bbf只新增review_recovery.py、改4个调用模块；Copilot85ef没有新增变动，其此前History v4必要验证保持范围。原34aa完整双仓及真实Docker证据不重绑定到新配对；新配对的完整Copilot源码、真实质量和最终发布验收仍开放。

首次完整测试为纠正writer provenance方案主动停止，原中断报告保留；最终方案不使用会把旧writer误标为新协议的DB trigger。最终完整源码与安装结果均来自修正后的同字节代码，不把中断报告计为通过。

本切片5生产文件净+72行／+282 AST节点；相对原Git基线Copilot244文件+12994行／+96507 AST，Bot39文件−1511行／−2924 AST；相对重构前保留源码分别为Copilot172文件+9743行／+76349 AST，Bot16文件+108行／+772 AST。AST对缺失文件按空Module，与累计口径一致。包括此前保留工作，净减行不是门槛。

完整双仓代码／wheel／验证包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/legacy-review-restart-20261017/legacy-review-restart-review.tar.gz`)包含原失败与通过复现、原始JUnit／命令、正常安装、指标、完整binary patches与私有index重建证明。分别相对647e6e02／1e70e983应用一次，不叠加到已有变更树。真实生产调查另见[只读保留清单](single-capability-production-retention-survey-20261017.md)；没有修改生产账本、部署或触碰凭据。

此修复闭合新发现的重复派发风险；[框架交付](single-capability-framework-delivery-20261017-final.md)的归属和架构保持，机器清单新增显式业务恢复policy owner为70能力／33删除记录。真实模型质量、深层恢复矩阵、其他后端与最终release／pins／在途保留／回滚／发布按后续阶段继续；完整目标不标记完成。
