# 预览审批、维护者输入与旧记录兼容 · 2026-10-17

框架交付保持封存。本切片修复完整整合发现的兼容性问题，完整重构目标继续开放。

- 唯一读取与回执校验所有者：kb_service/preview.py。缓存、Discovery目录验收、portable回执和发布保持checked模式。
- 普通非portable新dry-run通过统一阶段策略读取未经批准的维护者编辑，保留路由补齐和harvest校准标签流程。实际内容绑定链路身份；旧未决预算／质量调用在新派发及改写检查点前拒绝。
- 原始647 dry-run没有prepared JSON。普通旧预览可作为未经批准的新草稿输入；旧Discovery只将原来持久化的两个哈希转换为同一校验器的回执，不从当前磁盘补造审批。没有证明的旧缓存／发布仍拒绝，保留原环境处理。
- 旧完整源码报告保留12失败、3资源环境错误和1skip。15个失败／错误已逐个映射到当前wheel通过的同名案例；当前相关安装215项及资源3项通过。原报告不改绿；此前Bot／评测完整通过和Docker／namespace证据仍属于59a5。

可审阅补丁、精确wheel、原始报告及运行说明 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/legacy-preview-receipt-20261017/README.md`)。当前生产增量相对59a5为3文件、+43行／+334 AST；没有新增付费调用或部署。最新完整配对、真实质量、深层恢复、其他vendor和最终发布仍待分阶段验收。
