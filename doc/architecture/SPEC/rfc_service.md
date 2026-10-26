# rfc_service/ —— 规范

<!-- verified-against: 2026-10-16 -->

`可移植 RFC 应用、授权工作台与受限 Agent · refactor-status: ok`

## 职责

提供仓库无关、支持多个 RFC 的共享应用：起草、导入、明确发布与纳管、工作跟踪、
关联实现验证和人工验收。HTTP、CLI、SDK 与 MCP 共用应用权限和生命周期；宿主模型
负责调查与撰写，服务本身不要求模型凭据或仓库知识 adapter。

本包规范覆盖其全部实现模块，路径映射遵循 SPEC 的包目录规则。

正文展示在 Markdown 解析前移除独立 HTML 注释（包括 roadmap 内部元数据），并保留分段，
避免紧邻表格的注释变成可见数据行。草稿原文不变；围栏和缩进代码中的注释仍展示，原始 HTML 仍禁用。
路线图空白区域通过 Pointer Events 拖动滚动视口，节点仍保留点击和键盘操作；取消或释放指针结束拖动，重置同时恢复缩放和滚动位置。

## 功能与模块边界

| 模块 | 独占职责 |
|---|---|
| `models` | `SourceRef`、`Principal` 与传输无关的应用/提供方错误 |
| `drafts` | Markdown 草稿模板、保留原文的跟踪解析、依赖校验与确定性进度投影 |
| `store` | SQLite schema、事务、审计、操作 claim 与心跳租约 |
| `application` | 身份、仓库/RFC 权限、草稿/工作/验收动作、操作队列与后台协调 |
| `changes` | 独立于聊天会话的人工编辑预览、冻结候选和幂等审阅提交 |
| `runtime` / `service_status` | 队列唤醒、worker 心跳、授权诊断、连接验证与分页摘要 |
| `model_status` | 不含模型原始事件的额度、限流、认证及可用性状态 |
| `providers` | GitHub、AtomGit v5、允许根目录内的 Markdown/Git 访问与发布恢复 |
| `http` | 固定资源列表、会话、同源 HTTP 传输及受保护的旧 roadmap 路由 |
| `client` | CLI、MCP 与公开 SDK 共同使用的延迟加载本地／HTTP 客户端；SDK 仅重导出 |
| `cli` | 独立服务命令、用户请求、文件交换、前台 worker 与显式迁移入口 |
| `migration` | 旧 Personal-Agent 跟踪状态的只读比较、备份和幂等 sidecar 导入 |
| `chat` | 私有会话、独立任务租约、受限模型工具、完整候选提案与校验 |
| `translations` | 受权限过滤的双语缓存、独立队列与隔离 GLM 调用 |

## 公开契约

- `RFCService(state_dir, providers=None, roots=None, clock=...)`；
  `bootstrap_admin`、`authenticate`、`dispatch`、`process_pending`、`sync_due`；
  `build_service(state_dir, config_path=None)` 从执行主机配置构建提供方及可选模型适配器；
  `ChatService.dispatch/process` 管理独立聊天能力，服务可注入 `chat_agent`。
- `dispatch(principal, action, payload)` 是所有业务入口的共享授权点。动作族包含
  `users.*`、`tokens.*`、`grants.set`、`repositories.*`、`sources.preview`、`chat.*`、
  `rfcs.*`、`operations.*`、`audit.list`、`service.settings/configure`。
- `rfcs.get` / JSON export 返回完整的可见记录；`rfcs.status` 返回去掉源正文和候选
  列表的紧凑进度；`rfcs.suggestions` 按可见候选分页，limit 为 1–100。
- 提供方实现 `get_source`、`get_item`、`publish`、`find_publication`、
  `update_source`、`discover`。源引用携带提供方、仓库、种类、标识与规范 URL/相对路径；
  观察区分作者、所有者与评审者，并返回版本、head SHA 和原生/规范状态。
- `create_server` / `serve` 提供 HTTP；`main(argv)` 提供 `infermatrix-rfc`。
  API 版本来自包的 `RFC_API_VERSION`，独立于 review API。
- `migrate(service, principal, source_dir, repo_id, rfc_id, apply=False)` 默认只比较。
  只有显式 apply 才备份并导入；返回的绝对备份路径属于本地 operator 结果。

## 不变量

- **私有聊天与明确编辑。** 会话绑定用户、仓库和 RFC；历史、事件、工具上下文及
  提案读取重查当前所有权、RFC 和来源权限。独立 SQLite 队列在服务内最多运行两轮，
  每个会话一轮，模型 I/O 在事务外执行。Zcode GLM-5.3-Flash 通过隔离 complete
  调用受应用控制的查询/提案工具，每轮最多四次调用和 180 秒；取消、撤权、过期租约
  和重启均不能提交迟到结果。历史按最近 20 条及字符预算裁剪，明确告知裁剪。
  审计和通用操作结果不包含聊天正文或原始模型事件。

- **提案版本绑定。** 原文替换锚点、来源行范围和稳定任务 ID 组成候选；服务校验
  依赖、角色、RFC revision、手工草稿摘要和完整候选摘要。人工编辑也进入预览与确认，
  用户确认后一次事务保存并为已有来源创建 `rfcs.update_source` 操作。无来源草稿
  不自动发布。未涉及的正文、内部注释、人工删除、证据和决策历史保留；模型不能
  宣布实现完成、验收通过或豁免。设计与条件修改使相关验证失效。

- **持久来源写回。** 等待、失败或结果不确定的写回保护已保存正文，后台仍可更新
  关联实现观察。写前重查身份、来源版本和候选摘要，写后读回；重试先核对远端结果，
  避免重复写入。已知冲突保留候选并要求重新审阅，不自动覆盖远端。页面分开显示
  已保存、等待同步、已同步和来源冲突；任务 sidecar 不写进上游正文。

- **持久聊天界面。** 侧栏在页面内容容器之外，编辑弹窗复用同一面板，移动端全屏。
  导航、局部刷新和语言切换保留输入、历史、滚动及任务；来源上下文使用未清理原文。
  发送语言决定回答语言，记录与差异不自动翻译；章节/节点/任务/条件有讨论入口。
  聊天更新不得重建现有 SVG 或丢失未提交草稿。

- **轻量交互。** `view=summary/detail` 为可选投影；默认 SDK 与导出契约保持完整。
  建议由授权分页接口读取。来源权限索引只存活于当前事务，撤销后重新计算。
  通用静态资源支持 gzip/ETag；登录页和私有数据保持 `no-store`。操作结果直接
  更新视图，复用未变的 Markdown 和 SVG 布局，更新状态/负责人而不重画；退出
  清除内存视图。保存过程只序列化模型一次，并保留原有规范化版本摘要。

- **路线图交互。** 保留源 Mermaid 的 LR 轨道、关系与上下文节点，工作状态、
  新增依赖和验收来自当前跟踪模型；明确的依赖决策覆盖源关系，人工删除与新增
  工作会反映在图中，不固定轨道数量。
  源指令与回调不执行。节点提供受角色限制的工作/验收操作、键盘访问；支持
  缩放与下载，SVG 中的工作链接返回受保护的 RFC 页面。
  贡献者通过 `rfcs.work` 的 `mark_done` / `clear_done` 及状态更新记录有身份与原因的
  人工实现状态；人工记录优先于 PR 观察，不能改变验收结论。解析、同步和聊天编辑
  保留人工事实与历史。`rfcs.graph` 的 merge / rename / unmerge 保存 RFC 级视图分组，
  使用稳定特性 ID，不合并源正文、任务、验收或证据。分组可跨轨道，投影保留边界依赖、
  隐藏内部边，全部成员实现才呈绿色。分组读取受当前权限过滤，写入重查贡献者权限，
  可用 expected_revision 拒绝过期操作。分组与任务深链保留操作能力，状态变化复用 SVG。
  Ctrl/⌘ 点击或修饰键加 Enter/Space 按稳定任务 ID 多选，重复节点计数一次；合并弹窗
  预选对应成员。选择只留在当前 RFC 的会话内存中，刷新与切换语言保留，撤权、切换
  RFC、退出或显式清除后移除；不触发来源写入，也不导出临时高亮。
  移除与恢复工作使用页面内原因确认表单，不依赖浏览器原生 prompt；绑定当前 RFC
  revision，取消不写入，仍沿用维护者权限、人工删除意图和历史保留。

- **日常工作台。** 默认紧凑 Outcome、图/任务列表与 Details/Agent 检查器；目标、
  设计、全局验收和参考资料按标签页读取。检查器仅显示明确关联任务的验收条件。
  视图筛选、选择、缩放与输入状态独立于内容投影；私有表单 checkpoint 按用户、
  仓库、RFC 隔离，重新授权后才恢复，退出或失去访问后清除。所有写操作采用页面内
  表单确认与就地错误；UI 内置双语目录，翻译网络故障不得阻止登录或加载。

- **共享审阅提交。** `rfcs.changes.preview/apply` 将人工编辑冻结为有身份归属的完整
  候选，绑定 RFC/source 基线、摘要和幂等键。人工与 Agent 复用同一保存和持久来源
  写入事务；来源正文直接 update 返回 `review_required`。升级和后续同步保护尚未
  审阅的本地差异，不能静默覆盖或自动对外发布。故障、冲突、超时与重启保留候选。

- **服务可用性。** 纯读取使用读事务，摘要列表支持服务端筛选和可选分页，权限索引
  在请求内复用。新队列任务唤醒 worker，进程间一秒轮询兜底；小时来源核验保持独立。
  最小公开存活检查与已授权服务状态、管理员诊断分离；错误保留 code/request ID，
  来源配置和实际连接能力分别呈现。workspace_v2 开关允许回滚界面，保留数据及 URL。

- **组件工作台。** 详情按成果、目标与范围、路线图与工作、设计、验收、风险与参考
  组织。共享浏览器投影使用 Markdown 标题 token 与原始行号，保留层级；中英文规则
  集中配置，未分类与历史内容折叠保留。工作描述仅挂载到可见的稳定特性编号。
  已合并实现与验收独立统计；最近三项成果必须有通过结论及未过期的版本/环境证据，
  来源性能陈述不得转为已验证事实。长章节按需渲染，正文版本缓存复用内容节点与图表。
  原文编辑在有权限的独立弹窗内完成，保留未提交输入并校验保存版本；不新增 API/表结构。

- **双语同步。** `language=en/zh` 为详情、列表、分页和更新结果增加受权限过滤的
  `translations` 展示映射；原始字段不变。`?lang=en/zh` 选择页面语言，浏览器仅持久化
  公开语言偏好，不保存 RFC 译文。新增 SQLite `translations` 表按 RFC/文本摘要/目标语言
  隔离队列、缓存和租约。后台通过已登录的 Zcode 调用 GLM，模型 I/O 不持有 DB 事务，
  不阻塞跟踪队列；执行前和返回后校验有效身份、来源访问和当前文本，陈旧结果丢弃。
  代码/标识/链接/数值保留，译文不得修改验收事实；失效响应退避重试并展示原文。
  公开 `/roadmap-languages.json` 只返回随包 UI 字面量，私有 RFC 译文不能进入公开目录。

- **正文渲染。** 内容组件、草稿与导入预览使用随包交付的 markdown-it 15.0.2 渲染
  Markdown；禁用源 HTML 与图片加载，链接只允许 HTTP(S) 或当前文档锚点。
  原文保留用于编辑、发布和导出。

- **记住登录。** 浏览器会话与 HttpOnly Cookie 均保留 30 天，服务重启后继续有效；
  Cookie 不含个人令牌。退出登录、令牌撤销或到期、用户禁用立即失效。

- **仓库中立。** 提供方与外部仓库名来自注册表；本包不含服务仓库知识字面量。
  本地提供方只访问注册根内的相对 Markdown 路径或明确 Git 对象，拒绝目录穿越、
  不安全的文件路径与符号链接；源参数不能重定向提供方凭据到任意主机。
- **权限在共享应用执行。** reader / contributor / maintainer 由持久化身份与仓库
  grant 决定，RFC ACL 只能缩小范围。请求、源预览、所有投影、后台操作及远端 I/O
  后的提交都重新检查当前身份与权限；调用者伪造的 admin 属性没有权威。
  已注册私有 RFC 的源、候选和关联数据不能通过别名或另一条投影绕过 ACL。
- **身份与凭据分离。** 用户 token 以摘要落盘，session 绑定 token，禁用/撤销/到期
  影响后续请求和排队操作。提供方 token 只来自 operator 配置的环境变量，仅放认证
  header，不进入 URL、用户动作参数、错误或访问日志。首位管理员只能 bootstrap 一次。
- **预览与明确意图。** draft/import 不发布；发布要求 `post=true`、覆盖标题和正文的
  当前内容摘要及幂等键，可用 `expected_revision` 绑定跟踪预览版本。排队不等于发布
  成功，源变更/预览冲突必须显式恢复或重新预览。
- **外部写入可恢复。** 同一 actor 的幂等键绑定同一请求；SQLite claim、租约续期和
  提交时租约检查协调进程内外 worker。创建携带操作 marker，重试先扫描恢复；无法
  完成恢复扫描时不得假定未发布。`uncertain` 不自动变成新的创建请求，显式恢复仍
  使用原操作身份，并重新授权执行凭据。第三方 issue 更新是读/校验/写/读回防护，
  不承诺提供方没有提供的原子 CAS。
- **原文与跟踪 sidecar 分离。** 解析与迁移不重写源正文，保留稳定 feature ID、
  历史移除 tombstone 和 sidecar 工作。文件导入及 Markdown 文件导出保留行尾；
  Markdown export 交换源正文，JSON export 包含跟踪状态。跟踪变更不自动改上游
  issue 正文。
- **发现不等于验证。** 发现按页读取原生 summary，完整保留候选，不逐个请求 PR
  detail，不把 closed 推断为 merged。关联 PR 经 `get_item` 权威验证，同一链接每轮
  只读一次。成功扫描提交后推进扫描开始时刻的 cursor，并保留短重叠；失败或不完整
  扫描不能推进 cursor 或伪造新鲜度。
- **自动补充有边界。** 模糊关键词只负责召回。自动应用需要明确 RFC 引用和唯一的
  既有 feature，或明确既有 track 与精确纳管范围，并受每 RFC 上限约束；其余保持
  suggestion。自动补充不能指定所有者、依赖或验收定义；工作变化使旧验收证据失效。
- **实现和验收独立。** 必需 PR 全部 merged 才是实现完成，混合 merged/draft 是
  partial；issue、文档 URL、Git commit 存在及作者身份不能当作合并或验收事实。
  验收由维护者作出，passing 需要当前 evidence 的 revision 与 environment，waived
  需要理由；只有实现与验收都完成才返回 complete。
- **前台与后台读同一状态。** 纳管绑定明确 writer 和授权凭据；凭据失效时停止自动
  刷新并显示重新授权的 next action。发布、关联验证、发现与验收证据分别记录新鲜度。
  配置远端工作区失败时返回错误，不能静默创建本地替代库。
- **HTTP 传输不创造权限。** 静态登录壳不含 RFC 数据；全部数据和旧兼容路由先认证。
  cookie 为 HttpOnly / SameSite=Strict，HTTPS 部署加 Secure；cookie 写请求核对显式
  public origin，不信任代理头。非 loopback 部署要求 HTTPS public URL。仅服务固定
  资源，不公开数据库、备份或任意磁盘路径。
- **迁移只保留历史事实。** 旧数据库以只读模式打开，比较原文/ID/源身份，apply 前
  用 SQLite backup 创建 0600 备份；失败回滚整个目标事务。claims 不认证 GitHub
  login，不覆盖已认证所有者；旧 evidence 保持 historical/stale，不能自动通过验收。
  旧 PR 快照保留实际 head 和状态，不推进 live 验证时钟。同一输入重复迁移不重置
  后续人工变更；兼容 namespace 的 alias 仍受 RFC 权限保护。

## 边界 —— 不属于这里

宿主模型选择、仓库知识查询与调查策略属于 Copilot/skill；提供方身份映射属于 adapter。
本包不要求某一操作系统调度器、SSH 主机或单一 RFC；历史部署迁移是显式 operator
动作，不能隐式接管其他服务的状态与计划。

## 依赖（允许）

应用、状态、提供方、HTTP 与迁移使用 stdlib 和包内模块。CLI 用户动作通过 RFC SDK；
SDK 自身的惰性导入、版本校验和本地/HTTP 选择属于 SDK 契约。可选聊天与翻译适配器
在启用时惰性导入现有 ZCodeTransport，默认应用不加载模型依赖。HTTP 资源通过
`importlib.resources` 从同一 wheel 读取。核心不反向依赖 MCP、模型配置或顶层 CLI。

## 扩展点

新提供方实现统一 contract 并由 operator 注册；新动作进入 `RFCService.dispatch`，
先定义角色、幂等与恢复语义，再由接口薄委托。来源证据与验收定义分开存储。
部署封装前台 `serve` / `sync --watch`，不把主机路径或凭据放进用户协议。

## 测试

`test_rfc_application.py`、`test_rfc_providers.py`、`test_rfc_http.py`、
`test_rfc_integration.py`、`test_rfc_security.py`、`test_rfc_migration.py`、
`test_rfc_chat.py`、`test_rfc_chat_integration.py`、`test_rfc_source_updates.py`。
提供方测试使用注入 transport；多页 GET 预算、恢复不完整、opaque ID、角色区分、
本地路径与原子文件更新均有护栏。迁移测试验证原文、历史身份、重复 apply、备份恢复
和失败回滚；接口测试覆盖失效凭据、ACL、紧凑状态和行尾交换。
RFC portability workflow 声明 Linux/macOS/Windows 与 Python 3.11/3.12 的六组矩阵。

## 重构备注

应用是权限与生命周期的单一实现；新增接口保持薄委托。拆分时不得让 CLI/HTTP/MCP
各自复制状态机、权限或验收计算。运行与 operator 示例见
[RFC 服务使用说明](../../features/rfc-service.md)。
