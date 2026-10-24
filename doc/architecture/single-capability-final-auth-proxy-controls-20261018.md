# 最终9855原生认证／代理控制 · 2026-10-18

在最终SDK98554010、Docker镜像a0006918上完成两种位置的既有canonical admit_subscription／admitted_context控制。使用用户授权的Personal-Agent Codex缓存，仅检查离线login status；未输出或归档凭据payload。

- local控制完成chatgpt.com:443、api.openai.com:443 CONNECT与证书／hostname校验的TLS握手，应用HTTP请求0；example.com:443被403拒绝，未打开上游。随后离线subscription auth status与bound context一致。
- SSH控制使用独立临时socket，最终SSH部署profile上的同一离线admission／context核对通过。
- 两个临时代理均已正常关闭，专用socket在核对归属／监听关闭后删除。旧proxy与执行环境未动。local控制脚本在执行前明确加入自己的socket inode和关闭后清理；不重现历史a10中“context关闭但文件残留”的错误假设。

本检查使用native completion admission API；不能冒充独立session调用的账号证明，也不能将已关闭proxy当作可持续live readiness。实际模型调用仍需按其session／completion模式绑定同一受信profile、原生context和预算。没有服务端账号HTTP／模型RPC，不证明服务端认证、served model、usage或真实质量。

证据目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-auth-proxy-evidence-20261018`)含最终artifact绑定、profile、控制脚本、临时proxy关闭及admission metadata；无credential原文。产品改动0，新增模型调用0，519／spare／paid unknown保留。

新八类Codex双臂质量草案182次仍未分配；已向用户请求明确预算选择，未答复或选择暂不新增时不派发。完整目标保持，框架交付不重开。
