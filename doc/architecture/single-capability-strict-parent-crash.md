# Strict SDK父进程突然退出恢复验证

已通过独立安装包检查：父进程在原调用仍reserved、worker存活时os._exit(91)，新SDK按原business key恢复同run／同worker／同launch，释放原脚本响应后正常完成，不新增启动或重复调用。产品21ab／3bbf不变。此测试比此前SDK对象close/reopen更强，但不是实际付费网络、远端主机或电源故障的全矩阵验证。

新增加1个eval测试文件，生产代码零修改；安装版407项旧报告保留，新增测试独立1项通过，不合成为伪造的408项全量报告。首版fixture环境继承错误在fetch阶段失败，零模型／脚本调用，原失败保留。

归档 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/strict-parent-crash-20261017/strict-parent-crash-review.tar.gz`，SHA256 `8021df5450909dc902c904f1e4e45832448b72b3f04a468d98f24485980f5e31`，10成员hash核验；result.json与XML包含父／worker PID、原run与最终状态。原真实执行／协议不改写。完整质量、恢复矩阵、源码全量和发布验收仍开放。
