# 安装包知识贡献文档缺口修复

当前Copilot wheel `b7e6087611da0ba40b19a1069c8813157cf45a4e0093af9de0d786490853811c`，Bot3bbf不变。框架保持封存，本切片关闭1448安装补验发现的P2贡献文档链接缺口。

## 问题与修复

knowledge内既有相对链接指向仓库`doc/knowledge/contributing`。旧wheel只打包knowledge及单独的_runtime schema，缺少相邻贡献文档，导致六条带anchor链接失败；源码模式通过。此前1448和26bd的失败报告与原字节均保留。

pyproject新增一条force-include：现有`doc/knowledge`原样打包到`infermatrix_copilot/doc/knowledge`，保留knowledge与doc的相对布局。24个既有文档与源文件逐字节一致，不新建知识页或修改正文，不复制validator实现。原有payload全部字节不变，391个Python文件未改；唯一生产配置变化+1行，生产Python／AST增量0，Bot未改。

## 精确验收

在独立正常安装环境中，原完整123项安装选择（包含原失败的anchor检查）加3项便携validator／资源／containment检查全部通过：126项零失败／错误／skip，没有排除旧失败测试。原anchor测试未修改。新增测试证明贡献文档和schema在安装布局可读，但KnowledgeDocs读取`../doc/...`仍因越过原knowledge root而拒绝；打包资源不扩大模型读取权限。

两种validator命令及结构化runner在无src／doc的临时workspace运行，采用包内同一canonical schema与validator，exit语义保持。Direct／Strict预留、执行／恢复、应用组合和发布资格检查沿用原安装组，通过受控模型／GitHub回复，不是真实质量比较。

产品正常安装而非源码链接；所有产品payload核对一致。测试副本无相邻src，不通过source-path注入；依赖复用旧环境，不称fresh最小消费者。本轮未重跑Bot套件、完整source、远程镜像或真实模型。Bot包保持3bbf，1448的Bot117及e3eb全量证据保留原身份，不冒充新候选全量。

## 交付和下一阶段

`packaged-knowledge-guidance-20261017`包含相对1448配置／测试／文档overlay、当前wheel、资源输入hash、payload差异、正常安装原始JUnit与重建证明。旧包和失败报告不回写。

当前无新增模型调用、预算修订、旧run重派或发布。后续优先回到P1当前Direct／各knowledge真实质量与深层恢复；其余预算、served model、其他后端和最终配对发布验收仍开放。此项P2资源缺口关闭不等于全部打包／质量／发布要求通过。完整目标继续active。
