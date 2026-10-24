# 真实PR的local／SSH worker可见范围 · 2026-10-17

框架阶段保持收口。本次使用既有通用sandbox profile／同一Docker backend验证两组真实PR输入；只切换location，没有新建vendor／Docker特化实现，没有产品代码／AST变化或模型调用。

## 实际执行范围

当前Copilot1b36／ReviewBot24ac产品保持不变。使用不可变镜像`sha256:7656aac2801a341b150646a2629e7cddcbf5d681aca9afeb1ecb7fc750ac027e`，controller与Personal-Agent的安装包完整canonical fingerprint均为`ece933d2d51d2a7f382aefc01c9a71e268754089b4015dc8de89e1e4391b381e`。

SSH location从控制机通过标准remote sandbox协议执行；local location在具备Docker的Personal-Agent上由同一driver运行，外层SSH只用于启动该本地控制。没有将控制机描述成已有Docker，也没有改装或重启host daemon／其他Codex进程。

两组真实exact-head源码、冻结request／host packet／patch、完整原knowledge均进入输入。相同输入哈希分别用于两种location；PR5636／6062分别4470／4492个session文件。不是toy源码，没有替换成空知识或把私有oracle根一起挂载。控制命令仅执行Python校验，不执行vendor CLI，不挂载账号或model proxy。

## 已知结果

原结果与原命令 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/review-peer-worker-isolation-20261017`)／逐项验证 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/review-peer-worker-isolation-20261017/validation.json`)：

- 四项原控制均已知exit0：两组PR×两种location。实际容器核对head、冻结host输入哈希与source evidence patch；canonical verify_snapshot验证全部1702份知识及manifest。
- 源码和knowledge的实际输入挂载只读；写打开被拒绝。网络接口仅lo；指定账号路径、关键凭据环境变量和Docker socket不可见，profile没有credentials_source或model_proxy_socket。
- 指定的coordinator私有oracle根、控制机canary、远端宿主canary和原账号路径均不可读。远端canary在控制前后都存在且hash未变，未通过删除宿主文件制造通过。只上传显式列出的input payload／profile／driver，没有上传私有labels或canary正文。
- 每个原结果读取通过，原binding hash不变；同一execution再次execute被拒绝。没有替代运行、paid dispatch、SDK review reserve／start或GitHub写入；不使用这些控制去结算旧模型unknown。

以上关闭的是这批精确输入／镜像／profile的已知路径可见范围控制，不是所有潜在侧信道或完整模型工作流验收。冻结shared inputs的profile仍未获得模型派发准入；账号认证、实际模型调用和质量裁定不能由本控制替代。

## 原失败与取件

初始remote包检查的shell quoting错误在执行Python前返回exit2；改用stdin后只读检查成功，不是sandbox能力失败。local控制driver实际exit0，但取件launcher随后用普通SCP读取root-owned结果目录失败并exit1。保留原日志及initial_local_launcher_all_passed=false；用已授权sudo cat只读取回同一原result，未chmod／chown／重跑控制。四项原执行没有pending。

## 后续范围

P1：完成完整独立labels／新finding裁定与容差，核对原baseline／candidate完整渲染，冻结实际model profile及新的不可变预算准入后，才执行真实比较。PR6062已有知识回归的限制继续保留。其余八层、原519全部分配与spare、旧unknown及原环境不改写。

深层恢复／精细预算、其他vendor原生认证、最终配对release／pins、在途保留与rollout继续分阶段完成；框架不重开，完整目标未宣布完成。
