# 最终候选离线host安装包 · 2026-10-18

最终9855／24ac的双wheel及已验证依赖已封装为39个wheel的hash-locked离线host安装包。已在另一个全新CPython3.12/Linux x86_64 venv实际执行--no-index --find-links wheels --require-hashes安装，pip check及原public release SDK smoke通过；依赖版本与此前fresh验证环境一致。

requirements.lock逐包固定版本和SHA256，包含用于验证的pytest与pip；不依赖旧site-packages或源码PYTHONPATH。下载阶段可访问公开PyPI，安装验证阶段明确--no-index，不声称操作系统网络namespace被禁用。wheel hash提供完整性，不是publisher签名或published commit证明。

这是可审阅的host安装候选，不是published／deployable release。没有新commit、消费pin、发布或部署；source tree不冒充已发布revision。native CLI、Docker镜像、凭据和持续proxy／模型就绪不包含在包中，仍通过受信执行profile单独管理。该包不替代真实质量、深层恢复和在途／rollout验收。

安装目录 (host-local path: `/data/zhoutaichang/copilot/output/single-capability-completion-20261012/final-offline-candidate-20261018`)含README离线命令、manifest、逐wheel hash、实际原PID／退出码、依赖版本、release SDK smoke和完整锁文件。产品改动0，新增模型调用0，原519／spare／paid unknown保留。
