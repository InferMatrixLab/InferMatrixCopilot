# Direct真实请求捕获与共享prompt修复 · 2026-10-17

框架保持收口；本次属于后续主流程正确性修复。当前配对更新为Copilot `2ddf2f41…`／ReviewBot `24ac7cfb…`。产品只有两个Copilot成员变化，Bot未变；新代码、原失败和精确wheels一起交付。

## 从实际输入发现并修复

1. 唯一共享Direct prompt仍要求模型执行base..head，与实际PR的merge-base diff不一致。对base已向前推进的PR6062会包含目标分支无关变化。现在审查和图示都使用同一三点range。新增分叉Git用例实际执行prompt中的两条命令，验证无关target文件被排除。
2. 默认prompt已经嵌入SDK plan，执行层又追加完整plan，导致同一知识注入两次。现在canonical builder以显式provider_issued_context标记权威上下文，只渲染一份；自定义prompt继续强制追加SDK plan，不能借此省略知识或替换其身份。权威标签保持，现有custom／canonical测试未弱化。

## 实际捕获与验证

原命令、raw prompt／schema及验证目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-pr-diff-prompt-20261017`)。使用原安装baseline与当前安装SDK，沿已有Direct评测driver／durable runtime走到CLI边界，捕获完整首次request后由受控捕获器返回exit78。没有生成模型回答，也不将driver exit1称作review成功。两个中间candidate和两个最终candidate fixture durable run均终态，未创建付费业务运行；原519、spare及unknown不改写。

两组最终捕获都验证exact head、同一原knowledge tree及三点diff指令，resource_revision身份从两份降为一份：PR5636从200059降至116295字节，PR6062从297931降至160004字节。仅说明注入与输入量变化，不宣称模型质量提高。custom prompt保留的权威注入由现有E2E测试覆盖。

40项相关源码测试通过。最终新wheel完成新venv离线hash锁定最小安装、pip check、2337个成员逐字节匹配；没有MCP或源码路径注入。该最小安装还实际执行上述两个Direct首次派发捕获；Bot兼容build_review_prompt仍是public SDK的同一函数对象。

baseline和candidate原schema文件及hash均保留。唯一结构差异是existing_thread的空分支与optional-group正则写法，等价参考形式及代表性引用验证已登记。捕获位置的baseline CLI未显式带model而candidate带gpt-6-sol，不能据此称作已冻结匹配模型；真实比较仍须legacy translator／canonical provider profile的不可变model／effort准入。

首次追加去重修复让旧权威标签断言失败，另一个新增fixture缺少必需pull。原source-final.xml保留；产品恢复权威标签、fixture补齐业务输入后，source-corrected.xml全部通过。没有删除原断言或把失败改成skip。9276／a2a两中间wheel与环境保留，不作为当前交付。

## 可审阅代码与当前范围

完整双仓补丁、最终wheels与本次证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/direct-pr-diff-prompt-20261017/direct-pr-prompt-review.tar.gz`)。full patch分别对原647e6e02／1e70e983应用一次，不叠加已有full patch。新wheel完整hash、tree和私有index重建见code-identity.json／validation.json；真实index未变。production-delta.json列相对1b36的两文件行数／AST变化；累计patch包含此前保留工作，不以净减行作门槛。

此前1b36完整套件、fresh安装及image7656的local／SSH控制均保留原范围。新产品不能借用这些结果冒称已经完整native验收；最终发布需新的不可变配对／image／环境／profile，原付费环境不在原地替换。当前Bot完整2925及回执修复的未改范围继续保留。

本次完成的是完整**首次Direct**请求／schema捕获，不是后续repair、Strict全部prompt或十层真实质量比较。下一项继续Strict／后续阶段输入、完整独立labels／裁定／容差、实际profiles和新预算准入；真实质量、深层恢复、其余后端与最终release／rollout仍开放。没有commit／push／发布／部署。
