# Review 严重度单一归属（框架交付后 P1）

框架交付23a5／3bbf不变，本切片推进完整目标中的共享 review 规则与真实质量缺口。当前后续候选为 Copilot `e3eba87112145515ad93b92a5d0b378827e3488d519cceaa3c0c375d5a66ec0e`，Bot仍为3bbf；这是独立候选，不重绑定框架或292e整合证据。

## 实际反例与原因

原292e真实Strict比较双方都找到核心缺陷，但都把有界lease-store问题列为blocker。旧调查提示定义blocker为“merging as-is causes breakage or data loss”，合并提示又写“breaks on merge”，结果投影却将其变成P0。Direct没有对应的完整严重度定义。原复核提示只解释major，覆盖补查也自行决定“breakage→major”。这些说明与线级等级转换不共享规则，且会将是否阻止合并与影响紧迫程度混为一谈。

这只能证明共享规则存在不一致，不能证明这就是模型选错等级的唯一原因，也不能证明修改提示后模型质量已改善。原冻结比较仍不通过，原输出、账本、身份和预算保持不变。

## 归属、调用方及删除

唯一领域模块：`review/severity.py`。它定义同一组标签／P0–P3身份、等级说明、排序和既有逐级降级表，不依赖SDK、应用、provider或执行内核。

| 调用方 | 委托内容 | 删除的独立主体 |
| --- | --- | --- |
| review/direct_prompt.py | canonical Direct 提示引用共享严重度说明 | 补齐原缺失的同源说明；显式兼容自定义提示保留原行为。 |
| engine/steps/review/prompts.py | 调查与合并共用说明；第二轮继承调查说明 | 调查内联四级定义、合并内联定义，以及按是否阻止合并／finding数量推动升级的独立说明。 |
| engine/steps/review/refinement.py | 覆盖补查与逐条复核引用同一说明；逐级降级引用领域表 | 补查的breakage自动对应major说明、复核的独立major说明、内联降级表。补查输出仍限major/minor。 |
| review/result_decode.py | Strict→公共结果等级转换引用领域映射 | 内联blocker→P0等四级映射。 |
| engine/steps/review/utils.py | 排序引用领域映射 | 内联四级排序表。 |
| improve/objectives.py | review replay vocabulary校验引用领域映射 | 独立允许值元组；评分与准入规则保持。 |

P0／blocker需证明当前触发和关键影响；普通正确性缺陷、CI阻挡或等待合并本身不足以证明P0。P1需说明高优先级影响，P2用于有界问题／本PR应处理的改进，P3为可选修饰。各领域已有明确等级要求继续保留。该变更明确生成时的判断标准，不通过文本启发式重新分类结果。

## 兼容与验证

- 不修改等级枚举、wire身份、历史结果、发布接受条件、任务状态或recheck门禁。显式blocker仍映射P0；未知Strict等级仍拒绝。既有unverifiable逐级降级行为保持。
- 实际SDK Direct临时CLI进程验证收到共享说明；Strict调查、合并、覆盖补查和逐条复核的实际派发测试验证同源说明抵达模型边界。临时CLI／模型回复为受控替身，不是真实付费模型。
- 初次源码组合133项通过；其后补上objective vocabulary引用，独立71项addendum通过。1532安装版129项及配对Bot66项通过；补类型负控后的e3eb安装版133项、配对Bot66项通过；资源入口修复后的源码13项通过，均零失败／跳过。原133项不是后续所有测试文件的全量证明。
- 安装第一次有1项失败：测试直接从_REPO_ROOT/playbooks查找，wheel资源位于_runtime/playbooks。测试改用产品_resource_dir解析入口，产品不变；原失败报告installed.xml保留，installed-v2.xml为修正后的报告。
- 新wheel所有2306个非元数据payload成员与隔离安装产物逐字节一致；相对23a5只有7个生产Python文件变化，+41行／+198 AST节点。Bot不变。独立delta记录排除测试、eval及文档；不把行数减少作为门槛。

本切片未重新跑完整双仓套件、未构建新远程镜像，也未做新的真实质量比较。source／installed报告各保留精确范围；旧292e实际Docker／namespace及框架证据不冒充1532验收。

补充负控确认1532初稿在replay severity为数组／对象时因映射membership抛TypeError，而23a5基线返回0分。e3eb增加字符串类型检查恢复原拒绝行为，显式测试数组、对象、null和数字；完整原生模型执行未涉及这条受控评分路径。旧1532 wheel／归档及负控结果保留；新源码与安装报告独立记录，不能把1532声明为最终候选。

## 后续与交付

下一步P1保留：冻结新候选真实质量比较，验证任务状态指引和严重度规则；单独检查摘要／证据行号一致性（原样本inline18正确、摘要17错误，尚未声称修复）；补各knowledge阶段质量及深层恢复。其他后端认证／原生验收、用量与served model可观测性、原unknown对账及最终发布门禁继续保留。

独立包位于 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/review-severity-consolidation-20261017/review-severity-review-v2.tar.gz`，README列明在封存框架23a5之上应用overlay的顺序、安装方式和实际验证范围。框架包SHA `ae12ccf760b4db7ad9c2c83f16b49b1841c45e964f00de0fee031303de557cb8`保持不变。本切片零真实模型调用，未提交／push／发布／部署；完整目标仍开放。
