# 当前 bridge-pin 候选的沙箱准备 · 2026-10-17

框架保持交付；本次推进后续实际运行准备，没有生产代码变更（产品行数／AST增量均为0），未重新宣布完整目标完成。

## 精确身份与可复用配置

- Copilot wheel：`e98e4c087ea01853a4d088cd701d1dc8ccd8cd5a48117eadd286e45e548c8796`。ReviewBot未改，仍为`24ac7cfb…`。
- 新镜像：`sha256:0085a291a331d33dd084728b48f2eeebee4c293bad0904562369719144f9f3a2`。
- Personal-Agent新环境：`/home/ubuntu/single-capability-sandbox/bridge-pin-runtime-20261017/env-policy`。新建产品环境；依赖通过原环境symlink复用，**不是fresh安装验收**。旧paid环境、镜像、执行记录与proxy均未升级／重启。
- 证据与配置目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/bridge-pin-runtime-20261017`)。`settings-profile.json`绑定同一镜像供completion与session使用，没有credentials_source和model_proxy_socket；这是无认证的预检／控制配置，不是已获模型派发准入的生产配置。

## 已执行结果

新产品在远端安装与容器中的2337个payload成员逐字节等于wheel；pip check通过。Codex版本为0.162.1，binary SHA为628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1。controller／remote／container应用policy摘要相同。

同一个通用sandbox执行API、同一个新镜像，分别以SSH远端与Personal-Agent本地位置运行：

- 真实MCP stdio读取固定知识成功；篡改页被manifest检查拒绝，未返回篡改内容。
- stdin、stdout／stderr流式传递及伪receipt文本保留；idle、absolute和cancel路径经过真实Python子进程／容器验证，终止回执已确认。
- 原结果可读；缺少确定结果时仍保留pending语义。重复execute被拒绝，原绑定哈希未变化。dispatch前cancel不创建派发权威。

四个probe命令均terminal exit0，原始结果见controls/result.json与各partial／log。根拥有的local结果通过sudo cat读回，没有chmod／chown或重跑取代原执行。

实际安装的canonical provider对completion和session配置预检均通过，没有创建模型worker或派发模型。初次报告断言把原始配置当成规范化配置，因默认ssh路径和可选null字段失败；原脚本／错误记录保留，最终比较使用同一canonical normalizer，没有修改产品或放松校验。

## 尚未证明

没有挂载账号、模型代理，没有执行真实模型调用，没有GitHub写入或部署。本轮CLI只执行--version；未执行实际Codex effective-configuration probe，未验证认证、served model、费用或真实Direct／Strict完整输出。控制使用合成页及Python后代进程，不把它称为真实模型质量、完整PR工作流或所有故障恢复验收。

后续首先检查当前镜像的真实native有效配置／认证与不可变profile，再准备完整labels和预算准入后执行真实比较；十层质量、原unknown／外层ack对账、深层恢复及其他vendor继续保留。旧519分配及spare不变；本轮新增真实模型调用0。

归档证据包 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/bridge-pin-runtime-20261017/bridge-pin-runtime-review.tar.gz`)含构建／安装／配置脚本与原结果。当前可审阅双仓完整代码包仍见[bridge修复交付](single-capability-strict-bridge-pin-delivery-20261017.md)。框架不重开。
