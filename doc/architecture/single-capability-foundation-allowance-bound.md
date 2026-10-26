# Foundation 后续评估的调用与时间边界

两套已安装产品实际运行相同应用控制：两个 owner × 六 facet，在非 unlimited、subscription_generator=True、无 fallback、没有额外 feature jobs 的路径，调用2次生成＋12次审查，共14次；两个批准 facet 与十个拒绝 facet 保持，已完成批次恢复新增调用0。模型回复和判断全部脚本化，原生模型派发0；这不是质量比较，也不是已准备的新独立标签／源案例。

按每调用 host timeout300秒，保守串行 workflow wait为14×300＋30＝4230秒，另留300秒准备，建议外层phase至少4530秒。这是静态最坏等待下限，不证明实际模型延迟或整体质量。原有205调用额度全部保留；若未来独立配对需要28调用，总配置将为233，超出现有206，当前没有准入、扩额或释放旧额度。

不能把14次推广到 unlimited correction。该模式有初始提取＋最多三轮修正，并保留原输入绑定／archive要求；最粗的同规模四整轮估计56调用、16830秒，已超出现有7200秒整工作流门限，尚非可执行协议。其他后端／独立模型家族验证仍按用户指示后置。

本次发现另一个评估边界缺口：run_paired对Direct／Strict phase做整工作流时间下限检查，但init phase当前仅有正数／7200秒上限，没有按声明调用上限和host timeout拒绝过短外层phase。下一步补这个派发前门禁，原冻结helper／manifest不改写。

Baseline第一次控制误用了当前源码validator wrapper，旧安装包缺相应新API，前置阶段被校验器拒绝；保留该控制身份，之后新目录使用原baseline便携validator，原生调用仍为0。原始／最终控制驱动分别保存，fixture和validator字节绑定。

证据：`output/single-capability-completion-20261012/foundation-allowance-bound-20261017/result.json`。完整目标继续，未改生产代码。
