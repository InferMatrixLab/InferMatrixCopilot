# 当前e3eb／3bbf配对整合（2026-10-17）

当前候选完成独立确定性整合，另完成一个冻结Strict质量比较。框架23a5原交付、旧292e及1532初稿的报告／包保留原身份，不回写为当前版本通过。

## 代码及唯一归属

Copilot wheel e3eba87112145515ad93b92a5d0b378827e3488d519cceaa3c0c375d5a66ec0e；Bot wheel3bbf71df48a24eb05f6d07cb68fe145f8c2cd15b22f17a2f182dba4a7d001860。严重度归属review/severity.py，Direct／Strict生成、合并、复核及结果映射委托，内联说明和表已删除。任务状态指引仍由共享dispatch拥有，不放宽失败／recheck门禁。Bot业务、发布和回执边界保持。

完整双仓补丁可从原Copilot647e6e02／Bot1e70e983一次重建；包含新增、修改及删除，不需历史overlay。生产／AST变化对原基线与23a5切片分别报告，包含保留改动；减少行数不是发布门槛。当前只新增eval367明确额度修订，未改产品wheel。

## 当前精确验证

| 证据 | 结果与范围 |
| --- | --- |
| 完整Copilot源码test/ | 5831项，零失败／错误，3环境skip原样保留。 |
| 完整Bot安装版 | 2805项，零失败／skip。 |
| 当前完整evaluation安装版 | 410项，零失败／skip，含367修订；较早409报告对应冻结323 evaluator，不冒充新代码全量。 |
| skip逐项补验 | 同源test_evolution及test_knowledge_wheel_resources，在实际Personal-Agent Linux namespace／精确wheel资源中4项通过。原source no-skip gate仍false。 |
| 安装及资源 | 2434个配对payload字节核对，含1927个force-included资源；src/test与原输入manifest及主树匹配。 |
| fresh最小消费者 | 新venv、hash-locked file依赖、无MCP、无source-path注入及安装依赖symlink；SDK probe通过。 |
| Docker本地／SSH | 当前不可变image faa94e15真实stdio client正常读取与manifest篡改拒绝；流式stdout/stderr、idle、absolute、cancel、原结果回读和禁止重复派发均通过，零模型／无凭据。 |

控制机user namespace禁用和源码模式资源skip按同源测试补证，未改写原XML。首次本地Docker控制以普通用户访问socket权限不足，零模型；原失败保留，独立root控制使用不同路径通过，未改变产品或重复原native身份。完整结果在review-policy-integration-20261017/deterministic-closure.json。

## 限定真实质量证据

独立367协议保留旧323并新增每侧22；相同lease-store冻结head／知识／私有标签、相同原标准driver与review plan，双侧各7完成，共14，无本阶段unknown。candidate找对line18、返回fixed重查及typed完成，major/P1按“P2 or justified higher”接受；baseline同样找对且重查完整，但blocker/P0缺少critical依据。非盲host判定、不是统计或普遍质量提升证明。旧needs_review失败在本次两侧都未复现，不能作单因果归因。actual USD及served model仍unknown。

详细协议、原执行身份与局限见[single-capability-lease-strict-policy-native.md](single-capability-lease-strict-policy-native.md)。不释放旧未用额度，不重放旧unknown，不将旧Direct6629证据绑定当前e3eb，也不声明其余knowledge质量已通过。

## 后续优先级

P1：当前Direct及其余knowledge质量strata、深层恢复／原unknown对账。P2：本次实证的SDK诊断漏投影promotion用量（native账本与硬限额已计入）、Scan呈现及一般摘要引用一致性、厂商／Docker认证、其他后端原生矩阵。发布阶段：实际review、配对revision／pin、回滚与原在途环境保留、部署验收。

交付包review-policy-integration-20261017/current-paired-policy-review.tar.gz包含完整补丁、当前wheels、架构与清单、原始验证及受限质量证据。所有真实质量、恢复及发布要求保留；未commit／push／release／deploy，完整目标未完成。
