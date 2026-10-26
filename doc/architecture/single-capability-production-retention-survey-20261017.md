# 生产保留清单：只读刷新 · 2026-10-17

采样时间：2026-10-17T06:25:49.316855+00:00。本任务只读SSH采集服务与账本，没有部署、切换admission、清理环境、轮询provider、结算回执或新增模型调用。

当前实际ReviewBot revision为`d7f41394e3ede18d004bd1a8961e783319958ba1`，Copilot revision为`9761bd34653cbca2de4b4a03a88dfb9fb64e70dd`，解析后的release为`/home/ubuntu/project/.omni-reviewbot/releases/d7f41394e3ede18d004bd1a8961e783319958ba1-9761bd34653cbca2de4b4a03a88dfb9fb64e70dd.WQkkl0`。对应venv存在。昨天采样的release为`/home/ubuntu/project/.omni-reviewbot/releases/57ac7b2098795799410c5e81b769b9f5b118d212-34bba93b29e7199dd1cfad8c2ddf8b25346e7e16.RUbKfz`，已不同；不能把昨天环境或本任务候选的验证套用到当前生产运行。

| 实例 | 服务 | 排队／运行job | 中断／非终态attempt | 带provider引用attempt | 新身份字段／owner绑定 |
|---|---|---|---|---|---|
| vllm-omni | active | 71 | 79 | 1607 | False / 0 |
| vllm-gr | active | 0 | 0 | 11 | False / 0 |

vllm-omni中79条所列attempt均为interrupted，版本字段是unknown／infermatrix-copilot==0.2.0；这不够定位原release、位置、profile或是否仍有付费执行。不能据此推断79个模型仍在运行，也不能把缺少新字段当作未派发证明。vllm-gr没有此类记录。

completed且无GitHub receipt的1026／4条是检查分类，不等于丢失发布：可能包括历史shadow等情况，不能自动补发。两个实例各自采用只读SQLite一致性事务，不是跨实例／provider的全局原子快照。

必须保留实际原release／venv、两实例state roots和已有provider／native留存；不得为了rollout删除旧环境。provider profile／原业务键和原生终态回执仍未完整盘点，原unknown仍需在其原环境对账。当前生产记录不能自动迁成当前新候选的执行身份。旧无身份重启的本地复现已发现重复派发风险，防护修复与验证见后续legacy-review-restart证据；本调查不修改生产账本。

机器快照与采集代码 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/production-retention-survey-20261017`)绑定snapshot／collector SHA，仅输出服务字段、业务标识与受限绑定字段，未读凭据。发布前必须重采样并完成原运行绑定／保留或排空、真实immutable pair／pins与回滚验收。完整保留清单和发布均未完成，框架交付不重开。
