# 当前a10候选完整源码回归与安装补验 · 2026-10-18

框架保持收口，产品仍为Copilot a10c3484…／ReviewBot24ac7cfb…；本轮无生产代码／AST变更。当前模型质量、深层恢复和发布要求继续保留。

## 已完成的当前版本验证

从完整交付patch重建tree1092d774…后，原完整Copilot test进程pid1587075已terminal exit0。JUnit共6017项：6013通过，4项环境skip，0 failure／error。没有重跑替代原进程，也不把4项skip改称未发生。

三个skip是源码路径缺少已安装wheel资源；另一个是当前控制机禁用user namespaces。在Personal-Agent独立的新安装环境，以当前a10／24ac wheels执行对应资源／真实namespace及producer-scope补验，5项全部通过、0 skip。该环境使用稳定Python3.12.13，没有源码路径注入；全部2467个双包payload成员匹配当前wheels。依赖复用，不声称fresh安装。

这补齐了上述四项的当前版本对应运行证据；它不是所有平台或所有native backend的验收。原完整源码套件仍按6013 pass＋4 skip记录。

## 下一次比较的前置工作流准备

使用既有prepare_world，在原baseline与当前安装SDK中分别准备Screening、Foundation、History、Strict depth及Lightweight depth，共10个前置检查全部通过。脚本化前置gateway保持原fixture职责；实际ModelGateway.call_json被禁止，真实调用数0，测量阶段未启动。

五组upstream head／tree均在双臂一致。初始knowledge clone文件除两份各自版本的validator wrapper外相同；没有据此宣称所有生成overlay、域状态或最终模型prompt相同。完整payload冻结与模型质量仍待后续。

所有写入只发生在新的fixture／测试环境；产品knowledge、旧paid运行环境、旧unknown及原519分配未改。

## 配对ReviewBot完整检查

已为当前a10 SDK／24ac Bot创建独立安装测试环境，从原Bot full patch重建tree841e653e…，启动完整tests进程。SDK从已安装a10包加载，只有被测Bot源码进入PYTHONPATH；旧Bot源没有修改。

原进程和最终结果目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-paired-bot-full-20261018`)。本页封包时该进程仍运行，不能宣称配对完整套件通过；最终以原process.json和JUnit为准。该后续检查不重开框架，不替换已有旧版本结果。

当前验证归档 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-deterministic-readiness-20261018/a10-deterministic-readiness.tar.gz`)含原JUnit、skip原因、安装namespace补验、前置准备及精确身份。完整代码仍见[当前代码交付](single-capability-session-deadline-delivery-20261017.md)。

八层模型比较仍须完整input／label／profile／retention batch与预算准入；182调用仅为未分配提案，原519不变。Discovery／Module-card按既有策略与用户后验选择保留；真实模型、原unknown／History ack、深层恢复、其他vendor及最终release／rollout均未宣称完成。没有GitHub写入、commit／push或部署。
