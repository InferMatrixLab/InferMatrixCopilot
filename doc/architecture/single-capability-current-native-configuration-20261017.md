# 当前镜像真实Codex配置与登录识别 · 2026-10-17

框架保持收口，本轮推进后续运行准入准备；无生产代码修改（行数／AST增量0），未派发模型，不宣布完整目标或发布完成。

## 身份与结果

当前Copilot wheel `e98e4c087ea01853a4d088cd701d1dc8ccd8cd5a48117eadd286e45e548c8796`，镜像`sha256:0085a291a331d33dd084728b48f2eeebee4c293bad0904562369719144f9f3a2`。沿用[该镜像已完成的local／SSH控制](single-capability-bridge-pin-runtime-20261017.md)，没有升级旧运行环境。

通过安装后的同一CodexTransport.configuration_probe和通用sandbox_profile_for_config，在local／SSH两处执行真实Codex0.162.1的initialize、config/read、configRequirements/read。未创建model thread／turn。两处完整effective配置与requirements相等，canonical CodexCompletionPolicy.validate_configuration均通过：gpt-6-sol／high、指定冻结catalog、web disabled、内置工具／hooks／apps等全部关闭、无额外MCP和startup项。

当前managed requirements包含allowRemoteControl=false、allowedWebSearchModes=[disabled]、featureRequirements.hooks=false，以及api／chatgpt两个允许的login methods；没有把实际非空requirements伪装为空或忽略。

原配置结果读取成功；相同原执行位置的第二次configuration_probe被SandboxPending拒绝且不能fallback。原binding哈希保持。控制已terminal exit0，无未知模型结果产生；既有unknown未触碰。

## 现有登录缓存

使用当前安装包的canonical snapshot_source读取Personal-Agent已获授权的原登录；只记录kind／expiry，不记录token或account ID。观测时snapshot identity为有效codex-chatgpt（expires_at=1793113369）。

独立的当前容器通过WorkerCredentials临时通道获得过滤后的external-token snapshot，真实`codex login status`返回0并识别为ChatGPT登录。未挂载原auth.json／HOME，未导出refresh token；未改动原缓存，证据不保存原始payload。容器network none、无model proxy；仅在worker临时目录物化必要凭据并按原机制清理。

这证明当前凭据通道和本地CLI登录识别可用；不证明服务端已接受token、订阅额度、served model或推理。元数据与登录结果具有观测时效，真实派发时仍须重新绑定有效凭据及准入。

## 后续准入与验收

本轮只验证restricted completion配置；带唯一只读MCP bridge的session组合已有MCP控制但尚未进行本轮真实native effective-config组合探测。下一步补该组合，并检查proxy／服务端连接条件，冻结完整独立labels、profile和预算后再执行真实质量比较。原519累计分配及spare保持，本轮新增模型调用0；不得复用旧admission授权变更后的profile／image。

十层质量、原unknown／History外层ack、深层恢复、其他vendor、最终pins／shadow／rollout继续保留。没有GitHub写入、commit／push或部署。

原始结果、脚本与输入 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/bridge-pin-native-config-20261017`)；核验归档 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/bridge-pin-native-config-20261017/current-native-config-review.tar.gz`)。local根拥有结果只通过sudo cat读取，未chmod／chown或重新执行取代结果。
