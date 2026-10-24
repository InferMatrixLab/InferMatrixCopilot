# 当前a10的凭据、代理与canonical admission控制 · 2026-10-18

框架保持收口，产品仍a10／24ac，无生产代码／AST变更。当前a10镜像03dccf46…的local与SSH执行位置均通过同一subscription_admission.admit_subscription／admitted_context控制，未调用模型。

## 已观察结果

使用原获授权的Personal-Agent Codex登录来源，canonical credential adapter在内存及临时worker通道中过滤凭据；未挂原cache／HOME，未导出refresh token。真实CLI仅执行login status，由现有transport认证命令进入共享sandbox执行；同一registry／provider代码处理local和SSH，没有新CLI runner。

两处返回native-subscription-admission/1并通过原proof／config／expiry／execution context校验。package release与image一致，各自config hash和proof root独立，不借用旧版本或另一个位置的admission。凭据hash／到期元数据保留，报告没有原token／account ID。

代理均由当前安装SDK的network_relay.model_proxy生成，host只提供固定chatgpt.com:443和api.openai.com:443路由。本地控制对两个允许authority完成CONNECT及证书hostname校验的TLS握手，握手代码不发送应用层HTTP请求；未允许authority返回403。不是服务端账号权限、推理、served model或费用测试。

## 原错误和资源关闭

local控制原认证／TLS结果已写入后，控制脚本用“socket文件不存在”判断关闭而退出1。Unix监听已关闭仍可留下文件；原失败脚本／log保留。单独只读连接确认原socket拒绝连接，再核对该新控制socket的device／inode并移除；未重跑认证来替换结果，没有删除或采用旧proxy。

SSH控制原进程正常exit0，服务由同一个context关闭，调用方移除自己创建的socket。两个临时proxy均已关闭，没有留下后台监听；旧proxy和旧paid环境未改。

## 准入范围

canonical admission成功在此证明离线CLI登录识别、明确credential source及执行context绑定；不证明服务端已接受当前token／模型请求。proxy TLS检查只证明这些观测时的匿名连接条件。没有模型thread／turn、API completion或模型质量结果。

这些proof绑定临时控制路径，不能直接复制进live campaign；临时proxy已关。实际比较需拥有自己的proxy lifetime、不可变profile／credential快照及新proof，随后重新核验完整manifest。当前主草案仍无live派发准入：保留519＋182提案需要701，但上限仍519；没有增加额度、释放spare或触碰原unknown。

两个位置的原结果、配置、失败／关闭证据与脚本 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/a10-auth-proxy-evidence-20261018/a10-auth-proxy-review.tar.gz`)仅供宿主审阅，不是模型输入。原auth-probe根保留在各自执行地点；这些路径不能替换为新的paid调用。

当前完整确定性配对保持有效，完整十层模型质量、旧unknown／History外层ack、深层恢复、其他vendor及最终release／rollout仍未完成。没有GitHub写入、commit／push或部署。
