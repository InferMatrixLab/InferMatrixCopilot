# 真实PR请求与diff合同准备 · 2026-10-17

框架阶段保持收口。本次推进后续真实质量输入，修复评测准备层的实际缺口，没有更改产品src、wheel、provider或真实知识页，没有派发模型。

## 已修复与复现

PR6062的目标base不在head祖先链。旧review_case_arm.load_case固定比较base/head两revision，导致正确GitHub merge-base diff被拒绝；原失败保存在before-real-pr.json。新请求显式声明pull-request-merge-base-v1并绑定diff_base_sha，校验实际唯一merge base。Direct／Strict及host准备共用同一实现；未知语义、缺失／错误／歧义merge base和错误两点diff均拒绝。旧请求缺少该字段时保留exact-revisions-v1合同，不悄悄改写历史付费fixtures。

Host准备新增哈希绑定github-files.json的薄适配器，调用已安装GitHubClient.list_files；不新增patch解析或API字段转换主体。PR5636的added文件由该codec解码；缺失patch仍是None，不编造完整性。旧Git重建路径保留其modified-only范围，也调用共享diff合同。

## 当前证据

准备与验证目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/review-pr-diff-contract-20261017`)保存原失败、修复后真实PR结果、21项定向测试、CLI准备driver及兼容记录。

- 两组真实源均验证exact head、merge-base diff及干净checkout；生成哈希绑定request和host packet。
- current public SDK构造Direct／Strict请求预览；carried findings和business context相同，尚未reserve／start。PR5636两条carried finding经宿主finding_identity生成83771df3e0ebd163／e47c1e384ad3b60a，保留原评论URL／ID、原head和原line映射，未读取私有标签来构造输入。
- 既有PR4870请求字节未改；旧host patch字节、changed rows及patch UUID与原冻结结果相同。CI checked_at仍表示准备时间，不伪造历史时间。兼容重放使用当前安装产品，不冒称原baseline产品已运行新driver。
- coordinator检查已知held-out批准ID／正文及注解ID未进入新准备文件；这只是文本／布局检查，实际sandbox mount未验收。整个输出父目录含协调材料，不能整体挂载给worker。
- 产品386份src仍与原整合逐字节相同。eval实现、测试和PROTOCOL有变化，所以旧完整eval结果只保留旧版本范围；本次21项为新输入合同的定向证据，不重绑定旧完整eval为当前通过。

原GitHub页面是后来抓取的受控replay素材，不是当时完整API归档；历史CI与resolution不可用，标题／正文不声称从未改过。host使用既有裁剪政策。SDK预览不是完整执行准入，也不是质量通过。

## 剩余工作

P1：冻结相同knowledge快照、完整独立case标签／新finding裁定与容差、真实profile及新预算准入；核对原baseline和candidate的实际渲染／裁剪差异，并完成实际worker可见目录隔离，随后才进行真实比较。其余八层完整要求继续保留。原519分配、未用额度、旧unknown、原环境及失败均不改写。

本次代码变化仅eval／测试／文档，见validation.json及完整补丁；原产品代码包和本次完整补丁不能叠加重复应用。框架不重开，深层恢复、其余后端与最终发布继续按完整计划分阶段完成。
