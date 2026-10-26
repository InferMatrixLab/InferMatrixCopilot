# 7095 PR-history终态与内容裁定 · 2026-10-17

原session59273／PID3464539已正常结束，两臂各3笔原生调用全部complete，pending／新unknown为0。两侧应用为dry_run，aggregate均approve；**两侧完整冻结案例质量仍未通过**，因为都只采用formatter合同，缺少原标签要求的core合同。框架交付保持封存，完整目标开放。

## 已验证的机制

5313项绑定未变；两臂原生回复与应用记录一致，私有标签未进入模型包。aggregate的实际Git完整diff及SHA、base/head、所有已采用rule／commit均对应，完成后resume不新增调用。每侧实际选择PR101／102，但只为102准备知识commit；aggregate正确覆盖这一份已采用commit，不证明被跳过的101也满足正向覆盖。

六份当前规范原生归档均通过TraceStore原生attempt验证，原请求system／prompt及reply可读且校验有效。核验前后trace文件及逻辑付费行不变。原SDK虽然domain原生omit，本次独立显式留存策略在规范宿主保留了请求／回复；这次是真实原生回执，区别于准入前模拟RPC边界的控制。默认拒绝策略未放宽。

## 实际内容

| 冻结合同 | 两臂实际结果 | 宿主裁定 |
| --- | --- | --- |
| Engine.step返回13、重复调用保持实例状态 | 两侧均no_upgrade，理由是“Engine属于model，未提供Engine model rule page” | 未覆盖。实际输入已提供core component owner，model_scopes为空；提供的普通类源码／测试没有证明其是model scope。不能凭类名臆造model专属归属，进而丢弃该合同。 |
| fmt返回37、保持模块状态、保留重复调用检查 | 两侧均将TOY-I3归tooling，引用实现及实际测试行 | 满足这一条。验收字段描述所需检查，不是虚构已经执行CI。 |
| aggregate完整性 | 两侧均批准各自最终完整diff，只有TOY-I3及PR102 | 对已采用部分有效，不能把approve提升为双owner完整语义覆盖。 |

该owner-scope误判在原始647与7095各一次运行中均出现，不归因为重构引入的回归，也不作因果或统计无回归结论。已知合成语料、原标签未改、宿主未盲，不能宣称新独立语料。canonical owner routing在受控两owner检查中允许普通class合同；当前缺口是提取模型的scope解释，不增加宿主另造规则／纠正循环来制造覆盖。

| 原生用量 | 候选 | 基线 |
| --- | --- | --- |
| 输入token（已含cache） | 17575 | 17588 |
| 输出token | 720 | 782 |
| cache-read输入 | 10752 | 10752 |
| reasoning输出 | 381 | 494 |

六笔实际USD和served model仍unknown；规范trace的served_model均为空，不能用requested model填充。CLI计数不代表厂商内部turn或货币账单。保留全部旧427分配及新12，本轮用6、余6不释放／不复用，保守累计439。旧History拒绝样本、Foundation完整性失败、原Strict／Discovery unknown均不动。

终态审计 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-current-20261017/terminal-audit.json`)、原生留存核验 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-current-20261017/native-retention-audit.json`)、内容裁定 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-current-20261017/adjudication.json`)及model-evidence.json保留实际文本和身份。审阅包及校验见同目录outcome-artifact.json。

下一项P1是版本化通用model-scope指导：区分普通实现类与明确有证据的model scope，保持model专属归属限制，不硬编码本玩具类名，不改旧标签，不更换原unknown／batch身份。实际模型效果须在独立新协议下验证，不能原样重派本轮。其他十层、深层恢复、预算／发布回执、vendor及immutable paired release／rollout仍开放；未commit、push或部署。
