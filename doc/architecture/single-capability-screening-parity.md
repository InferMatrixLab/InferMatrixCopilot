# Screening 失败样本的当前包核验 — 2026-10-16

当前精确2d27安装包，在独立新工作区回放保留的候选 generator 输出，仍以 `index_link_broken repos/toy/_index.md: README.md` 拒绝。原 baseline、较早 candidate 与当前包对同一输出的拒绝结果一致。使用当前安装 SDK／canonical validators，无源码产品路径注入；只消费记录中的两份 generator 回复，没有派发模型或重放原付费／未知执行。

这份样本证明结构校验拒绝边界保持一致，没有证明新的生成质量或整个 screening stratum 通过。原生两臂均未提出可执行规则候选，也没有 native whole-workflow rule judgment。已有 source-authored 补充判断不能替代该覆盖。

因此下一步是单独冻结有代表性的非空规则候选、独立标签与预算，再评估原生生成／筛选。不能通过放宽链接检查让坏产物通过，也不能仅凭 candidate 曾 blocked 就认定重构引入拒绝策略回归。已有失败、缺覆盖及 served identity／invoice cost 缺失继续保留。

证据目录为 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/screening-current-2d27-parity-20261016/`。`result.json` 绑定原始记录、旧两臂报告及当前 wheel 身份；`screening-parity-review.tar.gz` SHA256 为 `81f2e5095cbca53b44c693b78d76685247045fc159c58d02751b2fe631ce7789`。这是应用行为回放，不是新增原生推理证据。没有产品／测试修改、实际模型／GitHub 调用、原账本／环境修改或发布。
