# 新History scope比较输入准备

本次准备新的四PR合成案例，不替换已失败的7095样本；零真实模型调用、零额度修改。候选准备范围是1160，真实比较尚未准入。1160完整整合发现generation缺少工作流策略绑定，须先完成修复、冻结新候选并补控制；当前准备不能绕过该缺口派发。

| PR | 源码合同／预期归属 |
|---|---|
| 201 | VersionedMap先检查revision冲突，再检查容量；失败保持revision和contents；core组件。 |
| 202 | canonicalize返回排序副本，保持重复项与调用方状态，并可重复应用；tooling组件。 |
| 203 | QuartzDecoder神经模型cache按token分行、每行两channel；已提供model:quartz-decoder页。 |
| 204 | OpalEncoder模型专属padding ID，无对应模型页；应说明不采用，不能塞入组件。 |

两臂source pin、各PR base/head和完整两文件补丁相同；源码测试均通过并各拒绝五种故障变体。模型可见知识页和路由字节相同，知识Git基线SHA因两份validator实现不同而不同，差异已逐文件核对为原实现与canonical薄包装，不能假称两个完整仓库tree一致。私有标签位于独立private目录，作者知道标签，不称独立盲测或统计证明。

两侧受控run_stage实际经历四次提取和一次aggregate，采用201／202／203、不采用204，完整diff与base/head绑定，完成后resume新增调用为零。这些是脚本回复，不是模型质量。首轮控制把未被owner路由覆盖的tests/文件放入rule evidence，正确被拒绝；修正控制为该owner下实现文件的证据，没有修改production归属策略。初次未合并prerequisite和旧SDK测试辅助文件导入不兼容也已保留原日志；最终使用原有本地夹具merge和旧版兼容辅助文件，不真实发布。

候选domain留存标志五次均true；原647为四次false、aggregate true。此控制没有验证旧版canonical宿主留存适配；正式准入仍须验证原生留存，并保持默认omit拒绝。未来现有native driver须使用capture避免旧两PR硬编码characterization校验，宿主按新私有标签裁定四PR合同。实际执行仍委托原有history_native_arm／run_stage，没有新增vendor或修复循环。

最小分析为每臂5次，按每调用一次有界修复为10／臂、成对20；这是分析，不是新增分配。旧439及全部闲置、unknown均保持原状。后续必须冻结正确artifact/profile/roles/schema/期限/命令，并以新协议正式准入后才能调用模型。

输入和证明目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-scope-comparison-preparation-20261017`)保留source-proof、evidence、private labels、model-context-equality、wiring及原失败；result.json明确quality_accepted/live_admitted=false。其他十层、深层恢复、vendor和最终发布目标不变。
