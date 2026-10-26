# History新批次：保留有证据的检查义务

来源：499原生回归已终态，candidate34aa的来源已连接，但TOY-I4遗漏明确保留回归检查的要求，被aggregate拒绝；baseline仅203导出，完整案例均未通过。原结果及499全部分配含闲置保持，不自动重跑。

当前只改Copilot init_history.py的生成策略及版本选择：新批次v4把所选合同中已有证据的回归检查义务保留到验收条件，区分结果示例与保留检查本身；上下文测试没有component归属时不引用为rule evidence，也不因此丢掉owned实现／实质PR证据支持的义务。没有证据就不新增测试要求，不虚构执行结果。owner／citation验证、aggregate门槛与流程、source-row渲染、预算和修复次数不变；仍无额外宿主repair循环。

0／1／2／3版本各三次提取＋一次aggregate的完整实际请求对原34aa逐字节相同；持有同一旧prerequisite baseline，不能把新生成的baseline当旧批次。未知原请求保留而不重派；原实际运行仍需其原runtime／profile／policy环境，版本分支不允许跨release冒充恢复。新4默认绑定新输入身份，旧记录选择原版本。

当前wheel **85efe57beee28c21d818a8bb3a05958edc679fcd5083c9515ff8ad515984abe4**，Bot仍3bbf71df。与34aa相比全部payload集合相同，只有init_history.py内容变动；源码与wheel逐模块核对。当前源码受影响行为、依赖边界和正常安装定向回归通过，安装不注入产品src、复用依赖，不能称fresh最小环境。初始安装／旧请求比较因测试fixture未复制validator包装而setup失败，补齐同字节夹具后按同wheel重跑通过，原失败XML／log保留；首次no-isolation build缺hatchling，标准隔离构建成功，原日志保留。

生产相对34aa为1文件净+12行／+12 AST节点。完整双仓patch包含此前保留工作，不全部归于本切片，净减行不是门槛。当前策略只完成确定性接线与兼容验证，尚未证明真实模型会满足完整性；499失败不改写，34aa完整配对／Docker证据仅属其原包，不重绑定新wheel。

当前代码／wheel／验证目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-obligation-policy-20261017`)包含完整binary patches、新增／删除文件、原基线私有index重建、必要验证及旧实际请求。完整patch各相对647e6e02／1e70e983应用一次，不要叠加到已有变更树。[框架交付](single-capability-framework-delivery-20261017-final.md)保持封存，架构和69能力／33删除归属不变。后续完整质量、深层恢复、其他后端及最终配对／release／pins／在途保留／回滚／发布仍开放。本切片零模型调用、未commit／push／创建PR／发布／部署。
