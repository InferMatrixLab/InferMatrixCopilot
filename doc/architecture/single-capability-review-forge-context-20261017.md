# 冻结forge讨论类型与渲染比较 · 2026-10-17

框架保持收口。本次修复评测适配器的实际证据改写：旧适配将所有PR讨论变成inline，并把作者换成frozen-shadow-fixture。PR5636原有1条issue comment／4条inline被变成5条inline；PR6062只有2条issue comments，却被变成2条inline。before.json保留原复现。

## 修复与兼容

strict_host.frozen_gh新增显式context_version=2：issue comments、review summaries回到pr view的对应集合，只有inline进入inline API投影；保留已知作者、base_ref、labels，不把original_line冒充当前head的line，不编造branch名称。它仍只做冻结数据投影，不新增review业务循环、模型修复或Github客户端。

旧v1默认及原字节合同保留；真实两PR的旧pr view／repo view／inline输出与修复前逐字节相同。新版通过Strict driver的--forge-context-version 2显式选择，verify_installed_arms也转发；该选择须进入新比较的不可变stage参数／harness绑定，不修改旧付费manifest或默默重解释旧输入。未知版本／讨论类型拒绝。

## 实际验证

原命令、源码与证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/review-frozen-forge-context-20261017`)。31项定向测试通过，包括实际forge CLI子进程接收v2、旧默认wire、缺少讨论的旧repo读取、unknown拒绝与已有durable guard。原baseline和当前安装SDK均在-I、源码目录外运行；原_pr_context_bundle只在GitHub读取边界注入同一冻结adapter，阻止网络及subprocess，没有替换产品renderer。

两种安装的同名原renderer使用v2输入时输出完全一致：PR5636为6567字符，PR6062为1652字符。候选实际frozen_pr_context序列化同一host evidence，分别12017／4051字符，且讨论对象全部保留。因此PR上下文不是字节相同：原renderer裁剪description到4000、单讨论到700、每种来源最后10条；PR5636有4条讨论超过700。原renderer也不渲染thread_ref／original_line等候选JSON保留的元数据。没有强行改baseline生产代码来抹平这些差异。

这只是PR-context子输入的原始渲染比较，**不是完整Direct／Strict模型prompt比较或质量验收**。当前head line未冻结就保持None；branch／commit timeline／linked issue独立证据不足也不编造。raw render与hash保留，未来裁定必须考虑这些信息量／裁剪差异。

产品386份src及1b36／24ac wheels未变，生产行数／AST变化均为0。本次变更仅eval adapter、driver参数、测试与文档；旧完整eval仍保留旧输入范围。本次没有模型、reserve／start、GitHub写入、commit／push或部署；原519分配、spare和unknown保持。

## 下一步

冻结包含v2选择的完整Direct／Strict预派发prompt与schema，补齐独立完整labels／新finding裁定及容差、实际model profiles和新预算准入，再执行真实比较。相同knowledge及已完成的指定路径local／SSH控制保留精确原版本范围；不称其已覆盖本次所有完整prompt。PR6062已有知识回归限制及其余八层要求不变，完整目标保留。
