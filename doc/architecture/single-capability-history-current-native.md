# 7095 PR-history独立原生比较 · 2026-10-17

> 当前状态：原执行已终态，6笔complete、pending0；原生留存及aggregate绑定通过核验，但双owner质量覆盖未通过。见[终态裁定](single-capability-history-current-native-outcome.md)。下文保留准入／启动时范围，不表示仍在运行。

框架交付保持封存，完整目标开放。本阶段不修改生产代码；只扩展评测账本的显式history-current-20261017修订，并将现有额度边界测试参数化共用。

原7095 Foundation session75210已终态：22调用complete、pending0、完整案例未通过，原报告／标签／闲置额度均保留。本比较是已知合成History语料的新软件版本实验，不是原History状态恢复、失败样本替换或新独立语料。

当前候选7095与原始647使用[冻结输入](single-capability-history-current-inputs.md)的两个精确PR、原标签和source tree。测量全部提取、owner验证、完整aggregate及最终树；只有已完成阶段才验证resume不新增调用。没有脚本生成或假批准作为模型质量证据。真实GitHub发布禁用。

## 冻结与准入

- 显式留存：retain-request-result绑定独立0700运行根、输入manifest及其SHA；当前domain选retain-private，原SDK的omit在规范evaluation host显式留存政策下转换。默认没有该政策仍拒绝耐久派发，不能因使用Docker就推定允许记录。
- 旧SDK实际集成：原始安装的ModelGateway经真实LegacyGateway和retention策略运行；原调用方omit标志不变，规范model record保留system／prompt，RPC context绑定留存摘要，配置变化在再次RPC前拒绝。仅RPC边界模拟，真实vendor派发／原生回执为0，不认证质量或native archive。
- 既有正常安装／Docker payload证明复用精确7095：2335成员一致，镜像ac19f97234bcfc835b9e062da5f7fb7e052074a4123e5e09ea4d537e5291cb26、Codex0.162.1。新执行位置根独立；新CLI／账号及代理TLS／拒绝控制通过，不是推理权限证明。
- 新修订保留全部427旧分配及闲置，仅增加12次成对CLI上限，每臂6。正常无fallback流程为2次提取＋1次aggregate；余量不能授权unknown替换。金额和厂商内部turn不由CLI计数推出。
- 整段workflow期限1830秒（6×300＋30），phase2130、stage2160。初版1530被canonical deadline检查拒绝；原manifest及失败日志保留，尚无派发时修正，不改变期限规则。
- 额度／留存／输入协议的52项定向验证通过；未将旧413完整评测结果改名为当前439修订全量。双臂受控History接线、完整diff绑定、标签隔离和完成后恢复为0新调用的证据单列，不当作真实语义通过。

正式manifest `5657b8bcc833e9c7ac48ceb31fe66bffdc0d84decc69eaa53d8e95d65f41ec91`，5313项冻结绑定。准入记录于2026-10-17 01:52:31 UTC写入；准入审阅包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/native-quality-history-current-20261017/admission-review.tar.gz`)118成员核对，SHA256 `9880d699e80815daa8f216143a5b8ffffd8b309c844a7bde3460ec8c664ac2d2`。allocation.json记录准备时状态；当前是否运行以live-admission／原coordinator和原账本为准。

原协调进程于01:52:59 UTC首次启动，session59273／PID3464539。启动不等于完成或原生质量通过；继续观察原句柄，超时不授权重启、另起付费调用或复用旧状态。实际接受内容、原生回执、费用／served model未知及最终aggregate将待终态后分别审阅，**本页不宣称质量通过**。

后续仍保留全部十层质量、Foundation完整性、原unknown原位对账、深层恢复／预算／发布回执、其他vendor及最终paired release／rollout要求。本轮无commit、push、pins或部署。
