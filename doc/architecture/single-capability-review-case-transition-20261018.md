# 后续质量阶段：独立 review 案例准备 · 2026-10-18

框架继续保持[已交付](single-capability-framework-final-handoff-20261018.md)，当前产品仍为 a10／24ac。本轮生产代码及 AST 变化 0，模型调用 0，未改原账本、预算或已有 manifest。下面是后续质量输入准备，不是质量通过或发布验收。

## 原未决身份保留

只读核对原 Strict baseline 的 calls.sqlite：call `42028995…` 仍为 reserved，对应 PR1／head `8071398213f28143a69cc303e6abee609ae68214`；原业务 run `run-20261016-141023-9135ea` 为 interrupted。这两个状态同时成立：业务中断不证明最后一笔 native 请求已结算。

现有新草案仍引用同一个 PR1 案例，因此不能以换实验 ID／新建目录作为恢复方式。Ledger 的 unresolved 检查限于本账本，不自动扫描不同 campaign；本轮没有实现或宣称已实现跨账本全局识别。旧草案继续被 retained allowance 校验拒绝，未改为可派发。原 Discovery unknown、History 外层 ack 及其他账本状态也没有被本次单项核对清除。

## 显式独立的新案例

在新输出根准备 PR4870，head `231b3ddb97c177e6cee50f16be5ab8e9dee9228c`，base／唯一 merge-base `0798b0bf03cb6e7c4dc40ee58fe12ce492ce255f`。它是不同业务案例，不能算原 PR1 的恢复或质量结果。

- 可见 case 只有 request、review.diff、source manifest 和仅列这些输入的 manifest。private labels 移到独立 private 目录，字节与旧冻结标签一致；没有根据模型输出修改标签。不能把整个输出根挂给 worker。
- 新 request 显式声明 `pull-request-merge-base-v1`；canonical loader 验证唯一 merge-base 与 clean head。diff 字节与原 PR4870 完全相同，SHA256 `255e5cf441bd5ce35db06b33d068cd3027981636737a6d2081d5ac6711e1491a`。
- 使用当前安装 ReviewBot 的原 host serializer 构造 packet／patch，未另写 evidence parser。4 个修改路径及 1 个 carried finding 保留。讨论身份明确为 synthetic；CI unavailable，publication disabled。
- 两侧隔离安装解释器分别通过同一 canonical source／host loader，并成功构造 Direct／Strict SDK request。repo／PR／head／carried findings 一致，request 与 host packet 哈希两臂相同。当前 Strict 接收 SDK business_context；原 baseline 仍沿用其 original forge adapter，未改旧包。
- 四份 Direct／Strict × baseline／candidate 阶段文件已指向新输入，通过既有 load_phases 及 hash 检查。这是阶段提案，不替换旧 manifest，不构成全局准入。没有 reserve_review、worker、provider 或模型运行。

该案例是已有训练 replay，不是未见过的 blind case。PR body 中的作者测试叙述未经独立 CI 验证；现有源码／CPU oracle 不等于 GPU 服务或真实 review 质量验收。

## 可审阅证据和下一步

本轮输入及身份记录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-review-case-transition-20261018`)包含 private transition 原记录引用、两臂 installed-type 验证、可见输入、四份阶段提案及 loader 结果。根和 private 目录为 0700；文件为 0600。source repo／48MB bundle 沿用原冻结位置，仅记录绑定，不重复打包。

后续仍需把新案例显式并入完整 paired manifest，冻结相同 knowledge／当前 schema、精确 worktree destination 与可见范围；随后完善有效 profile／proxy 生命周期和独立预算准入。现有固定 host preparation timeout 对真实 source 的充分性尚未验证，不能沿用 synthetic 的通过宣称真实整例可运行。旧 519／spare 保持，新 182 次提案仍未分配。只有原身份对账或明确独立的新案例才可能进入后续派发审查，不能隐式替代未知结果。

完整计划正文保持不变，真实质量、深层恢复、非 Codex 后端及最终发布目标继续开放。
