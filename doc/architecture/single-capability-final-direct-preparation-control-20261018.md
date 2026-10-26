# 最终Direct应用入口／SSH Docker准备控制 · 2026-10-18

最终9855／24ac实际public SDK预留进入Direct应用流程，经canonical readonly structured session、provider和原始native accounting，完成Personal-Agent SSH Docker配置及只读bridge准备，然后由显式control预算钩子在模型派发前拒绝。控制通过，工作流为blocked、exit3；这是预期停止，不能当成完成review或模型质量通过。

- 输入为真实PR4870 head231b3ddb、完整源码／Git、冻结host证据及1702文件knowledge snapshot。没有改用缩小的合成输入。原request／readonly intent／运行树保留在专用目录，身份hash列于final-identity.json。
- 最终Docker镜像a0006918、SDK98554010。生产配置及bridge产生2条configuration_probe；真实doc_read／doc_search bridge检查通过。预算钩子收到harness_session与绑定的原生attempt，finish facts为sent:false，未产生模型execution，pending:false。
- 从修正后的SDK构造到停止用时193.43s；模型期限300s。未挂载凭据或proxy，无实际模型调用；不验证服务端认证、真实usage或完成review结果。
- 初次控制错误混用config与legacy settings_overrides，在预留前被SDK拒绝。failure.json保留；使用环境配置后进行第一次预留，未重派未知原执行。control acquire／prepare只作观察和拒绝，没有替代生产transport、bridge、解析、repair或accounting机制。
- 本检查只证明实际Direct入口选择现有受限session并能准备最终SSH运行环境。local底层历史控制不自动重标为本版本实际Direct local端到端验证。

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-direct-preparation-control-20261018`)。生产代码改动0，没有真实模型或GitHub publication。原Paid unknown／519／spare与其他执行环境不变。
