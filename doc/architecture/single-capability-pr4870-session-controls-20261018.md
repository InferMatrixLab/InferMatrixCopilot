# 后续质量阶段：真实 PR4870 的 local／SSH session 准备 · 2026-10-18

框架保持[已交付](single-capability-framework-final-handoff-20261018.md)。本轮没有改产品、评测实现或原 manifest；当前产品仍为 a10／24ac。完成同一真实 PR4870 完整数据的生产派发前控制，并发现现有 native deadline 不足以覆盖这次 SSH 准备。

## 实际输入与控制路径

从已冻结 head 231b3ddb97c177e6cee50f16be5ab8e9dee9228c 的 clean source，使用原 write_bridge_spec、freeze_session 构造 4094 文件输入；包含源码及 Git 对象、可见 request／host／diff、1702 个完整知识文件和 verification sidecar。没有截短源码、减小知识树、加入 private labels 或替换业务 parser。

输入 envelope 142652214 字节，SHA256 7e43c65c569a82bb1c852e3d8684b22079ecb4e274400afc791f5c258a70dee5。这个是外层输入文件 hash；policy／bridge 的 session_inputs_sha256 使用其 canonical frozen_inputs 编码，不能混称相同字节身份。配置控制另注入 policy catalog。

控制 prompt 明确禁止模型；这是完整数据准备，不是完整业务提示词渲染或模型评测。profile 不挂账号或 proxy；因此不验证服务端身份认证。Personal-Agent 上的 local Docker 与控制机经共享 SSH sandbox API 都调用当前安装包的 prepare_completion_transport，而非另写 vendor CLI。

## 原进程结果

- local：成功，约 56.216 秒。
- SSH：成功，约 137.161 秒。
- 两侧配置接受 native-policy-request/3，唯一允许 MCP 为 infermatrix-tools；真正 bridge --check 包含 doc_read／doc_search。各侧 policy／bridge proof 绑定同一 context；两条路径共享同一 session input hash。
- 两侧各有 2 个 configuration_probe，model_call 为 0，无 model execution reservation。控制 attempt 均为 canceled_before_dispatch、native_call_sent=false、native_outcome_pending=false，不称作 review done。
- 控制机与远端 SDK 重新逐字节核对 2337 个产品成员，都匹配 a10 wheel。当前 Docker image digest 与原不可变 a10 image 一致；镜像内成员验证沿用该相同 digest 的已有证据，没有冒称本轮重新安装或 fresh 环境。

初次文件传输成功，但 SSH python -c 未转义使解压命令在 shell 解析阶段 exit2；当时 probe 尚未启动。原命令／日志与 exit 保留。随后使用独立解压脚本校验原已传文件，仅在原 local 控制目录尚不存在时首次启动 probe；没有重发未知模型请求。local 和 SSH 原控制进程之后均 exit0，没有观察超时后替换进程。

## 发现的 deadline 缺口

[现有草案](single-capability-case-knowledge-paired-draft-20261018.md)的 native_timeout_s 为 120、host_timeout_s 为 180。本次 SSH 配置／bridge 准备耗时已达 137.161 秒；180 秒控制通过不能代表该 120 秒 native 调用预算足够，更不代表仍有足够模型执行时间。

源代码核对：ArmRuntime.settings 将 native timeout 投入 SDK settings，execute_session 的同一个 absolute deadline 覆盖 configuration／bridge／模型。不能只增大 host_timeout_s 来掩盖短 native budget；也不能全局抬高 host timeout，因为 Foundation 的 32-call workflow 仍须满足 7200 秒上限。

下一项为已有 plan_for_case 加入明确有界的逐案例 deadline policy，并让 SDK／legacy host／workflow admission 共用同一校验；只给 review 冻结合适预算，其他 init 的原语义不变。再进行所需回归和正式准入。此次观察不证明所有未来运行都必然超过 120 秒，但足以拒绝把当前 180 秒控制作为原 native budget 的通过证据。

## 交付与剩余目标

原始结果、proof、命令与身份记录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-pr4870-session-controls-20261018`)；大型 inputs.json、压缩传输文件、native-policy-request 和 worker 记录留在原控制根，归档只保存绑定与 compact evidence。来源和 sidecar 仍由 canonical reader 验证。

本轮模型调用 0、生产行数／AST 变化 0；未改 519／spare、未新分配 182 次提案，未动原 paid unknown、History 外层 ack 或其环境。原配对草案仍不可派发。真实模型质量、完整 deadline／账号准入、深层恢复、其他后端及最终发布继续保留，未 commit／push／部署。
