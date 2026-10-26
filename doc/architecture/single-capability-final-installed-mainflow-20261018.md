# 最终9855／24ac安装后主流程 · 2026-10-18

本轮不改产品，不新增模型调用，不更新正式pin或部署。框架交付保持关闭，完整验收继续。

- 选择114个不同用例，覆盖public SDK Direct／Strict、发布准备、knowledge SDK与portable wheel validators。首轮98 pass，Strict16个因输出目录缺少pr-review.yaml fixture产生setup error；复制冻结的playbooks fixture后，仅重跑该16个，全部pass。保留两份JUnit，不把第一轮描述为全绿，也不重复计算测试。
- 测试以-I从复制目录运行，清除PYTHONPATH／PYTHONHOME。产品来自安装wheel；Direct子进程额外断言SDK导入路径在site-packages且不存在PYTHONPATH。只复制测试、validator包装和冻结的playbook／adapter数据，不复制产品源码作为导入根。
- 实际Bot scripts/verify-release-sdk.py exit0，核对Direct1.1.0／Strict1.4.0／Knowledge1.1.0、durable review API、generation checkpoint、public操作、append验证及签名containment。本检查不执行真实模型或GitHub发布。
- 两个精确wheel的RECORD均核对通过：Copilot2340条、Bot133条。记录依赖版本；依赖复用，不能声称fresh-install。

最终wheel：Copilot98554010edec39df8f318f297b1d694d089d7c6cb094bdf9f7a7c8bcaa76e334；Bot24ac7cfba06139988757ee192179ef77f59346066a9e2917ab6102250e84989a。

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-installed-mainflow-20261018`)包含原始JUnit、fixture补验、复制文件hash、wheel RECORD、发布SDK smoke和依赖版本。完整源代码套件仍仅观察原PID3142657／3142674；本检查不替代它们的终态结果。
