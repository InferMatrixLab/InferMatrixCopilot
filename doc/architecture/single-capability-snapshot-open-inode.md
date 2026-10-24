# P1：快照稳定性修复候选

原框架交付6629／3bbf保持不变；本页是独立后续候选21ab4259／3bbf，不代表完整目标或新候选已最终验收。

两次零模型标准Strict重现将变化路径定位到run下的knowledge-context.sqlite。诊断中inode、权限、大小不变，mtime／ctime变化，原目录为fuse.aliyun-alinas-efc。原始失败未记录文件名，不能反向证明原失败必然由同一路径引起，也不能仅凭这些观测证明底层文件系统根因。

旧实现将打开前的lstat时间戳与读取后的路径属性比较。新增确定性失败对照证明：仅模拟过时的打开前时间戳、字节不变时，旧wheel6629仍拒绝。修复只改canonical sandbox.inputs：通过O_NOFOLLOW／O_NONBLOCK打开文件，校验身份和regular-file类型，以同一fd读取前后的fstat检查稳定性，最后确认路径仍指向该inode；限量读取按实际字节计量。读取期间真实写入、替换、符号链接和FIFO置换保持拒绝。没有跳过SQLite证据，没有延迟或自动重试模型调用。

源码与精确安装包各74项相关检查通过，零失败／跳过；386项生产Python成员和资源逐字节匹配当前源码及安装包，pip check通过。独立安装包下的真实标准SDK worker完成快照，然后诊断钩子在模型派发前明确停止。模型和container dispatch被禁止，identity／image边界为替身，因此这是工作流快照验证，不是实际模型或新镜像验收。

生产改动1文件，+28行／+262 AST节点；两个测试模块随包交付。新wheel SHA256为21ab4259659fd4784743f1b447a8d1a1105070e4738c5f71828d848e6103ebc4。两仓全量回归已启动，尚未终态，不将旧全量证据绑定到新wheel。后续还需安装组／实际remote新候选补验和真实质量；原未知执行、原Strict失败与预算不修改、不释放、不重派发。

诊断第一次脚手架期限设置不足，第二次远程替身接口错误，均作为诊断失败保留。第三、四次复现成功，第五次验证修复越过快照失败点。安装环境最初复制到FUSE耗时过长，复制进程显式终止；不使用不完整副本。最终独立本地venv使用旧环境依赖的符号链接，产品由新wheel正常pip安装，无产品源码路径注入；依赖未重建，不声称完全独立依赖下载验收。

原框架仍可审阅运行，本候选未commit、push或部署；原归档和协议保持不可变。

证据目录：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/snapshot-open-inode-fix-20261017`。focused-result.json记录验证范围；后续全量证据独立记录。


## 新候选后续整合补验

21ab4259／3bbf：新provider安装包下Bot完整2805项通过；配对SDK smoke和两个安装资源检查通过。实际Personal-Agent新image `sha256:cdfba3fa6ed8cc30a43c83ad7a5f6738c9f59b2ea1a01b0ac1c2104133500033` 的local及SSH四类控制（complete、idle、absolute、cancel）通过，原结果回读和绑定保持、重复派发拒绝，模型调用为零。原8fe镜像与原环境保留。Copilot源码全量及已安装产品的评估工具全量仍在原进程中运行，未宣告整合验收。详细身份与范围见snapshot-open-inode-fix-20261017/integration-partial.json。

运行补验归档：`/data/zhoutaichang/copilot/output/single-capability-completion-20261012/snapshot-open-inode-fix-20261017/snapshot-open-inode-runtime-review.tar.gz`，SHA256 `28184f89c852c24c7905fdd67a2f2ef62d032e852748efc887df9615c4015c14`，27成员逐项hash核验。包不包含仍运行的全量日志或不完整XML；不得据此宣称全量通过。


## 全新消费者与真实namespace补验

新配对21ab4259／3bbf的安装版evaluation完整407项通过，无失败／跳过；全新hash-locked基础消费者安装不含MCP／dev extras，产品和依赖正常pip安装，无安装目录依赖symlink，公共SDK非法schema在预留前拒绝。实际Personal-Agent Python3.12.13上4项namespace／producer scope／资源检查通过，2433项产品文件逐字节匹配。namespace测试依赖复用原安装发行版（symlink），两个产品正常从精确wheel安装，不将其误称为全新依赖安装。源码test/全量仍在原进程79793运行；最终skip／失败尚未知，不提前声明全部已补齐。证据归档 `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/snapshot-open-inode-fix-20261017/snapshot-open-inode-installation-review.tar.gz`，SHA256 `075db275ef437fe68cefd7298c079d68e75df1d043527afd526857665e6557d8`。所有检查零模型调用，完整目标、质量与发布验收仍开放。


## 21ab配对确定性整合终态

原源码test/全量已终态：5810项，零失败／错误，3个环境skip按原样保留；同候选实际namespace的相同测试及安装资源补验覆盖其环境限制。安装版evaluation407项、Bot2805项、独立父进程crash新增1项保持各自报告范围。fresh minimal消费者、SDK及实际local/SSH通过。测试后386项生产Python与21ab wheel仍逐字节匹配。结论仅为记录范围的确定性整合（带精确补验），不是无skip原始报告或完整目标完成。详情deterministic-closure.json；新发现bridge知识manifest校验差异在独立工作树修复，不纳入本旧候选。
