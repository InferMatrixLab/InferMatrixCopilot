# 最终配对全新虚拟环境验收 · 2026-10-18

Copilot98554010／ReviewBot24ac7cfb在全新venv安装后通过：114个选定主流程测试、发布public SDK smoke以及pip check。没有复用或链接旧site-packages，没有产品源码导入路径。产品成员2337／130均与最终wheel逐字节匹配。

先只安装双wheel及声明依赖（Bot依赖的kb extra包括cryptography），不装MCP或pytest，运行原paired verifier的minimal SDK probe：无MCP时public SDK可以导入，非法schema在预留／派发前拒绝且没有execution目录。之后单独安装pytest，运行已冻结的Direct／Strict、public knowledge、发布准备和portable validator用例；保留已有Direct子进程site-packages／无PYTHONPATH断言。首次完整运行114 pass、0 error／failure／skip。

依赖从公开PyPI安装，可使用pip下载缓存；不是从旧venv链接包。按既有环境版本约束安装，constraints.txt、实际dependencies.json和完整原进程日志保留；本检查不声称hash锁定整个第三方供应链。测试基础解释器／系统库继续使用主机安装，不声称全新操作系统。

发布SDK verifier核对Direct1.1.0／Strict1.4.0／Knowledge1.1.0及共享capabilities、append和签名containment。没有真实模型或GitHub发布；用例中的模型／GitHub effect边界受控。本结果不替代仍在运行的Copilot全源码套件或真实模型质量。

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-fresh-pair-20261018`)包含全新安装过程、每个原始PID／退出状态、隔离probe、JUnit、release smoke、依赖版本和安装成员核对。生产代码变化0，原519／spare／unknown不变。
