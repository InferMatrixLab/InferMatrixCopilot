# Foundation 独立 revision-cache 质量案例准备

新增与历史失败样本不同的源码／文档案例，测量 RevisionCache 与独立观察工具两个 owner 的六个 Foundation facet。私有验收标签在任何原生派发前单独冻结：旧revision先于容量判断、同revision冲突不改变状态、快照独立且容量按当前内容记账、compare-and-delete不得删除新replacement。没有固定模型措辞或预设pass数量；模型生成、应用judge、最终tree与宿主质量判断分开。

宿主oracle在原源码通过，分别拒绝破坏revision删除条件、旧revision顺序、同revision冲突的三个变体。公开源码测试也通过；这些宿主结果不提供给模型，模型看到的测试源码只能支持“存在相应断言”，不能声称已经运行。

两套已安装应用通过相同fixture与ModelGateway脚本回复，各2生成＋12审查，两个owner十二facet，完成后恢复零新增调用，tree／verdict不变。脚本假批准不代表质量。模型实际源pin在两臂相同，与独立准备源码Git tree精确一致；提交历史差异由source-equivalence.json明确映射。

前置空规则批次没有rules页，首稿路由指向不存在的rules页导致owner被丢弃；零原生派发时修正为现有architecture页，原草稿与scratch世界保留。标签和源码内容未修改。没有产品代码、权限或验收策略变化。未来原生phase复用foundation_native_arm.py并显式--capture，避开旧案例硬编码断言，保留应用本身的校验／judge／恢复检查，另做冻结宿主验收。

材料目录：`output/single-capability-completion-20261012/foundation-revision-preparation-20261017`。新配对需28调用，旧205全部保留时总233，当前206协议不允许；未准入、未扩额、未调用模型。最终可执行manifest、账号／镜像控制及额度处置仍待完成。仅覆盖Codex非unlimited范围，不能替代unlimited修正或全部十层质量验收。
