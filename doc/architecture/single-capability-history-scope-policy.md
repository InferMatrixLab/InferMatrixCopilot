# History scope指导 · 后续P1切片

框架交付保持封存；本切片不重开框架连接。完整目标及真实质量／深层恢复／其他vendor／发布验收仍开放。

已确认的原7095比较中，两臂均把普通实现类误认为AI model scope，漏掉已提供component owner的合同。本次只更正提取指导：由当前源码和实质PR证据判断语义scope，不凭类／函数／对象名臆造模型；真正模型专属合同仍必须使用已提供模型owner，没有相应页则解释不采用。空model_scopes不允许把真正模型专属知识转移到组件。确定性路由及model_scope_valid保持原实现，不增加宿主纠正或生成循环，不硬编码旧案例名称／返回值。

新批次scope_policy_version=1，纳入input options／digest，选定PR后、派发前持久化；系统提示SHA由现有generation检查点绑定。已有批次缺版本字段等价0，保持旧提示精确字节、旧指纹、原选择和结果。版本错误在读入时拒绝；未决批次不能通过版本修改重派。b333 per-PR日志仍只读适配，新的shared journal与历史模型身份均保留。

安装后定向检查覆盖新批次实际选择提示、已完成旧批次结果复用、旧unknown原提示SHA与意图不变、旧已知派发前拒绝恢复时继续使用原提示（单项安装补验）、改版本拒绝且检查点字节不变、非法版本拒绝、真实模型owner及variant路由限制、共享review／预算／任务恢复。初次源码选择49通过、1处测试错误地预期blocked结果，实际更早抛InitError；原日志保留，修正测试后通过当前正常安装完整选择。依赖复用，无产品src注入；受控模型边界，不是真实模型质量证明。

生产变更仅init_history.py；行数／AST及源码与wheel核对见production-delta.json。框架交付401dbd／Bot3bbf的完整patch与验收范围保持不变；本包提供在该精确tree上的增量patch和当前Pwheel，不能把历史全套报告重绑定为新版本。

本次零真实模型调用，不释放、复用或重置旧439分配。新的实际质量效果须冻结新的请求／比较协议后验证；旧两owner完整案例仍未通过，不修改原标签或重派原coordinator。Foundation冲突／失败不变量、其余质量层、深层恢复及最终发布继续按原清单处理。未commit、push或部署。

验证与代码目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/history-scope-policy-20261017`)；result-selector.json、input-proof-selector.json、commands与原日志保留各自范围。
