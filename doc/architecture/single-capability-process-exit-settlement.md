# Process exit settlement follow-up (2026-10-16)

本切片推进已登记的超时子进程清理 P1，修复两个有确定复现的缺陷。框架与前一共享 pending 交付包保持冻结，完整目标不缩减。

| 复现 | 旧实现 | 当前行为 |
| --- | --- | --- |
| 子进程已退出为 zombie，但父进程尚未 wait | kill(pid, 0) 成功，被误记为 live | 同一 procfs 状态读取识别 Z/X 为无运行中工作；活进程正向控制仍返回 live |
| 记录在案的 detached 子进程关闭输出、忽略 TERM，SIGKILL 完成延迟 | 零 TERM 宽限期同时把 KILL 结算等待置零；父进程／管道结束即可先返回 | TERM 宽限仍为零，已记录身份的 KILL 结算保留一秒窗口；受控延迟下返回前工作已停止 |

唯一实现仍在 process_tree.py：一个 `_proc_status` 解析状态／父 PID／启动时间，`_proc_stat_ids` 与 `_alive` 委托；祖先查询删除独立解析主体。已有 PID／启动时间及 ancestry 校验继续使用，没有按未知 PID 猜测归属。provider、命令、测试运行器与 compatibility alias 继续共用原机制，没有新增业务回收循环。生产变化相对 a45 为一个文件，净 10 行／54 AST 节点；Bot 未变。

源码及精确安装包的辅助命令、provider pipe/spool、测试 substrate、PID 复用／归属、命令成功后台策略检查通过；原失败 run_shell 案例在独立安装解释器中少量重复通过。新 wheel 的 Direct／Strict E2E 及公共 SDK smoke 也通过。模型／GitHub 为替身，进程控制使用实际本地子进程，无真实模型调用。离线 hash-required 安装、pip check、RECORD 和全部生产 Python 字节与冻结源绑定通过。

Copilot wheel SHA256：`b79b94e5f44c5599a1c5a1be30632df0ed9290893821a39d3c1ca4e7535c6143`；Bot 保持 `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`。证据目录：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/process-exit-settlement-20261016`；result.json、原始 XML、delta.json、overlay.patch 与 wheel 可审阅。先应用框架完整包609dfaaf，再应用共享 pending 包bd61edae，最后应用本切片 overlay。各旧包和其原始失败状态保持不变。

原框架 policy 失败没有记录当时的 proc 状态，不能断言它只由 zombie 引起；原始失败保留，新的稳定复现与通过仅支持上表修复。before.xml 是初始测试缺少 Path import 的收集错误，已标为无效；before-v2.xml 才是两项产品回归证据。

本切片不证明任意调度下的完整清理：终止窗口结束后仍有真实 survivor 的状态传播、观察间隔中生成／脱离的未记录子进程、非 Linux 行为及深层崩溃矩阵继续登记。原未知付费调用按原身份对账，不重派；真实质量、其他后端、release／pins／部署仍未完成。没有 commit、push、发布、原运行环境或账本改写。
