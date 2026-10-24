# 最终Direct本地Docker准备控制 · 2026-10-18

在Personal-Agent以local sandbox location运行最终9855应用／Docker镜像a0006918，实际public SDK预留进入Direct prepare／model步骤，完成配置和受限bridge准备，再由同一个预算钩子在模型派发前拒绝。没有另建业务runner或vendor机制。

原控制进程77.21s后已终止，但postcondition错误假定execution目录必须不存在，产生AssertionError／exit1。真实local Docker适配器在构造时创建空execution目录（sandbox/docker.py:73）；SSH控制端没有该本地目录。原失败保留，不将原进程改称exit0，也没有重跑控制。

只读核对原attempt9213be0c后，确认：目录为空，没有execution identity／intent／worker receipt；canonical attempt为canceled_before_dispatch；model_call trace仅1条取消的控制attempt，native_call_sent:false、native_outcome_pending:false；预算finish同样sent:false。两条configuration_probe均model_calls:0，真实doc_read／doc_search bridge准备通过。只读inspection脚本exit0，结果单列inspection-result.json；它不是新执行。

冻结输入取同一PR4870／head231b3ddb、host evidence与完整1702文件knowledge，SHA清单及source bundle保持原值；source bundle在local控制中重建Git工作树。工作目录、issued context／prompt与Git序列化不同，因此不声称与SSH的session input hash逐字节相同。bootstrap只解压并逐文件核对冻结数据，未复制标签／凭据／proxy。原本地运行树保留在Personal-Agent专用路径。

这与[实际Direct SSH控制](single-capability-final-direct-preparation-control-20261018.md)一起证明最终版本按local／SSH位置复用同一应用／session／provider准备路径；都在派发前停止，不证明真实模型认证、质量或完成review结果。

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-direct-local-control-20261018`)含原失败、原进程、只读核对、冻结输入hash、配置／bridge证明及受信profile。生产代码改动0，未改519／spare／paid unknown，未commit／push／部署。
