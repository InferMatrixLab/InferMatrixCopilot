# 当前正常安装版主流程补证

框架保持封存；最终Copilot26bd／Bot3bbf新增独立正常安装SDK子进程、Direct／Strict及Bot发布恢复补验通过。产品无修改，生产行数／AST变化0。

## 本轮结果

- Copilot63项通过：Direct持久预留／执行／恢复／兼容入口、Strict完整工作流与类型化结果、应用workflow、发布资格／head漂移拒绝。
- Bot117项通过：统一provider端到端、provider run恢复、混合Direct／Strict、发布workflow／进程崩溃后对账及防重。两组零失败／错误／skip，driver进程已exit0。
- 正常venv内安装精确26bd及3bbf wheel，全部产品payload逐字节匹配。父进程及SDK子进程的import provenance均指向本venv site-packages，无产品源码注入。测试文件复制到独立目录，旁边src不存在；Direct fixture设置的PYTHONPATH因此不能加载产品源码。
- Strict fixture所需pr-review.yaml从精确安装资源原样复制，单独记录hash。pytest以独立配置和-I启动，不采用checkout pyproject的pythonpath。
- 依赖复用上次安装环境（symlink），不是fresh最小消费者证明；产品本身正常安装且不是symlink。模型CLI、LLM和GitHub边界均为原有受控测试替身。保留原测试字节，无放宽门禁。

result.json、input-proof.json、import-provenance.json、两组原始JUnit／日志、精确命令、fixture资源hash及完整测试输入hash是本轮证据。run.py记录构建／安装／验证方法；scratch路径要求不存在，重现应先选独立新路径，不能对原结果原地重跑。

## 范围和后续

本证据补足最后增量的正常安装SDK子进程Direct／Strict和Bot publication/recovery路径，不是新全量双仓、远程Docker、真实模型质量或所有深层恢复证明。e3eb完整报告和原质量样本保留原身份，不重绑定。

后续仍保留P1当前Direct／各knowledge质量、深层恢复与原unknown按原身份对账；P2细致预算／实际费用／served model、呈现、认证及其他厂商原生矩阵；最终配对release／pin与部署验收。未知付费调用不重派，未修改原账本或旧归档，未commit／push／发布。框架不重新开启，完整目标保持active。

证据：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/current-installed-mainflow-20261017`。归档SHA256：`f6e9637c38dbd1a1f8275224c424f194477fbeef1c06ffc1ac2c874b57f9fb35`。
