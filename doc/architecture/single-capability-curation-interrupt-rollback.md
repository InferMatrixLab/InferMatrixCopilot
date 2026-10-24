# 框架后恢复验收：curation中断回滚

已修复实际复现的遗漏：curation在普通validator失败报告时恢复文件，但异常／KeyboardInterrupt直接离开验证器时，已追加规则仍留在目标页。真实SIGINT负例中，supervisor已停止validator，页面却没有恢复。三个原始负例均保留，不用后来结果覆盖。

`knowledge_service/apply.py` 现在由同一个异常边界覆盖写入、validator与结果构建。原两处恢复循环删除，统一在原writer locks仍持有时恢复所有目标页的精确字节，然后重新抛出原异常。写入OSError保留原错误合同；正常validator拒绝保留结构化结果；清理不确定仍不宣称稳定rollback、不继续模型修复。SIGINT与RuntimeError不会被改写成可以继续修复的语义拒绝。

## 验证与可审阅代码

- 源码167项相关curation、validator、knowledge操作／lifecycle、process及依赖边界检查通过，无fail／error／skip。
- 当前wheel安装的SDK／Direct／Strict／应用工作流／发布准备及知识修复检查189项通过；Bot public SDK knowledge整合2项通过，均无fail／error／skip。测试复制到产品树外，正常venv导入，无产品src注入；依赖复用，不称fresh安装。
- 真实worker SIGINT用例确认进入validator前文件已修改，随后保留KeyboardInterrupt、停止原validator并恢复原始字节。两页异常注入同时证明原异常对象保持。模型回复与知识提案为fixture，没有真实模型／GitHub调用。
- 当前Copilot wheel **749ab461a2ca35dc1e2868f414c99da8a43fcd13119892f80bbf676aae0c6c25**，相对564d仅apply.py与RECORD改变，全部安装payload成员逐字节匹配。ReviewBot a57 wheel未改；此前真实Docker／完整套件仍属各自原身份，不重绑定本次新wheel。

代码、wheel与证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/curation-interrupt-rollback-20261017T072429Z/curation-interrupt-rollback-review.tar.gz`)；结果 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/curation-interrupt-rollback-20261017T072429Z/result.json`)；相对上一切片的overlay (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/curation-interrupt-rollback-20261017T072429Z/copilot-overlay.patch`)；生产变化 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/curation-interrupt-rollback-20261017T072429Z/production-delta.json`)。

本切片1个生产文件，+26／−22行、AST净-20；包括删除重复恢复循环，不把行数当验收门槛。所有权不迁出Copilot，当前主清单70能力／35删除条目；封存框架的70／34清单保持原范围。

## 适用范围与剩余要求

这是**Python异常与可协作SIGINT**的精确回滚，不是SIGKILL／主机掉电后的耐久事务证明。没有新增恢复日志、自动重放或付费调用。若进程没有机会运行异常处理，原文件恢复与原任务结算仍需耐久事务机制；此要求继续作为深层恢复P1，不因本次绿色测试关闭。

[框架交付](single-capability-framework-closeout-current-20261017.md)和[旧pending部署守卫](single-capability-legacy-pending-deploy-drain.md)保持原范围。应用原框架full patch一次、再应用部署守卫包对应overlay，最后应用本包copilot-overlay.patch；不要重复叠加。真实模型质量、旧unknown原身份对账、其他vendor和不可变release／pins／在途保留／回滚／上线仍开放。未commit、push、PR或部署，完整目标保持活动。
