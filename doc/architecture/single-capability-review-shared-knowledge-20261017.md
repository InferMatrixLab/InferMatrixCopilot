# 真实PR共享knowledge绑定 · 2026-10-17

框架保持收口。本次完成两组真实PR输入的共享knowledge绑定及安装SDK读取验证，产品代码／wheels未改，模型和durable runtime均未启动。

## 唯一输入与宿主／沙箱边界

从原Copilot基线647e6e02读取完整knowledge，调用GitSource.export保留1702个regular files及原执行位；逐文件Git blob身份核对。使用既有build_manifest和verify_snapshot构造／验证同一快照，不选择空知识、不删减规则来适配案例。内容共10,313,714字节；文件系统块占用大于内容量，不能混称沙箱payload超限。

knowledge tree SHA256：`d338c32ba22af684429186f31ab822a3912aa9d6afefad32160ce599c2abe509`。

宿主两侧KNOWLEDGE_ROOT都指向snapshot根，以加载MANIFEST.json；native共享执行的knowledge_by_arm都指向snapshot/knowledge，由freeze_session将相邻manifest一起映射到/tmp/inputs/MANIFEST.json，知识映射到/tmp/inputs/knowledge。两者路径含义不同，不能将snapshot根直接当native知识子树。1719份request／host／knowledge文件由同一binding记录逐项哈希固定；该记录明确不是派发准入manifest。

## 已执行验证

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/review-shared-knowledge-20261017`)包含原命令、SDK计划与读取结果、字节投影和bindings。

- 原baseline安装的2351份既有冻结文件重新核对匹配。baseline与当前SDK都在源码目录外以-I运行，隔离HOME、无PYTHONPATH注入；public DirectClient.plan与绑定context的read_document读取两组真实请求，每组7份文档，内容哈希、字节数、路由、必读指南一致。规划期间明确阻止subprocess和网络；没有模型调用。
- 两侧resource_revision不同，原因是该身份还包含各自安装的adapters；没有强行使其相等。相同knowledge输入不表示所有产品版本或未来模型输出必须相同。
- 两个真实exact-head源使用canonical freeze_session与stage_inputs完成本地字节物化，分别4462／4484个session文件。物化后的1702份知识与原manifest完整再验通过。没有替换成小型模拟源码。
- 这证明canonical session投影／本地物化，尚未证明实际Docker mount或完整Direct／Strict执行。共享输入binding可供两种模式使用；本次SDK读取探针只执行Direct规划，不将其称作Strict workflow通过。
- 本次没有产品或真实知识页编辑，386份产品src仍与原完整整合一致。没有新增paid dispatch、reserve、GitHub写入或部署；原519及未用额度、unknown、旧环境保留。

初次纯准备driver有函数名错误，首次读取探针未处理SDK的sha256:前缀；原失败日志保留，修正验证器后两侧通过。这些是准备／验证代码错误，没有对产品做绕过。

## 评估范围与剩余项

原基线knowledge明确包含PR6062来源及spatial-shard边界规则。因此该PR是已有知识的回归输入，不能称作未见案例泛化；保留完整知识，不通过删规则制造盲测。已知留出批准ID／完整批准正文未出现在知识Markdown，但该字面扫描不证明无训练污染或完整标签独立。

两组peer oracle仍只支持特定机制／carried finding，不等同完整新finding标签，更不替代原计划需要的actionable defect／resolved discussion等案例覆盖。其余八层及原成功、失败、unknown的原范围保留。

下一项：完成完整独立标签与裁定／容差，核对baseline／candidate完整渲染，冻结实际profile和新预算准入，验证worker可见根／Docker投影后再派发。真实质量、深层恢复、其余后端及最终发布仍按完整计划推进；不重开框架阶段。
