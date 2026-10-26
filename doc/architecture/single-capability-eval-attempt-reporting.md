# 评估尝试计数归一（2026-10-16）

维护中的 init 评估 helper 已不再把 gateway 记录数称为原生调用数。产品包保持当前6629/B939，框架交付和该包的确定性整合结论不变；本切片仅修改评估代码、调用方测试和格式文档。

## 格式及调用方迁移

- stdout 使用 `init-stage-report/2`，产物 JSON 使用 `init-stage-artifacts/2`。
- `gateway_attempts` 统计已记录的 gateway 尝试，包含派发前失败。scope 明确普通流程计 generation／judge，scripted correction 只计 judge，模拟生成单独记录。
- correction 的 `first_judge_attempts`／`correction_judge_attempts` 替代旧 calls 字段；其 recorder 不再漏掉 sent=False 的拒绝记录。
- 同一 helper 的全部阶段和仓内消费者已迁移，旧 `native_calls`／`first_judge_calls`／`correction_judge_calls` 发射主体删除。格式说明见 [eval README](../../eval/single-capability-shadow/README.md)。

实际派发／完成仍由调用账本及原生事实证明。报告没有新增计费、预算结算或重试实现，也不把缺失事实当零。旧 manifest、复制的 helper、原始报告及模型回执保留原字段与 hash；修改后的 helper 必须重新冻结到新 manifest，不能覆盖旧运行。

## 验证与代码

两个回归用例在实际冻结的旧 helper 上失败，修正后相关五个模块138项检查通过，失败／错误／跳过均为零。验证从独立目录加载已安装6629/B939产品，没有产品源码路径注入，也没有模型调用。

一个回归通过完整 Skeleton 及真实 ModelGateway 边界验证：四条 gateway 记录只对应三次 fake completion，另一次在 preflight 拒绝。另一个验证 correction recorder 保留拒绝记录，同时 canonical trace 明确 sent=False／pending=False，fake provider 未被调用。初稿测试对 iter_records 的 tuple 返回值处理有误，其原失败报告保留；最终测试按实际 API 解包。

可审阅补丁、五文件重建证明、精确冻结输入及原始报告位于：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/gateway-attempt-reporting-v3-20261016/gateway-attempt-reporting-review.tar.gz`。SHA256 `03df2244e54ec5d054ce17c09f1315aa311d808e842e88e5e37755ef6da66af0`，24个成员逐项核验。patch 对原 helper／测试及新 README 在私有目录重建成功，没有修改原 Git index／objects。

2306个打包输入与已整合候选逐项相同，没有产品 wheel 输入变化。前次完整源码／七组安装证据属于其冻结评估快照，本次变更另有上述定向证据，不重新绑定旧报告。

原实际 screening phase 的5119个绑定文件另行核对均未变化；其八次原生调用结果及完整质量不通过的结论不改写。累计额度仍从205／206保留，未追加或释放。完整质量、恢复／后端及发布验收继续开放。
