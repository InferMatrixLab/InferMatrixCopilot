# PR-history当前包输入准备 · 2026-10-17

框架交付保持封存。本切片准备下一项Codex验收输入，不修改生产代码、不新增调用额度、不派发模型。原Foundation coordinator75210仍须原位观察，不能因本准备完成就替换或并行重跑。

复用原模型执行前冻结的history_truth_fixture_v2源码、源事实标签及测试辅助文件，字节不变；这是同一已知案例的新软件版本准备，**不是新独立语料或历史失败样本替换**。私有标签放在独立private目录，不进入source／模型输入。原历史结果和裁定保留。

正常安装的7095和原始647各自建立独立workspace，完整准备脚本前置阶段；两个PR的base／head、路径和完整差异逐项相同。PR101为Engine.step返回13及实例状态保持，PR102为fmt返回37及模块状态保持；两个PR均包含源码及各自测试文件补丁，不走遗留fixture丢失第二份补丁的转换。

| 输入 | 精确身份 |
| --- | --- |
| PR101 base → head | 8a2c0459734cdaeb093b9e80315a2c6a85c895e5 → 80c2dd7ee9f9a8629fc0a7d5b619385c11ee835b |
| PR102 base → head | 80c2dd7ee9f9a8629fc0a7d5b619385c11ee835b → f7992baf46b67ac88dc29b513887df0e2749c4bd |
| 两臂最终source tree | 49463989d687236c8822545d17fbddb22d13fb4e |
| 原标签SHA256 | b73810ddbc4e1766244ee5e570ec77c9afa384fcf4a5377185685bce381913d4 |

两臂宿主源码检查均通过，均拒绝错误返回值、实例状态变更、模块状态变更三种故障；恢复源码后Git diff为空。检查禁用／清理源字节码缓存，防止变异测试误读旧缓存。候选的早期准备日志与证明保留，正式候选在清理缓存的独立workspace重验。宿主运行测试不能当作未来模型运行过测试。

冻结准备包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-current-preparation-20261017/history-input-preparation-review.tar.gz`)包含fixture、原标签、准备／核对脚本、两臂精确PR证据和源码证明。result.json列出未完成的准入条件；当前live_admitted及native_history_quality_accepted均为false。

下一步先等待原Foundation终态及实际内容裁定，然后为History单独冻结保留全部427旧分配的新额度、每臂上限、artifact／profile／roles／schema／deadline／commands；显式retain-private绑定完整请求／结果和最终aggregate base/head。默认omit仍在模型前拒绝。完成角色／原生／标签隔离／受控接线检查与正式准入后才首次启动，不恢复或改写旧History付费状态。

当前仅证明两臂输入可重建且源码检查有效；没有执行History extraction／aggregate、没有生成批准树或原生resume结论，不代替真实质量。其他十层、深层恢复、发布回执、vendor及最终release／rollout保持原完整范围。

## 后续受控接线补证

两臂已在各自独立workspace通过完整受控History流程：2次提取、1次aggregate，最终base/head与完整diff及其哈希一致；完成后resume新增调用为0，私有标签不在测量包。它使用脚本回复和假批准，只证明接线，不证明真实质量或原生身份。

当前版domain payload标志为true／true／true，并持久记录retain-private。原始647为false／false／true，没有后来新增的domain policy字段；不能伪装成原始SDK本来就具备新保障。正式旧版比较仍须走现有canonical evaluation host的显式留存适配与默认拒绝协议，不能直接用本脚本替代。当前检查明确canonical_host_retention_exercised=false，未准入派发。

受控接线审阅包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-current-preparation-20261017/history-wiring-review.tar.gz`)与wiring-result.json保存此范围及原失败日志：最初的mock缺subscription接口、外部Git末尾换行比较、旧版不存在PAYLOADS_ENV／旧版实际omit标志。后续控制使用正确mock接口、发布器既有diff格式及真实新旧策略差异；生产代码未改。原输入准备包保持原样。

调用预算仅分析为每臂3个初始调用、无配置fallback，保守上限6／臂、成对12；尚未增加任何额度或修改427旧分配。正式manifest须单独冻结这些上限，并保留旧闲置额度；上限余量不能授权unknown替换派发。
