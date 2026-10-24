# Strict 完整应用的额度耗尽行为

框架已交付，当前Provider6629／Bot3bbf未变。本次补齐后续Strict预算策略的一项前置证明：在没有关闭深度调查、第二轮、逐评论验证或zero-yield retry选项的情况下，完整标准Strict应用能明确区分已完成调用与派发前额度拒绝。

使用真实Git PR checkout、公共SDK reserve／find／get_result、RunService、标准playbook、通用executor与实际provider／budget hooks。仅forge读取和container/native进程边界脚本化，执行worker为进程内以注入脚本边界；不声称已测试耐久child或真实Docker／模型。串行调查与验证并发1与现有冻结质量协议相同。

| 脚本额度 | 实际流程和结果 | 回执事实 |
| --- | --- | --- |
| 1 | investigator后，adversary前拒绝；blocked | 1条complete；拒绝记录sent=false、completed=false、pending=false |
| 3 | 三个调查pass后，第二轮前拒绝；blocked | 3条complete；相同明确未派发事实 |
| 4 | 第二轮完成后，逐评论验证前拒绝；blocked | 4条complete；没有把未验证评论伪装完整结果 |
| 5 | 同一单评论脚本流正常done | 5条complete，无拒绝，作为正控制 |

所有情况同业务键find保持原run，再次execute是already-claimed no-op，按既有契约返回0；结果仍保持原blocked或done，调用与账本行不变。退出0本身不是评审成功，必须读取类型化状态。来源是原测试模块的实际XML属性和trace事实，不是仅检查Ledger.reserve。相关account composition回归和精确安装包／RECORD／provenance也通过。

初稿遗漏脚本镜像的directory-input protocol8能力stub，真实preflight正确拒绝；尚无调用账本，不是产品缺陷。补齐测试边界后，首稿又误期望already-claimed执行必须非0，随后按真实no-op契约检查结果与账本。第三稿把所有额度都当作不足，但单评论脚本5次已完整完成；保留此正控制，没有增加候选或关闭流程以强迫预算失败。原失败报告与草稿均保留。

这证明的是当前完整应用的硬额度停止行为，不是任意Strict请求的最坏调用总数。候选验证fanout、修复、fallback、promotion等仍可能增加调用。以后真实实验可以明确选择预算上限并在耗尽时保留一次失败样本；不能承诺旧13或新22一定覆盖全图，也不通过缩减流程来保证完成。若选择每臂22、host300，至少workflow6630、phase6930、outer6960秒；这是期限下界计算，未新增该协议或准入，没有新增真实调用。

后续还须验证历史基线预算失败的原生契约、耐久worker恢复及实际Strict质量。原Strict／Discovery未知执行、旧配额和环境保留。完整目标不标完成。证据目录：output/single-capability-completion-20261012/strict-budget-stop-20261017。

范围补充：本次使用已有 evaluation-native 账户的 Refused 路径，不推广为所有领域 BudgetExceeded 的契约。XML 中四条 record_property／xunit2 兼容警告及原属性保留，不是产品失败。
