# API Reference

## 独立项目和任务接口（2026-10-15）

独立项目和多轮任务统一使用 `/api/projects` 与 `/api/project-tasks`。当前客户端没有使用 AgentComputer 专用协议，因此移除 `/api/agentcomputer/v1` 及其 Bridge 转发；暂不提供远程项目和任务访问。旧专家绑定、Run、事件、文档和命令回执协议不恢复，已有 `ac_*` 数据原样保留，不迁移、不删除。

「项目和任务」页面的项目管理使用 `/api/projects`。项目自己保存配置，没有专家或 Agent 外键。创建项目不要求模型服务，不会创建专家。此前的托管专家项目原型不自动转换、不做兼容同步；已有原型数据保留。

| 接口 | 用途 | 当前验证 |
|---|---|---|
| `GET /api/projects` | 获取当前账号的最新项目列表，按最近修改排序 | 服务端自动化通过 |
| `POST /api/projects` | 新建项目，保存名称、简介、服务端/客户端工作目录、Skill/MCP 和行动指南 | 服务端自动化通过 |
| `GET /api/projects/{project_id}` | 获取项目完整配置和 `revision` | 服务端自动化通过 |
| `PATCH /api/projects/{project_id}` | 局部修改；未提交字段保持原值，以 `expected_revision` 防止覆盖修改 | 定向自动化通过 |
| `PUT /api/projects/{project_id}` | 显式完整替换；必须提交全部项目字段，局部请求返回 422 | 定向自动化通过 |
| `DELETE /api/projects/{project_id}` | 按 ID 永久删除项目及其全部任务，无需版本参数；成功返回 204 | 定向自动化覆盖 |
| `PATCH /api/projects/{project_id}/archive` | 以 archived 和 expected_revision 归档或恢复项目，保留全部任务及其归档状态 | 定向自动化覆盖 |
| `GET /api/projects/resources` | 获取可选 Skill、当前用户 MCP 连接和目录浏览范围 | 服务端自动化通过 |

以上接口均需登录，响应 `Cache-Control: no-store`。其他账号的项目返回 404。项目名称在账号内唯一，重名或编辑版本冲突返回 409。新建与修改仅保存配置；发送任务消息时才实际加载项目选定的 Skill/MCP。

请求字段：`name`（必填，最多128字符）、`description`、`directory`（服务端工作目录，在运行 Octop 的主机上验证，可为 null）、`client_directory`（客户端工作目录，用户电脑上的绝对路径，可为 null）、`instructions`、`skills`（多个 `{package_id, slug}`）、`mcp_servers`（多个连接引用）。`PATCH`/`PUT` 另外必填 `expected_revision`，成功后版本加一。`PATCH` 至少包含一个修改字段；省略保持原值，目录传 null 解除关联，Skill/MCP 传 [] 清空，文字字段清空使用空字符串。除两类目录外不接受 null。`PUT` 必须显式包含全部项目字段，不能用仅改名的请求清空其他配置。服务端工作目录遵守当前用户的目录访问范围，不创建、不移动、不删除目录。MCP 引用按账号校验，接口不返回连接密钥。

创建项目必须传 `Idempotency-Key` 请求头（8–128 位字母、数字、下划线或短横线，推荐 UUID）。同一账号、同一标识、相同规范化请求重试，返回第一次创建的完整 201 响应，即使项目之后已被编辑；同一标识换内容返回 `409 PROJECT_REQUEST_CONFLICT`。该记录与项目在同一数据库事务中保存，跨重启保留；创建失败不消耗标识。不同用户的标识互不影响。网页保存失败后保留本次内容对应的标识以便重试。创建或编辑成功后再 GET 获取最新状态。

例如改名仅发送 `PATCH /api/projects/{project_id}`，请求体 `{"name":"新名称","expected_revision":3}`；已有目录、Skill/MCP、指南都保持不变。版本过期返回 `409 PROJECT_REVISION_CONFLICT`，客户端应读取最新详情并让用户处理冲突。

两类目录独立存储。`client_directory` 支持 macOS/Linux 绝对路径、Windows 盘符绝对路径和 UNC 路径，按原字符串保存，不使用服务端的文件系统或用户根目录策略解析该路径；空值可为 null。该字段仅关联位置，不代表目录已授权访问，也不上传或同步目录文件。新建、修改、详情和列表均读写该字段，旧项目新增字段为 null。

项目通过“添加技能”打开与专家复用的技能目录：提供为你推荐、近期飙升、下载热榜、最近上新、搜索、详情和已安装技能选择。榜单复用 `GET /api/skills/hub/rankings?type=all`，搜索复用 `GET /api/skills/hub/search`。SkillHub 选择项以 `{source: "skillhub", slug, name, description}` 保存，已安装项使用 `{package_id, slug}`。两种来源可多选并回显；失败显示重试入口。保存 SkillHub 引用不会下载或执行技能，也不会创建专家。

页面每次进入先展示当前账号在本标签页保存的列表缓存，再通过项目独立同步游标获取变化；恢复前台、恢复网络和手动刷新也会同步。创建或修改成功后先应用写入响应，再同步补齐其他变化，不重复获取完整列表和详情。项目详情默认打开任务会话，可直接输入需求创建任务、在同一任务内追问、打开历史和停止回复。项目配置保留在同名页签中。


项目响应包含 `archived`，新项目默认为 false。项目归档入口为 `PATCH /api/projects/{project_id}/archive`，请求体例如 `{"archived":true,"expected_revision":3}`；恢复时传 false。归档前须停止项目内所有排队或运行中的任务，否则返回 `409 PROJECT_TASKS_BUSY`；归档项目中的任务仍可读取历史，新的任务、追问及重试返回 `409 PROJECT_ARCHIVED`。项目及任务各自的归档状态独立，项目恢复后仍需单独恢复已归档任务才能继续对应会话。归档/恢复与项目版本递增和同步记录原子提交；不删除任务或工作目录文件。SQLite 和 PostgreSQL schema 40 为项目增加归档字段，现有项目默认未归档，不修改旧任务归档状态。

### 任务对话接口（独立项目模型）

Task 是一段持久的多轮对话。发送首条消息时创建 Task；后续消息继续同一个 Task，无需先选择专家。`project_id=null` 代表无项目任务。

新任务先显示首条消息的首个有效行（最多 60 字符），再由服务端内置的 `TaskTitleAgent` 异步概括标题。标题请求沿用首轮模型，只接收最多 4,000 字符的首条消息，不加载工具、Skill、项目行动指南或工作目录；主回答开始输出后才安排标题请求，不阻塞创建和主回答。生成标题通过现有列表同步和会话事件更新，不改变任务活动时间。手动改名后不再自动覆盖，追问不重复命名。新任务首次最多尝试两次，超时或失败保留临时标题，待处理记录支持重启后恢复。标题请求的已报告 Token 用量独立保存并计入所属用户累计配额。

客户端进入任务详情时发送 WebSocket `task.open`，返回最近一页 TaskDetail，并按需补生成默认截取的旧标题；随后发送 `task.subscribe` 观察变化。列表读取、详情 GET、分页、普通订阅和断线恢复不触发标题补生成。打开时只为当前任务排队一次；失败冷却至少 10 分钟，之后再次打开才给予一次新的尝试，累计尝试次数和用量保留。已生成或手动修改的标题不会重命名，不扫描或批量回填未打开的历史任务。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/projects/{project_id}/tasks` | 列出该项目的任务，按最近活动排序 |
| POST | `/api/projects/{project_id}/tasks` | 在项目中发送首条消息，原子创建任务和首轮，返回 202 |
| GET | `/api/project-tasks` | 分页列出无项目任务；archived=true 查看归档任务 |
| GET | `/api/project-tasks/resources` | 本轮可用的模型、Skill 和 MCP 引用 |
| POST | `/api/project-tasks/attachments` | multipart 的 file 上传本人任务输入附件，返回 201 和附件元数据 |
| GET | `/api/project-tasks/attachments/{attachment_id}` | 本人输入附件的鉴权读取，图片预览、其他文件下载 |
| GET | `/api/project-tasks/artifacts` | 跨任务成果资料库，按文件发布时间倒序分页，支持项目、任务、类型、文件名及归档筛选 |
| GET | `/api/project-tasks/artifacts/{artifact_id}` | 本人成果的元数据及当前来源任务和项目信息 |
| WS | `/api/project-tasks/ws` | 多任务共用连接，创建、追问、停止、重试和实时状态；见[客户端接入指南](project-task-websocket.md) |
| GET | `/api/project-tasks/{task_id}/artifacts/{artifact_id}/download` | 本人任务已发布的不可变附件；鉴权下载，支持中文文件名 |
| PATCH | `/api/project-tasks/{task_id}` | 修改名称、网络搜索开关、归档或恢复，校验 expected_revision |
| DELETE | `/api/project-tasks/{task_id}` | 按 ID 永久删除任务及会话、附件和工具记录，无需版本参数；成功返回 204 |
| POST | `/api/project-tasks` | 创建任务并发送首条消息，`project_id` 可省略或为 null |
| GET | `/api/project-tasks/{task_id}` | 分页读取会话轮次，默认最近 50 轮，最多 100 轮 |
| GET | `/api/project-tasks/{task_id}/events` | 订阅实时轮次状态，支持游标或 Last-Event-ID 续接 |
| POST | `/api/project-tasks/{task_id}/messages` | 发送下一轮消息，继续相同任务 |
| POST | `/api/project-tasks/{task_id}/cancel` | 指定 turn_id 停止对应回复，保留已有正文 |
| POST | `/api/project-tasks/{task_id}/turns/{turn_id}/retry` | 重新尝试最后一个失败或取消轮次，创建新轮次 |

发送体：`{"request_id":"每次发送唯一的UUID","prompt":"用户消息","attachment_ids":[]}`。通用创建接口另接受 `project_id`。prompt 可省略或为空，但文字和附件至少提供一种；attachment_ids 可省略，图片和其他文件合计每轮最多九个且不能重复。网络重试必须复用 `request_id`、消息、附件 ID 顺序和原 options；标识与消息、附件、执行选项或重试来源冲突返回 409。执行中发送新消息返回 `TASK_BUSY`，停止或结束后可继续。所有读写均按当前登录用户隔离；其他用户的资源返回 404。读取带 `Cache-Control: no-store`。

每轮采用发送时项目配置的快照：行动指南进入系统提示；服务端目录作为实际工作目录；所选已安装技能和 SkillHub 包通过 Harness 工作区加载；仅装配所选 MCP 和插件能力，凭据沿用连接管理。子代理角色、长期记忆和团队协作配置同样按轮保存。项目修改在下一轮生效，已保存轮次的 `project_revision` 保留原值。客户端目录仍然只记录路径。无项目任务使用服务端自动分配的独立工作区。

未显式选择本轮模型时，使用全局已配置的可用对话模型（优先启用的全局默认模型，未设时采用已有文本执行模型配置，最后回退到第一个可用模型）。未配置模型不创建空任务。有当前或历史图片输入时，所选模型须支持图片，不能用纯文字模型继续，也不会自动切换模型。执行遵循现有全局安全策略和 Token 配额；模型用量直接记在任务轮次的 `input_tokens/output_tokens` 中，并纳入用户累计配额；原专家 Token 统计页面暂未合并这部分展示。主 Task 需要审批的工具保存交互及原生断点，收到决定后续跑；任务提供文字、图片和通用文件对话、项目文件/脚本、Skill、所选 MCP 和插件、子代理、任务独立记忆及同步团队协作；浏览器和桌面未接入。

状态为 `queued/running/waiting_approval/waiting_answer/waiting_client_tool/paused/completed/failed/cancelled`。运行时约每 0.5 秒保存增量正文；网页通过 WebSocket 接收已保存的轮次状态，约每 30 秒同步任务列表摘要。会话断线后从最后应用的游标续接，恢复前台或网络时重新连接。已完成轮次以及失败/取消轮次的模型消息、工具调用和结果用作后续上下文；历史仅有问答文本的记录继续保留文本；未完成轮次附加明确标记，提醒模型核对已发生的操作，不自动重放工具调用。取消不会回滚已发生的工具副作用；优雅停服标记中断，进程异常退出后约 90 秒释放失效执行，不自动重试。回复失败不向客户端返回原始模型/MCP 异常或凭据。

主 Task 已接入原生暂停恢复：审批、提问和客户端工具等待时保存检查点并释放执行槽，全部结果到齐后继续同一轮，Worker 重建不重复派发已登记调用。协议版本为 3，合法等待不按运行心跳过期处理；手动暂停在安全边界生效，继续不替代审批。嵌套人工中断恢复尚未支持。

新增 `POST /api/project-tasks/{task_id}/pause`、`/resume`，body 为 `{turn_id,request_id}`；回应入口为 `POST /api/project-tasks/{task_id}/turns/{turn_id}/interactions/{interaction_id}/respond`。等价 WebSocket 命令、数据结构和幂等规则见[交互协议](project-task-interactions.md)，客户端后续接线见 [AgentComputer 交接文档](agentcomputer-task-interactions.md)。本机工具保留领取、本机授权、执行和结果链路；远端审查回执只提供建议，不替代本机许可。

验证：任务 HTTP/持久化/多轮/幂等/用户隔离/取消/崩溃恢复集成用例通过；真实 Harness 配合本地模拟模型完成项目文件读取及模拟 MCP 调用，并验证 Skill/行动指南进入提示。8088 已登录正式页面使用现有 GLM-5 完成项目内首轮、同 Task 追问及刷新后历史恢复；无项目任务同样返回真实回复。真实 MCP 供应商连接、PostgreSQL 与 Ubuntu 盒子尚未实机点验。

### 任务审批模式与远端 Reviewer

TaskSummary/TaskDetail 返回当前账号全部任务共用的 `approval_policy={mode,revision,updated_at}`，轮次保留 `approval_policy_at_start`。`GET /api/project-tasks/approval-policy` 读取、`PUT /api/project-tasks/approval-policy` 提交 `{request_id,expected_revision,mode}` 并返回策略对象，无任务时也可用。原 `PUT /api/project-tasks/{task_id}/approval-policy` 和 `task.approval.set` 验证任务归属后也修改全局模式，返回 TaskDetail。全部项目、已有/运行/新任务统一生效，三种模式始终可选，新账号默认询问。创建顶层旧 `approval_mode` 已忽略，不可覆盖或重置全局。版本和幂等键为账号范围；网络重试复用原请求。新请求版本冲突返回 `TASK_APPROVAL_POLICY_CONFLICT`。切换不放行已有人工卡，不改动作/轮次旧快照、强制人工规则或本机资源授权。WebSocket 独立推送 `approval.policy` 事件，首次轮询及全局变化发送，不要求 task.subscribe。

`GET /api/project-tasks/resources` 增加 `approval={allowed_modes,reviewer_ready,execution_profile:"host_process"}`，`allowed_modes` 固定返回三种模式。`reviewer_ready` 仅供状态展示，不限制选择帮我审批；执行时 Reviewer 不可用则转人工。管理员通过 `GET/PUT /api/admin/security/task-approvals` 仅保存 `{require_human_tools}`，无需配置可用模式。网页在选择完全访问前说明服务端命令使用 Octop 主机进程权限；本机权限仍由执行电脑授予。

首版三档模式覆盖服务端 `execute` 与客户端 `exec_command`；其他需审批的工具保持人工，强制人工列表始终生效。自动审批使用 Octop 上独立且无工具的 Reviewer，沿用该轮已保存模型，只接收固定动作和本轮目标。45 秒超时、格式非法、不可用或配额不足均转人工。审查不占主任务执行槽，用量独立保存并计入用户累计配额。重启后不重复请求状态不明的已派发审查；取消后迟到结果只登记用量。

客户端新增领取凭证绑定的 `tool.review`，结果位于原 `ClientToolCall.review`，不会完成原调用或自动恢复模型。执行电脑须保留本机授权，核对动作和上下文摘要，并在派发前通过 `tool.get` 检查 `dispatch_allowed`、`manual_review_required`、暂停/取消和截止时间。服务端不探测客户端路径。完整字段见[任务协议](project-task-websocket.md#142-客户端远端审查-toolreview)及[客户端交接](agentcomputer-task-interactions.md)。本次未修改 AgentComputer。

成对的 SQLite/PostgreSQL schema 41 包含断点、交互、任务模式与审查记录；已记录 41 的本地库也幂等补齐缺失列和表。已有任务默认人工审批，已有数据保留。本轮使用 SQLite 与模拟模型验证，未发起真实供应商审查或进行 PostgreSQL 实机验收。

### 任务成果资料库

`GET /api/project-tasks/artifacts` 直接查询当前账号所有仍存在任务正式发布的成果，包含项目任务和无项目任务，不需要逐任务读取会话。固定按文件发布的 `created_at DESC, artifact_id DESC` 排序，与任务最近活动时间无关。返回 `items` 和 `next_page_cursor`，默认 50 条、最多 100 条；使用原筛选条件和 `page_cursor` 继续读取，末页游标为 null。

`scope` 默认 `all`，可选 `standalone` 或 `project`；`scope=project` 必须携带 `project_id`，其他 scope 不接受该参数。可使用 `task_id` 进一步限定，`kind` 筛选文件类型，`q` 搜索文件名的字面子串（ASCII 字母忽略大小写，去首尾空白，最多 200 字符）。`archived=all|active|archived` 默认 all，active 要求任务和所属项目均未归档，archived 匹配任一已归档。指定他人或不存在的项目/任务返回 404，合法但交集为空的筛选返回空列表。

条目复用 `TaskArtifact` 的公开字段，补充 `file_kind`、`task_title`、`task_archived`、`turn_status` 及可空的 `project_id/project_name/project_archived`。`GET /api/project-tasks/artifacts/{artifact_id}` 返回相同结构的单份元数据；下载继续使用原 `download_url`。列表和详情不加载文件字节、不依赖模型；归档后仍可读取，发布后轮次失败或取消不移除文件。永久删除任务或项目会删除对应成果。输入附件与未发布工作文件不收录。

资料库统一使用六个筛选标签：全部、文本和PDF、幻灯片、表格、图片、其他。列表、详情、任务历史和发布结果的 `business_category` 必返回 `document`（文本和PDF）、`presentation`（幻灯片）、`spreadsheet`（表格）、`image`（图片）或 `other`（其他）。查询参数 `business_category` 接受这五个值及默认值 `all`（全部）。资料库列表和详情继续返回原格式字段 `file_kind`，原 `kind` 筛选保持可用。例如 `GET /api/project-tasks/artifacts?business_category=document&kind=text&q=季度` 按资料分类、详细格式与文件名求交后分页，来源和归档筛选同样参与交集。

主分类由服务端按最终发布文件名后缀自动保存，忽略 ASCII 大小写，不使用 MIME 补充：文档、纯文本和代码配置文件归入文本和PDF；演示文稿、表格及图片各自归类；压缩包、音视频、未知后缀和无后缀文件归入其他。`create_artifact`、`publish_workspace_file`、`publish_workspace_archive` 不再接受 `business_category` 参数；重命名发布时以最终 `name` 分类，ZIP 按 ZIP 本身归入其他。schema 49 将所有已有成果按同一后缀规则回填，保留原内容、ID、发布时间及发布调用摘要。`all` 仅用于查询，不是存储或返回值；旧报告、数据、流程和未分类枚举不再接受。

分页游标使用 `artifact-library-v3` 用途并绑定账号、实例及全部筛选（包括 `business_category`），改变筛选、篡改或复用其他用途游标返回 `409 PROJECT_SYNC_RESET`；v1/v2 的旧游标同样重置，客户端重新读取首页。查询不是跨请求快照，新增成果通过刷新首页获取。卡片摘要继续由客户端下载小型文本后提取，服务端摘要字段后续补充；PostgreSQL 实库及查询计划仍待验证。完整参数、分类、权限和生命周期见[资料库技术说明](project-task-artifact-library.md)。

### 项目同步和任务列表分页

列表同步使用 `GET /api/projects/sync`、`GET /api/projects/{project_id}/tasks/sync`、`GET /api/project-tasks/sync`。项目和各任务列表分别维护游标，不能跨账号、列表、实例或用途复用。任务列表返回分页对象；本轮不修改原生客户端 UI。

项目首次不带 `sync_cursor`，返回 `mode=snapshot`、完整项目 `changes` 和 `next_sync_cursor`；之后带上游标只返回期间变化过的项目最终状态。任务首次只获取一页摘要，默认 50 条、最多 100 条；返回 `items`、`next_page_cursor` 和 `sync_cursor`，不预读全部历史或正文。后续以 `page_cursor` 加载更早任务，按 `updated_at DESC, task_id DESC` 续取；历史下一页不产生新同步基线。

任务首页返回的 `sync_cursor` 可立即请求对应 `/sync`，不必等历史读完。即使未加载的旧任务发生新活动，也通过增量进入近期列表。增量返回 `base_sync_cursor`、`changes`、`next_page_cursor`、`next_sync_cursor`；同一对象多次修改合并为截止版本的完整状态，中间页不返回可提交的新同步位置。收齐本批后再一起保存对象和游标。历史分页游标和增量分页游标用途不同，客户端分开保存。

分页与增量响应交错时，使用 `list_revision` 保留较新的任务摘要；项目使用 `revision`。自己写入成功也不能跳过期间其他变化，直接推进同步位置。无效游标返回 HTTP 409 `PROJECT_SYNC_RESET`，重新建立项目快照或对应任务第一页即可。后台任务完成、取消和中断恢复的摘要更新与变更记录同事务提交；回复正文和心跳不产生列表变更。删除项目或任务时，同一事务写入 `operation=delete`、`data=null` 的同步记录；任务删除记录携带递增的 `list_revision`。客户端移除对应项并保存删除标记，防止旧分页或迟到的会话事件重新加入任务。仍不提供自动历史清理，备份恢复流程仍需可靠推进 `restoreEpoch` 后才能使旧游标失效。

删除接口按 ID 操作，不要求 `expected_revision`，不因标题、状态或配置版本变化而拒绝删除；编辑和归档仍保留各自的版本检查。排队或运行中的任务须等待完成或先停止，单任务删除返回 `409 TASK_BUSY`；项目中存在此类任务则返回 `409 PROJECT_TASKS_BUSY`，整个项目及其任务均保持原样。错误响应中的本地化消息说明禁止操作的原因及处理方式，网页在删除确认框中显示具体错误；任务执行期间同时直接显示无法删除的原因。删除项目会原子清理全部任务及会话、附件、工具记录和上下文；其他项目和无项目任务不受影响。删除操作保留工作目录中的文件，包括自动分配的任务工作区，不回滚已执行的工具操作。已删除资源再次读取返回 404；项目名称和创建请求标识随项目删除而释放。

客户端删除请求、错误响应和提示要求见[项目和任务删除接口接入说明](project-task-deletion.md)。网页在 `sessionStorage` 按服务地址、账号、列表分别保存摘要缓存和游标，不保存任务对话正文。完整字段与示例见 [增量同步设计](project-task-incremental-sync.md)。

项目、任务和轮次 ID 都是不透明字符串，例如 `proj_…`、`task_…`、`turn_…`；调用方应原样保存和传回，不能转成 UUID。时间字段为 UTC ISO 8601，界面使用 `GET /api/settings/timezone` 返回的服务端时区。

### 项目字段和编辑规则

| 字段 | 类型与限制 | 创建默认值 / 编辑语义 |
| --- | --- | --- |
| `name` | 字符串，去首尾空白后 1–128 字符，账号内唯一 | 创建必填；PATCH 省略不变，不接受 null 或空字符串 |
| `description` | 字符串，最多 1024 字符 | 默认 `""`；清空传 `""` |
| `directory` | 服务端已有绝对目录路径，最多 4096 字符 | 默认 null；PATCH 省略不变，null 解除关联；遵守服务端目录权限 |
| `client_directory` | 客户端绝对路径，最多 4096 字符 | 默认 null；PATCH 省略不变，null 解除关联；只保存路径，不授予本地文件访问权限 |
| `instructions` | 字符串，最多 32768 字符 | 默认 `""`；清空传 `""` |
| `skills` | 最多 64 个已安装 Skill 或 SkillHub 引用，不可重复 | 默认 `[]`；PATCH 省略不变，`[]` 清空，不接受 null |
| `mcp_servers` | 最多 64 个当前账号可用的连接引用，不可重复 | 默认 `[]`；PATCH 省略不变，`[]` 清空，不接受 null；不提交连接密钥 |
| `plugins` | 最多 64 个已安装且全局启用的插件 ID，不可重复 | 默认 `[]`；PATCH 省略不变，`[]` 清空；加载所选插件的工具、中间件和技能 |
| `subagents` | 最多 16 个内置子代理角色 slug，不可重复 | 默认 `[]`；可通过 task 工具委派，不创建或绑定专家 |
| `memory_enabled` | 严格布尔值 | 默认 true；每个任务持久保存自己的记忆，不跨任务共享 |
| `team_enabled` | 严格布尔值 | 默认 false；将所选角色同时作为任务内成员，通过 agent_list、ask_agent 同步协作 |
| `expected_revision` | 大于等于 1 的严格整数 | 创建不传；PATCH/PUT 必填，成功后服务端 revision 加一 |

项目返回对象另外包含 `project_id`、`revision`、`created_at`、`updated_at`。请求不接受 `expert_id`、`agent_id` 等未声明字段。Skill/MCP 选择项来自资源接口和现有 SkillHub 目录；不能把仅在客户端安装的 Skill 路径直接当成服务端引用。

### 项目请求示例

以下 ID、路径、时间均为示意，不是本机已有资源；示例请求没有自动执行。

新建项目：`POST /api/projects`，请求头为：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
Idempotency-Key: 2b30948c-0b02-42a1-b2ed-386f04cd758a
```

```json
{
  "name": "产品研发",
  "description": "项目需求和研发协作",
  "directory": null,
  "client_directory": "/Users/alice/Projects/product",
  "instructions": "修改前先阅读项目说明。",
  "skills": [],
  "mcp_servers": []
}
```

成功返回 HTTP 201，完整对象如下。同一账号用同一 key 和相同内容重试，返回这个首次创建结果；如果项目后来被编辑，重试结果仍是最初版本，需要 GET 获取最新配置。

```json
{
  "project_id": "proj_example",
  "name": "产品研发",
  "description": "项目需求和研发协作",
  "directory": null,
  "client_directory": "/Users/alice/Projects/product",
  "instructions": "修改前先阅读项目说明。",
  "skills": [],
  "mcp_servers": [],
  "revision": 1,
  "created_at": "2026-10-15T05:00:00Z",
  "updated_at": "2026-10-15T05:00:00Z"
}
```

仅改名称：`PATCH /api/projects/proj_example`，无需创建幂等请求头：

```json
{
  "name": "产品研发二期",
  "expected_revision": 1
}
```

返回 HTTP 200 和完整项目，`revision=2`；目录、简介、Skill/MCP 和行动指南保持原值。解除客户端目录关联，同时清空已选 Skill，可继续发送：

```json
{
  "client_directory": null,
  "skills": [],
  "expected_revision": 2
}
```

该请求不清空服务端目录或 MCP。`PUT` 只用于完整替换，必须显式提交全部七个项目字段及 `expected_revision`；只有名称和版本的 PUT 返回 422。普通编辑统一使用 PATCH。

### 任务请求和返回示例

在项目中发送第一条消息，推荐使用通用入口 `POST /api/project-tasks`：

```json
{
  "project_id": "proj_example",
  "request_id": "9fca322b-4884-4acd-bdfd-67428aeac735",
  "prompt": "请先概括项目目标，再列出下一步。",
  "web_search_enabled": true
}
```

创建无项目任务时，仍使用上述入口，把 `project_id` 传 null 或省略。也可使用 `POST /api/projects/{project_id}/tasks`，此时不再重复传 `project_id`，仍可设置 `web_search_enabled` 和 `options`；审批模式读取账号全局设置。

这两个创建入口都返回 HTTP 202 和 `TaskDetail`，表示任务及首轮已保存并排队，不表示模型已执行完成。执行不要求预先创建专家或单独创建会话。

继续同一任务使用 `POST /api/project-tasks/task_example/messages`：

```json
{
  "request_id": "a803c45d-c397-4a3f-b1a7-91bd8ca642a2",
  "prompt": "继续细化第二步。"
}
```

消息支持文字、图片和通用文件，可混合发送：`prompt` 去首尾空白后最多 32768 字符；文字为空时必须提供 attachment_ids。`request_id` 为 8–128 位字母、数字、短横线或下划线。附件先通过独立上传接口取得 ID，图片和其他文件合计每轮最多九个。当前请求接受 options 中的本轮模型及 Skill/MCP 选择；创建任务可在顶层设置 web_search_enabled；旧 approval_mode 已忽略，继承账号全局模式。未覆盖的资源继承发送时的项目配置。

通过 `GET /api/project-tasks/task_example` 读取 `TaskDetail`，默认最近 50 轮。`limit` 为 1–100；使用 `next_page_cursor` 作为下一次的 `page_cursor` 读取更早轮次，每页内部按创建顺序正序排列。首屏与实时基线来自同一数据库快照，历史页的 `event_cursor=null`，不能用历史页推进实时位置。以下是单轮已完成时的示意，创建响应的轮次通常处于 queued：

```json
{
  "task_id": "task_example",
  "project_id": "proj_example",
  "title": "请先概括项目目标，再列出下一步。",
  "archived": false,
  "web_search_enabled": true,
  "status": "completed",
  "active_turn_id": null,
  "list_revision": 3,
  "created_at": "2026-10-15T05:01:00Z",
  "updated_at": "2026-10-15T05:01:05Z",
  "next_page_cursor": null,
  "event_cursor": "opaque_event_cursor",
  "turns": [
    {
      "sequence": 1,
      "turn_id": "turn_example",
      "request_id": "9fca322b-4884-4acd-bdfd-67428aeac735",
      "prompt": "请先概括项目目标，再列出下一步。",
      "reply": "项目目标是……下一步是……",
      "status": "completed",
      "error_code": "",
      "activity": "",
      "model_ref": "provider/model",
      "web_search_enabled": true,
      "options": {"model_ref": "provider/model", "skills": [], "mcp_servers": []},
      "interactions": [],
      "interaction_revision": 0,
      "pause_requested": false,
      "cancel_requested": false,
      "retry_of_turn_id": null,
      "input_tokens": 120,
      "output_tokens": 80,
      "project_revision": 1,
      "created_at": "2026-10-15T05:01:00Z",
      "updated_at": "2026-10-15T05:01:05Z"
    }
  ]
}
```

任务列表返回 `{items: TaskSummary[], next_page_cursor: string|null, sync_cursor: string|null}`；摘要为上例移除 `turns`、`next_page_cursor` 和 `event_cursor` 后的字段，包含任务级 `status` 和单调递增的 `list_revision`，详情和写入响应也携带这两个字段。`status` 表示当前或最近一轮的状态，取值与下表一致；首条消息或追问进入 `queued`，开始执行变为 `running`，结束后更新为对应结果。服务端持久保存摘要，每次状态变化与版本递增、列表增量记录原子提交。正文、工具活动、用量和租约心跳不递增列表版本。

每页直接批量返回所有任务状态，客户端用列表缓存绘制侧栏，通过现有 `/tasks/sync` 或 `/project-tasks/sync` 获取变化；加载一页只需一次列表请求，禁止逐个读取详情补状态。打开任务才读取正文详情。仅第一页返回同步基线，后续历史分页不推进同步位置。

| 轮次状态 | 含义 | 当前客户端处理 |
| --- | --- | --- |
| `queued` | 已保存，等待执行 | 显示排队，继续订阅状态 |
| `running` | 正在生成回复或调用工具 | 使用已保存的 reply 和 activity 展示进度 |
| `waiting_approval` | 等待服务端审批 | 显示全部 pending approval |
| `waiting_answer` | 等待问题回应 | 显示全部 pending question |
| `waiting_client_tool` | 等待客户端工具结果 | 执行电脑保留本机 journal 与审批 |
| `paused` | 用户暂停已到达安全边界 | 可继续或停止，仍保留活动轮次 |
| `completed` | 本轮完成 | 可以继续发送下一条消息，保留任务状态订阅 |
| `failed` | 本轮失败或中断 | 保留已存正文，读取 error_code，不自动重发工具操作 |
| `cancelled` | 本轮已取消 | 保留历史，可以继续发送消息 |

`active_turn_id=null` 表示当前没有活动轮次，不等于该任务永久结束。Task 仍是一段可以继续的会话。`activity` 只是当前工具名，结束后会清空，不能用它生成完整工具历史。

停止使用 `POST /api/project-tasks/{task_id}/cancel`，必须提交 `{"turn_id":"turn_example"}`。返回 HTTP 200 和任务详情；运行轮次先置 `cancel_requested=true`，继续订阅直到进入终态。重复或迟到的请求只作用于指定轮次，该轮已结束时不影响下一轮；缺少请求体返回 422，其他任务的轮次返回 404。

重新尝试使用 `POST /api/project-tasks/{task_id}/turns/{turn_id}/retry`，提交 `{"request_id":"new-retry-request"}`。只允许最后一个失败或取消轮次，活动任务返回 `TASK_BUSY`，不符合条件返回 `TASK_RETRY_UNAVAILABLE`。新轮次沿用原始消息，采用当前项目配置和模型，`retry_of_turn_id` 指向原轮次；原正文和状态不覆盖。同一重试标识重传不会重复执行。重试可能再次执行之前发生过的操作，网页在按钮旁明确提示；不自动重试失败轮次。

### 任务名称、归档与恢复

`PATCH /api/project-tasks/{task_id}` 接受 `expected_revision`（当前 list_revision）以及可选的 title、archived、web_search_enabled，至少提供一个修改字段；省略保持原值，不接受 null。名称去首尾空白后为 1–128 字符。版本冲突返回 409 `TASK_REVISION_CONFLICT`，应刷新后重新编辑，不能直接覆盖。相同字段值不递增版本。

例如 `{"expected_revision":2,"title":"需求整理","archived":true}` 会修改名称并归档。归档保存全部会话和执行记录，没有删除业务数据；`archived:false` 恢复任务。任务排队或运行时归档返回 `TASK_BUSY`，须先结束或停止；归档状态不自动取消执行。归档任务可以读取历史，但新消息和新重试返回 `TASK_ARCHIVED`，需恢复后继续。已成功发送的 request_id 幂等重传仍返回原任务。

项目内与无项目的任务列表都接受 `archived=false/true`，默认只显示未归档任务，归档任务仍按游标分页读取。两种视图的历史及同步游标不能混用。增量会返回变过任务的完整摘要，包含 archived；任务归档或恢复时，旧视图据此移出任务，新视图据此加入任务。保留较高 list_revision 的摘要，包括不属于当前显示筛选的摘要，防止迟到历史页重新加入旧状态。改名、归档、恢复与列表变更在同一事务提交，并可通过当前会话的实时连接获取最新摘要。

### 任务网络搜索

网络搜索是随任务持久保存的布尔设置 `web_search_enabled`，省略时默认开启；用户显式设置 false 才关闭。创建时在顶层传入；之后使用 `PATCH /api/project-tasks/{task_id}`，例如 `{"expected_revision":3,"web_search_enabled":false}`。任务摘要、详情和列表同步均返回当前值，刷新、重启和后续追问会保留，已保存的关闭状态不会被默认值覆盖。不写入项目配置，也不在每轮 options 中重复提交。

每轮入队时固定该任务当时的值，`turns[].web_search_enabled` 返回本轮快照。切换可在任务运行中保存，只影响之后入队的追问或手动重试；已入队、运行或暂停后继续的轮次维持原值。开启后装载已配置的搜索服务及免配置搜索工具，关闭后不装载内置搜索工具。此开关不控制网页读取、所选 MCP 或命令的网络访问。

### 本轮执行选项

创建、追问和手动重试都接受 options，不写回项目或全局模型配置。例如：

```json
{
  "request_id": "chosen-turn-request",
  "prompt": "请先检查代码，不调用外部连接。",
  "options": {
    "model_ref": "provider/model",
    "skills": [{"package_id":"pkg_example","slug":"review"}],
    "mcp_servers": []
  }
}
```

model_ref 省略或 null 使用发送时的默认选择；显式指定不可用模型返回 `TASK_MODEL_UNAVAILABLE`，不会静默改用其他模型。skills、mcp_servers 省略或 null 继承项目配置；[] 仅在本轮停用该类资源，非空列表完整替换该类选择。无项目任务同样可以选择本人有权使用的资源。重复引用、未知字段或无权访问的资源被拒绝，不创建空轮次。

options 也接受 plugins、subagents、memory_enabled、team_enabled。省略或 null 继承发送时的项目值；无项目任务默认 plugins/subagents=[]、memory_enabled=true、team_enabled=false。列表 [] 清空本轮选择，布尔值 false 仅关闭本轮能力。资源接口增加 plugins（plugin_id/name/description/available）和 subagents（slug/name/description）；全局禁用或未成功加载的插件不可选择。turns[].options 返回全部实际选择。保存项目只保存引用，不安装插件、执行工具或创建专家。

服务端按任务保存完整模型消息及工具调用/结果，追问在新执行环境中恢复这些消息；常规工具参数和结果不加入公开历史响应；审批交互会向本人展示该提案的固定参数。停止或失败时，将没有结果的工具调用补成中断结果供下轮理解，不自动重放。原有只含问答文本的记录保持原样。长期记忆的原始对话捕获与搜索保存在任务独立存储中，切换项目服务端目录不影响记忆。团队只包含当前任务选择的角色，成员不进入专家列表；成员结果供主任务汇总，模型用量计入当前轮次。目前支持同步成员调用，不支持后台成员派工或嵌套人工中断恢复。

options 还接受 reasoning_mode（auto/enabled/disabled）和 reasoning_effort。省略或 null 使用所选模型的默认值，不继承前轮；资源接口 models[].reasoning_config 返回模型支持的模式、强度和默认值，不支持的设置返回 422 TASK_REASONING_INVALID。只有始终推理能力的模型不提供关闭或强度选择。轮次 options 返回实际采用的设置，供应商请求参数随轮次快照固定。

轮次 reasoning 单独保存模型返回的可展示思考文本，不混入 reply。它通过现有实时事件累计更新，刷新、历史分页和重连均可恢复，停止或失败也保留已收到的部分；为空时页面不显示思考区块。前端分别合并两类文本，不让迟到短文本覆盖较长内容。思考显示为可折叠区块。子代理与团队成员的思考不混入主任务，不把思考文本加入长期记忆；不根据正式回复生成思考内容。APP 字段路径、完整推送与 Swift 读取示例见[APP 思考内容接入](project-task-websocket.md#app-思考内容接入)。

未发布数据库 schema 39 增加项目能力配置、任务思考文本和服务端私有上下文存储，SQLite/PostgreSQL 成对提供；保留已有项目、任务及运行数据。已记录 39 的本地库也会幂等补齐思考列，已有轮次默认空字符串，不补造历史思考内容。

`GET /api/project-tasks/resources` 返回可用 models（model_ref/name）、default_model_ref，以及共享 Skill 与本人 MCP 引用，不包含凭据。网页“本轮设置”在成功发送后恢复继承状态；网络失败保留选择和发送标识。同一 request_id 必须重传相同的原选项，项目后来修改不会影响幂等回放。手动重试采用该次请求的 options，未指定时使用当前项目配置，不隐式复制失败轮次的覆盖项。返回的 turns[].options 是该轮实际采用的模型、Skill 和 MCP，project_revision 仍记录发送时项目版本。

未发布的数据库 schema 34 包含分页索引和任务状态摘要，状态从活动轮次或最近一轮初始化；本地已记录 schema 34 但缺少状态列的库也会补齐。归档标记及任务、轮次和正文保留。任务列表游标及网页任务缓存版本随状态字段更新，旧游标返回 `PROJECT_SYNC_RESET`，客户端重建第一页；实际部署库升级、PostgreSQL 和真实供应商联调仍需另外验收。

### 任务输入附件

独立上传、发送、历史展示与多轮文件处理的接入步骤见[任务通用附件接入说明](project-task-input-attachments.md)。网页支持执行中将后续消息加入本地先进先出队列，当前轮结束后逐条提交；队列不改变接口的 `TASK_BUSY` 约束，具体生命周期和重试规则见[任务消息队列使用和接入说明](project-task-message-queue.md)。

先使用 `POST /api/project-tasks/attachments`，以 multipart `file` 提交文件并取得 `{attachment_id,name,media_type,byte_count,sha256,preview_url,workspace_path,created_at}`，再在 REST 创建、继续消息或 WebSocket `task.create/task.message` 的 `attachment_ids` 引用。支持文档、表格、PDF、文本、代码、压缩包等通用文件；每条消息图片和其他文件合计最多九个，每个上限为服务端上传设置与 25 MiB 的较小值，单文件大小超限返回 `413 TASK_ATTACHMENT_LIMIT`。消息附件超过九个、ID 为空或重复时，REST 返回 `422` 请求校验响应，WebSocket 返回 `422 TASK_FRAME_INVALID`。

真实 PNG、JPEG、GIF、WebP 作为视觉输入，`workspace_path` 为 null；伪造或损坏图片返回 `422 TASK_IMAGE_INVALID`，无法压到模型可接收大小返回 `413 TASK_IMAGE_LIMIT`。SVG、BMP 等其他格式作为普通文件。普通文件不要求视觉模型；本轮或历史含视觉图片时，非视觉模型返回 `422 TASK_IMAGE_MODEL_REQUIRED`。

上传文件先归当前用户暂存，未发送的附件 24 小时后不可用；首次发送成功与绑定任务在同一事务完成。同一任务可再次引用，手动重试沿用原附件。跨任务、跨账号、过期或不存在的 ID 返回 `404 TASK_ATTACHMENT_UNAVAILABLE`。TaskTurn 的 `attachments` 保存输入元数据，任务输出仍位于 `artifacts`；纯附件任务使用本地化附件任务标题。

普通文件执行前放入服务端工作区的 `workspace_path`，供文件工具、脚本和 Skill 使用；不会自动执行、解压或保证所有格式自动解析。后续轮次保留工作副本的编辑，项目目录变更后恢复缺失文件；每轮提供任务文件清单，支持长对话压缩后重新定位。原始内容随任务保存，与项目目录独立，`client_directory` 不被访问。

使用 `GET preview_url` 并附带 Bearer token 读取原始内容。视觉图片内联预览，其他文件作为附件下载；响应包含 `Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff` 和受限 CSP。刷新、分页、实时事件和断线恢复均不内嵌文件字节。图片持久化 `task-image://` 引用，模型调用前临时展开；`read_task_image` 仅列出、读取当前任务视觉图片。完整接入见[任务 WebSocket 指南第 15 节](project-task-websocket.md#15-输入附件)。

### 任务上下文压缩

任务支持自动压缩及无附件的 `/compact` 消息命令；REST 仍使用 `POST /api/project-tasks/{task_id}/messages`，WebSocket 仍使用 `task.message`。摘要和近期消息跨轮、跨服务重启保留，公开聊天记录不删除。连续压缩的原文追加到任务独立归档，项目服务端工作目录变更后仍能读取。详情页提供“压缩记忆”按钮，保留未发送草稿和附件。维护轮只调用摘要模型，不执行任务工具或写入长期记忆；沿用幂等、权限、取消和显式恢复，不支持暂停。完整语义和任务与普通会话的能力差异见[任务 WebSocket 指南](project-task-websocket.md#任务上下文压缩)。

### 任务上下文用量与知识库

`GET /api/project-tasks/{task_id}/context-usage` 提供最近一次主模型请求的窗口、占用与组成估算。缺少快照返回 `source=empty`，占用为估算时标记 `estimated=true`；运行中、暂停及重启后可读，压缩后立即更新。每轮 `input_tokens/output_tokens` 继续表示累计计费用量，页面与上下文占用分开展示；已加载页汇总不冒充整个任务总量。

`options.knowledge_base_ids` 用于本轮知识库选择，省略/null 取本人默认开启库，[] 禁用，共享库须显式选择。入队冻结合法 ID 和目录，检索时再次校验当前可见性；不修改项目或专家。内置 `search_knowledge` 与真实结果的结构化引用支持历史重开，来源通过现有知识库鉴权接口预览或下载。字段、权限和响应语义见[任务接入说明](project-task-websocket.md#上下文占用与计费用量)。

### 服务端附件交付

任务运行时提供 `create_artifact(name,content,media_type?)`、`publish_workspace_file(path,name?)` 和 `publish_workspace_archive(paths,name)`，均在服务端执行，客户端不能注册。后两者只读取本轮服务端工作目录，不读取 `client_directory`；ZIP 工具只打包显式列出的相对文件，不扫描整个目录。有无项目都可创建附件；只有显式工具调用成功才算正式交付。没有配置服务端或客户端项目目录的独立工作区中，本轮成功写入普通成果文件后，最后完成步骤和无计划最终答复会检查本轮确有附件；缺少时要求模型先交付。已有项目目录的日常编辑不强制打包，通过命令自行生成文件仍依赖模型遵循交付指引。每轮 `artifacts` 返回稳定 ID、名称、MIME 类型、大小、SHA-256、可选来源相对路径和鉴权下载地址；REST 历史和 WebSocket 使用同一结构，重连可恢复。源文件更改/删除不影响已发布快照。单文件最多 25 MiB，每轮最多 5 份、合计 50 MiB。详细字段、错误和客户端适配见[任务 WebSocket 指南第 13 节](project-task-websocket.md#13-服务端附件交付)。

### 任务 WebSocket：命令与实时状态

客户端开发以独立的[任务 WebSocket 接入指南](project-task-websocket.md)为准，包含完整命令、事件、字段、Swift 连接示例、恢复和错误处理。机器可读结构见 [JSON Schema](project-task-websocket.schema.json)。

正式任务页面使用 `WS /api/project-tasks/ws`（HTTPS 部署使用 WSS）。同一页面和账号共用一条连接，可同时订阅多个任务；切换任务取消旧订阅，不停止任务。项目配置、任务列表、历史分页、改名和删除继续使用 REST。AgentComputer 原生客户端本轮未修改，仍需按本节接入。

浏览器建立连接后须在 10 秒内发送首帧：

```json
{"type":"authenticate","token":"<access token>","locale":"zh"}
```

原生客户端也可在升级请求中携带 `Authorization: Bearer <access token>`，无需 authenticate 帧。两种方式都不把 token 放进 URL。认证成功返回 `{"type":"ready","protocol_version":3}`。连接绑定当前账号；每条命令和约每 10 秒重新检查登录态，滑动续期通过 `{"type":"session","access_token":"<renewed token>"}` 返回，客户端应按原来的记住登录偏好保存。认证失败返回错误后以 4401 关闭；数据库未就绪时拒绝连接。账号切换或退出时关闭旧连接，不能把旧账号事件交给新账号。

所有命令携带 `id`（8–128 位字母、数字、短横线或下划线），用于匹配本次响应。`data.request_id` 是持久化的消息幂等标识，与命令 id 分开：断线后手动重发可换命令 id，但必须保留原 request_id、正文、attachment_ids 及顺序和 options。

| 命令 type | 其他字段 | 成功 result.data |
| --- | --- | --- |
| `task.create` | `data: TaskCreate`，含 prompt、request_id、可选 project_id/options | TaskDetail |
| `task.message` | `task_id`、`data: TaskMessage` | TaskDetail |
| `task.retry` | `task_id`、`turn_id`、`data: TaskRetry` | TaskDetail |
| `task.cancel` | `task_id`、`data: {turn_id}` | TaskDetail |
| `task.subscribe` | `task_id`、可选 cursor | null；另外推送 snapshot/turn |
| `task.unsubscribe` | `task_id` | null |
| `ping` | 无 | null |
| `client.attach` | `client_id`；连接内不可切换 | null；随后推送该执行端的未结束调用 |
| `tool.get` | `task_id`、`turn_id`、`call_id` | ClientToolCall |
| `tool.claim` | 上述调用标识、稳定的 `request_id` | ClientToolClaim（含 claim_token） |
| `tool.result` | 上述调用标识、`data: {claim_token,output,is_error?}` | ClientToolCall |
| `tool.review` | 三个调用标识、`data: {request_id,claim_token,action_digest,context}` | ClientToolCall；审查建议不授权本机执行 |
| `task.approval.set` | `task_id`、`data: {request_id,expected_revision,mode}` | TaskDetail |

例如创建无项目任务并发送首条消息：

```json
{"type":"task.create","id":"command-create-001","data":{"project_id":null,"request_id":"message-first-001","prompt":"请概括下一步工作"}}
```

命令成功返回 `{"type":"result","id":"command-create-001","data":{...TaskDetail}}`。这表示消息已经保存和排队，不表示生成完成。创建、追问和重试复用当前任务服务，每轮使用最新项目配置及 options。后台仍按原 worker 串行执行，订阅多个任务不会开启并行推理，也不会创建专家。

取到 task_id 后订阅；可直接使用创建响应或 REST 首页的 event_cursor：

```json
{"type":"task.subscribe","id":"command-watch-001","task_id":"task_example","cursor":"opaque_event_cursor"}
```

不带 cursor 时先返回 `{"type":"snapshot","data":TaskDetail}`（最近一页），随后推送 `{"type":"turn","data":{"task":TaskSummary,"turn":TaskTurn,"cursor":"..."}}`。订阅时先补齐游标之后的变化，再返回订阅命令的 result；不同任务事件可以交错到达，客户端按 task_id/turn_id 分发。snapshot 与 turn 是完整状态，按 sequence 稳定排序，按 list_revision 防止旧摘要覆盖新状态；reply 和 reasoning 分别是累计全文，不能当新增文字重复追加。即使 reply 为空或 list_revision 未变，也要处理 data.turn.reasoning 的思考更新。正文和思考会在生成过程中持续推送，无需客户端反复 GET。

每条连接最多订阅 32 个不同任务，客户端发往服务端的单帧上限 512 KiB；超限帧以 1009 关闭。客户端只订阅当前打开的正文，其他任务的侧栏摘要仍通过列表增量同步。无关任务不会自动把全部正文发到连接。

服务端沿用约 0.5 秒保存一次的正文/活动状态，检查订阅变化后推送；中间片段可以合并，纯租约心跳不推正文。空闲连接约每 10 秒发送 `{"type":"heartbeat"}`。正文是实时状态推送，不是逐 token 的不可变事件账本。客户端工具通过同一连接下发 tool.call，领取后回传结果，模型继续当前轮次，见接入指南第 12 节。

### 断线续接和错误

断线只停止观察，不取消任务。客户端保存每个任务最近应用的 cursor，重新认证后分别 task.subscribe 续接，补齐离线期间各轮的最新持久状态；不重新发送用户消息、不重放工具操作。一次轮次 completed 不代表连接或 Task 结束，后续追问继续沿用 task_id 和连接。加载更早历史仍使用 REST，不推进实时游标。

业务错误返回 `{"type":"error","id":"<command id>","status":409,"error":{"code":"...","message":"...","details":{}}}`。订阅推送错误携带 task_id，移除该任务订阅，不影响其他任务。游标失效返回 PROJECT_SYNC_RESET，需丢弃该任务旧实时基线，重新获取最近一页；跨用户/任务游标同样无效。格式错误为 TASK_FRAME_INVALID；无法解析命令 id 时错误没有 id，禁止回显无效请求正文或 token。

网页在当前任务订阅断开时按退避重连；切到后台或离线时解除订阅，回前台恢复。连接和命令等待都有超时，网络故障时挂起的发送不会自动重放；保留 request_id 后由用户显式重试。较旧的写入回执、历史页不能覆盖已经收到的终态。

已有 `GET /api/project-tasks/{task_id}/events` SSE 保持原来的状态和游标语义，供已有调用方使用；工具领取和回传使用 WebSocket。正式网页的实时订阅与发送已切到 WebSocket。

### 错误和网络重试

业务错误沿用 Octop 格式，例如：

```json
{
  "error": {
    "code": "PROJECT_REVISION_CONFLICT",
    "message": "项目已被修改，请刷新后重新编辑。",
    "details": {}
  }
}
```

App 根据 HTTP 状态和 `error.code` 分支，message 用于展示，不能作为程序判断依据。字段缺失、类型错误、未声明字段等请求校验错误通常返回 HTTP 422 的 FastAPI `detail` 数组，不能假定所有错误都有 error 对象。运行中发生的模型错误则通常记录在 `turns[].error_code`，详情读取仍返回 200。

| HTTP / code | 含义 | 客户端处理 |
| --- | --- | --- |
| 401 | 登录失效或未登录 | 恢复登录，不回退本地项目或旧协议 |
| 403 | 当前账号无权限 | 展示权限错误，不自动换身份重试 |
| 404 / `NOT_FOUND` | 资源不存在或不属于当前用户 | 移除失效选择并刷新列表；不暴露其他用户数据 |
| 409 / `PROJECT_NAME_TAKEN` | 同账号下项目重名 | 提示更换名称 |
| 409 / `PROJECT_REQUEST_CONFLICT` | 创建 key 被用于不同内容 | 先核对原创建结果；新的创建使用新 key |
| 409 / `PROJECT_REVISION_CONFLICT` | 项目版本已变化 | 读取最新详情，处理冲突后再提交 |
| 422 / `PROJECT_DIRECTORY_INVALID` | 服务端目录不存在或不可访问 | 重新选择有权限的服务端目录 |
| 422 / `PROJECT_RESOURCE_INVALID` | Skill/MCP 引用不存在或无权使用 | 刷新资源目录后重新选择 |
| 409 / `TASK_BUSY` | 任务已有活动轮次 | 读取当前任务，等待结束或由用户停止 |
| 409 / `TASK_REVISION_CONFLICT` | 任务摘要版本已变化 | 刷新后重新编辑 |
| 409 / `TASK_RETRY_UNAVAILABLE` | 指定轮次不是最后一个失败/取消轮次 | 刷新会话，不重试旧轮次 |
| 409 / `TASK_REQUEST_CONFLICT` | 消息标识已用于不同内容或作用域 | 核对原发送结果，禁止换内容复用标识 |
| 409 / `TASK_MODEL_UNAVAILABLE` | 没有可用模型 | 保留草稿，配置模型后重新发送；不会创建空任务 |
| 409 / `CONTROL_PLANE_BUSY` | 任务 worker 尚不可受理 | 保留发送标识和草稿，待服务恢复后重试 |

创建项目丢失响应时，用原 key 和原内容重试。PATCH 丢失响应后先 GET 核对：原请求可能已成功，重复旧 revision 会返回冲突；不能不经核对就换成最新 revision 覆盖。任务创建/发送丢失响应时，用原 request_id 和原消息重试，不先创建第二个任务。

### 已验证范围与未交付项

| 范围 | 当前证据 |
| --- | --- |
| 项目创建/读取/修改、PATCH 保留未提交字段、空值规则、版本冲突 | 服务端定向自动化通过 |
| 同 key 并发创建、不同内容冲突、用户隔离、首次响应回放、失败不消耗 key | 服务端定向自动化通过；创建回执持久化在数据库 |
| 专用协议和项目任务 Bridge | 已从代码移除，当前只提供本机项目和任务接口；已有数据保留 |
| 项目与无项目任务、首轮和追问、幂等发送、历史恢复、取消基础行为 | 定向自动化通过；包含指定轮次取消、迟到取消不影响下一轮、手动重试和未完成上下文 |
| 正式网页创建重试复用 key、改用 PATCH、保存后增量同步 | 前端自动化通过 |
| 本机 8088 | 已更新到 schema 33 并重启后端；升级前已备份，2 个项目、5 个任务、6 个轮次和历史 ac_* 数据逐表摘要一致，数据库完整性及新同步接口验证通过；真实 GLM 双轮记录来自前一轮 |
| 当前原生 AgentComputer App | 本轮未修改，尚未验收新独立接口适配 |
| PostgreSQL、目标 Ubuntu 盒子、真实 MCP 供应商 | 迁移文件/接口实现不等于实机验证，目前未验收 |

此前专用协议和 Bridge 的验证记录仅反映历史实现，不代表现行可用接口。当前路由契约验证覆盖标准项目/任务入口，以及已移除地址不可用、不触发转发的边界。完整 `make all` 按约定后置。

本轮已补齐会话历史分页、可续接轮次状态流、精确取消和失败/取消后的继续及手动重试。任务列表分页和摘要同步已有实现。本轮继续补齐任务改名、归档/恢复和逐轮模型、Skill/MCP 选择。已提供服务端附件产物、任务审批模式与独立 Reviewer；客户端接入见专门交接文档。旧能力声明不再提供，历史 Run/文档/审批协议不可用。

每轮还返回 `content_blocks`（按发生顺序的摘要、思考、回答和工具活动）及 `content_revision`（内容版本）。内容块是完整快照，客户端按版本替换并保留 block_id 的展示状态；有块时不重复渲染累计 reply/reasoning。历史未记录分段时数组为空，继续展示原文。公开摘要来自模型实际返回，GLM 的 reasoning_content 保持思考类型。字段与恢复规则见 [有序内容块](project-task-websocket.md#有序内容块)。

2026 年 10 月 19 日后续公开进展方案：动作生成开启时，宿主立即保存已收到请求的开场及计划当前步骤的意图进展，首条带 `introduction=true`。独立异步模型只根据用户目标、已保存计划和真实工具事实补充文案，主执行不等待通知或宿主 brief。工具 `tool_label` 立即可用，`brief` 可缺失/null，生成后最多 240 字符，按原 call_id 更新同一工具块；不修改业务 schema、计划、审批或执行事实。主模型新请求隐藏 `message_notify_user`，注册表只保留已有调用的正常结束路径；生成文案不进入 reply，也不回灌模型。

每轮旁路最多一个模型请求在途和一个可替换的最新待处理快照，同身份去重、过期结果丢弃。复用该轮 model_ref，但独立使用 8 秒超时、最多 512 个输出 token 和支持的关闭思考/低 effort 参数，不新增模型开关或修改全局配置。主回答开始、暂停或等待、取消及终态时关闭旁路，迟到内容丢弃；已收到的 usage 仍原子加到原 turn，与主模型计量互不覆盖。恢复不重跑旧开场或补跑旧说明。旁路 progress 的 tool_call_id 使用稳定 `narration:...` 身份，不是真实工具调用；协议与现有字段保持不变，无新 API 字段、迁移或 Swift/UI 修改。此前开场/必填 brief 门控的交付和客户端实测只保留为历史记录，当前规则见[独立异步公开进展](project-task-planning-and-progress.md#独立异步公开进展)。

TaskTurn 的 `started_at/finished_at/processing_duration_ms` 分别为首次实际开始、终态及其墙钟毫秒跨度，包含审批/暂停等待、排除首次排队；历史或运行中的未知值为 null。带工具响应的普通正文归过程，只有确认无工具且未处于计划执行中的正文进入答案。计划完成或受阻后，已禁用工具的最终请求直接流式写入答案，不等待整段响应分类。

当前实现为 [`projects` 路由](../src/octop/api/routers/projects.py)、[项目请求模型](../src/octop/infra/projects/models.py)、[项目服务](../src/octop/infra/projects/service.py)、[任务服务](../src/octop/infra/projects/tasks.py)；验证依据为[项目集成用例](../tests/integration/test_projects.py)、[任务集成用例](../tests/integration/test_project_tasks.py)、[公开路由契约用例](../tests/integration/test_project_api_contract.py)。


Every route is mounted under `/api`. JSON unless otherwise noted; SSE
endpoints emit `text/event-stream`; chat turns use a WebSocket
described in [Chat (WebSocket)](#chat-websocket).

> **Interactive docs** (Scalar) live at `/api/docs` when
> `enable_api_docs=true` (or `OCTOP_ENABLE_API_DOCS=1`). The schema
> is at `/api/openapi.json`.

项目和任务接口在 `/api/docs` 的 `projects` 分组中；不再提供 `agentcomputer` 分组。历史原型测试记录不表示当前接口或原生 App 已验收。

## Authentication

调用方使用 `POST /api/auth/login` 返回的用户 Bearer token 访问项目和任务接口。响应中的 `X-Octop-Access-Token` 是滑动续期令牌，收到后应更新保存的 token。项目和任务 ID 是不透明字符串，不应转换为 UUID。

| Header | Value |
|--------|-------|
| `Authorization` | `Bearer <access_token>` from `POST /api/auth/login` |

Tokens expire after `OCTOP_ACCESS_TOKEN_TTL` seconds (default 24 h).
Rotating the JWT secret (`octop admin rotate-jwt-secret`) invalidates
every outstanding token immediately. Login attempts are rate-limited
(`OCTOP_LOGIN_MAX_ATTEMPTS` / `OCTOP_LOGIN_LOCKOUT_SECONDS`); the
admin can clear the lockout with `POST /api/users/{id}/unlock-login`.

### Auth column legend

- **public** — no token required.
- **user** — any logged-in account.
- **owner** — same user that owns the resource (or admin).
- **admin** — admin role required.

### Public endpoints (no token)

`/api/docs`, `/api/openapi.json`, `/api/health`, `/api/setup/*`,
`/api/auth/login`, `/api/auth/captcha`, `/api/auth/oidc/status`, `/api/auth/oidc/start`,
`/api/auth/oidc/callback`, `/api/auth/oidc/exchange`, `/api/auth/oauth/status`,
`/api/auth/oauth/start`, `/api/auth/oauth/callback`, `/api/auth/oauth/exchange`,
`/api/auth/ldap/status`,
`/api/connectors/oauth/callback`, and `/api/internal/mcp/*`. All other routes are JWT-gated by
`api/middleware/jwt_auth.py`; the setup lockdown middleware
(`api/middleware/setup_lockdown.py`) additionally blocks non-setup
routes until the wizard finishes.

Password login may require a vendor captcha token. `GET /api/auth/captcha`
returns `{provider: "slider"}` by default (dashboard slider only; no server
check). When a strong provider is active (`turnstile`, `hcaptcha`,
`recaptcha`, `recaptcha-v3`, `tencent`), `POST /api/auth/login` must include
`captcha_token` (max 4096 characters; for `tencent` the dashboard sends the
callback pair as `ticket:randstr`, verified via GET with the client IP).
Captcha failures do not increment
login lockout. OIDC login is unchanged. A request for an unknown username
with a garbage token still triggers one outbound siteverify request (bounded
by a 10s timeout and the token length limit); a known locked user is
rejected before siteverify.

Offline recovery: delete the `captcha.settings` row via local CLI /
`settings_repo` if a settings-sourced strong provider is unreachable. If
the process refused to start because `OCTOP_CAPTCHA_PROVIDER` is strong
without both keys and no readable settings blob, edit `~/.octop/env` on
disk — the dashboard and `PUT /api/envs` are unreachable until the
process starts. `GET /api/envs` redacts `OCTOP_CAPTCHA_SECRET` as
`********`; sending that sentinel on PUT keeps the on-disk value.

If you serve the dashboard behind a CSP, allow
`challenges.cloudflare.com`; `js.hcaptcha.com`, `newassets.hcaptcha.com`,
`api.hcaptcha.com`; `www.google.com`, `www.gstatic.com`;
`turing.captcha.qcloud.com`, `captcha.qq.com`, `ssl.captcha.qq.com`. Octop
does not set these headers itself.

## Setup & auth

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/setup/status` | public | `{required, completed, has_admin}` |
| `GET`    | `/setup/presets` | public | Provider templates for the wizard |
| `POST`   | `/setup/begin` | public | Begin a wizard session (no password) |
| `POST`   | `/setup/verify-password` | public | Exchange the wizard password for a session token |
| `GET`    | `/setup/validate-token` | public | Check a wizard session token |
| `POST`   | `/setup/initial-admin` | public | body `{username, password, display_name?, email?}` → `201` |
| `POST`   | `/setup/resume-wizard` | public | Issue a fresh wizard token mid-setup |
| `POST`   | `/setup/test-provider` | public | Ping a provider draft (kind/base_url/api_key/model) |
| `POST`   | `/setup/finish` | public | Finalise setup and unlock the rest of the API |
| `GET`    | `/auth/captcha` | public | `{provider, site_key?}` for the login widget; 503 while setup is required |
| `POST`   | `/auth/login` | public | body `{username, password, captcha_token?}` (`username` may be email) → `{access_token, role, user, ...}`. When no local password matches, the credentials are retried against the configured LDAP directory; a first directory login provisions an Octop account (unless `auto_provision` is off) and derives the role from `admin_groups` |
| `GET`    | `/auth/oidc/status` | public | OIDC login availability and provider display name |
| `POST`   | `/auth/oidc/start` | public | body `{redirect_after?}` → identity-provider authorization URL |
| `GET`    | `/auth/oidc/callback` | public | Identity-provider callback; redirects to dashboard login completion |
| `POST`   | `/auth/oidc/exchange` | public | body `{code}` → same JWT response as `/auth/login` |
| `GET`    | `/auth/oidc/config` | admin | OIDC provider configuration and callback URL; client secret is omitted |
| `PUT`    | `/auth/oidc/config` | admin | Write OIDC provider configuration; `client_secret` is write-only |
| `POST`   | `/auth/oidc/config/test` | admin | Verify configured discovery metadata and JWKS endpoint |
| `GET`    | `/auth/oauth/status` | public | `{providers:[{kind, display_name, enabled}]}` |
| `POST`   | `/auth/oauth/start` | public | body `{kind, redirect_after?}` → authorization URL |
| `GET`    | `/auth/oauth/callback` | public | App OAuth callback (`code` or DingTalk `authCode`); same completion page as OIDC |
| `POST`   | `/auth/oauth/exchange` | public | Same one-time code exchange as `/auth/oidc/exchange` |
| `POST`   | `/auth/oauth/bind/start` | user | body `{kind, redirect_after?}` → bind the identity to the current user |
| `POST`   | `/auth/oauth/unbind` | user | Unlink one SSO identity by `kind` (requires a local password when it is the last login method) |
| `GET`    | `/auth/oauth/providers/{kind}` | admin | Provider config; `kind` is `oidc`, `feishu`, `dingtalk`, or `wecom` |
| `PUT`    | `/auth/oauth/providers/{kind}` | admin | Upsert provider config; `client_secret` is write-only; WeCom uses `extra.agent_id` |
| `POST`   | `/auth/oauth/providers/{kind}/test` | admin | Test provider credentials |
| `GET`    | `/auth/ldap/status` | public | `{enabled, display_name}` for the login page hint |
| `GET`    | `/auth/ldap/config` | admin | Directory settings; `bind_password` is never returned (`has_bind_password` instead) |
| `PUT`    | `/auth/ldap/config` | admin | Upsert directory settings; `bind_password` is write-only (omit to keep the stored one) |
| `POST`   | `/auth/ldap/config/test` | admin | Service bind + probe of `user_base_dn` → `{ok, detail}` |
| `POST`   | `/auth/logout` | user | `204` |
| `GET`    | `/auth/me` | user | `{id, username, role, display_name, locale, ...}` |
| `PATCH`  | `/auth/me` | user | body `{display_name?, locale?, ...}` |
| `POST`   | `/auth/change-password` | user | body `{old_password, new_password}` → `204`. Accounts without a local password (directory/SSO-provisioned) get `PASSWORD_NOT_SET` |
| `GET`    | `/health` | public | `{status: "ok", version}` |

## Users (admin)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/users` | admin | `[{id, username, role, display_name, email, enabled, ...}]` |
| `POST`   | `/users` | admin | body `{username, password, role, display_name?, email?, permissions?, workspace_root_dir?, token_quota?}` → `201` |
| `GET`    | `/users/{id}` | admin | full user row |
| `PATCH`  | `/users/{id}` | admin | body subset of `{role, display_name, email, enabled, locale}` |
| `POST`   | `/users/{id}/reset-password` | admin | body `{new_password}` → `204` |
| `POST`   | `/users/{id}/unlock-login` | admin | `204` (clears the lockout) |
| `DELETE` | `/users/{id}` | admin | `204` |

## Agents

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents` | user | `[{id, agent_id, name, persona_mbti, state, unread_count, ...}]` |
| `POST`   | `/agents` | user | body `{name, persona_mbti?, default_model?, system_prompt?, description?, icon?, template_name?, config?}` → `201` |
| `GET`    | `/agents/{id}` | owner | full agent row |
| `PATCH`  | `/agents/{id}` | owner | body subset of create body |
| `DELETE` | `/agents/{id}` | owner | `204` |
| `POST`   | `/agents/{id}/start` | owner | `204` |
| `POST`   | `/agents/{id}/stop` | owner | `204` |
| `POST`   | `/agents/{id}/reload` | owner | `204` (rebuild harness runtime) |
| `POST`   | `/agents/{id}/read` | owner | `204` (mark unread badge cleared) |
| `GET`    | `/agents/{id}/status` | owner | `{state, last_error?, memory_maintenance?, ...}` |
| `POST`   | `/agents/from-expert/{expert_id}` | user | body `{name, ...}` → `201` (creates from bundled expert template) |
| `GET`    | `/agents/{id}/tool-settings` | owner | built-in + installed plugin tools with enable / disableable / available flags |
| `PUT`    | `/agents/{id}/tool-settings` | owner | body `{disabled_builtin: string[], plugins?}` — persists denylist + plugin flags (hot-sync, no reload) |
| `PATCH`  | `/agents/{id}/tool-settings/{tool_name}` | owner | body `{enabled, source, plugin_id?}` — toggle one tool (hot-sync) |

## Skills and skill packages

AgentComputer 可复用用户 Bearer token 直接调用这些通用接口。技能目录、项目选择、共享技能包管理的请求与响应见 [AgentComputer 技能接口接入说明](agentcomputer-skills.md)。

Skill copy operations create snapshots; they do not keep the source and
destination synchronized after the request completes.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/skills/hub/search?q=关键词&limit=50` | user | Search SkillHub without an agent |
| `GET` | `/skills/hub/rankings?type=all` | user | Browse SkillHub rankings without an agent |
| `GET` | `/projects/resources` | user | Installed skills available for independent projects; no `skill_packages` permission required |
| `GET` | `/skill-packages` | user (`skill_packages`) | List global packages and current user's `can_write` / `can_copy` flags |
| `GET` | `/skill-packages?writable_only=true` | user (`skill_packages`) | List only packages the current user may modify; admins may modify all packages |
| `POST` | `/agents/{id}/skill-packages/{package_id}/copy` | owner (`skill_packages`) | body `{skill_slugs: string[], overwrite?: boolean}`; copy selected package skills into the workspace |
| `POST` | `/agents/{id}/skills/{slug}/push-to-package` | owner (`skill_packages`) | body `{package_id, overwrite?: boolean}`; copy a workspace skill into a package created by the user (or any package for admins) |

## Chat (WebSocket)

| Path | Auth | Notes |
|------|------|-------|
| `WS /agents/{id}/chat/ws?token=<jwt>` | owner | Primary dashboard turn endpoint. Send `{"type":"user_turn", ...}` frames (optional `hitl_policy` is a thread-scoped tool-approval bypass); server replies with harness stream chunks ending in `{"type":"done"}` or `{"type":"error","message":"..."}`. `{"type":"ping"}` → `{"type":"pong"}`. `{"type":"subscribe","thread_id"}` → `{"type":"turn_status","thread_id","active"}` (attach to an in-flight turn without cancelling on disconnect). `{"type":"cancel","thread_id"}` stops the active turn (explicit stop; disconnect alone does **not** cancel). |
| `GET /agents/{id}/chat/welcome` | agent access | `{welcome_message, quick_prompts, task_examples}`; `task_examples` is `null` when the workspace field is absent |
| `POST /agents/{id}/chat/polish` | owner | body `{text, default_model?}` → `{text}` (one-shot prompt refinement) |
| `POST /agents/{id}/chat/hitl/resume` | owner | body `{thread_id, decisions: [...], hitl_policy?}` → SSE chunk stream; finishes with `{"type":"done"}`. Optional `hitl_policy` (`ask` / `allow_all` / `allow_tools`) is a thread-scoped bypass and does not change global tool-approval settings. |

### Legacy SSE

The previous `POST /agents/{id}/chat/stream` is gone. The dashboard
streams turns over the WebSocket above; HITL resume stays on SSE
because each request is a one-shot continuation.

## Threads & history

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents/{id}/chat/sessions` | owner | `[{id, thread_id, title, archived, last_active, unread, ...}]` |
| `POST`   | `/agents/{id}/chat/sessions` | owner | body `{session_key?}` → `{thread_id, session_key}` |
| `PATCH`  | `/agents/{id}/chat/sessions/{thread_id}` | owner | body `{title?, pinned?}` → updated row |
| `DELETE` | `/agents/{id}/chat/sessions/{thread_id}` | owner | `204` (archives the active row) |
| `GET`    | `/agents/{id}/chat/sessions/{thread_id}/history` | owner | paginated message history; `turn_active` tells a reconnecting client whether to re-`subscribe` over the chat WebSocket |

**Tool result blocks.** `tool_result` blocks in history (and `tool_result`
frames on the chat WebSocket) carry the tool's return value in `output`. When a
plugin returns a large `octop_ui` payload, the backend offloads the envelope's
`data` field: `output` then contains the slim envelope with
`data_ref: "artifact"`, and the full payload is on the block's `artifact` key
(absent otherwise). An explicit `data` key always takes precedence over
`data_ref` when both appear. Clients that render plugin UIs must resolve
`data_ref` from `artifact`; clients that only read `output` keep working.

### Trajectory ledger

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory` | owner | paginated event summaries; use `before_seq` for older rows |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/events/{event_id}` | owner | full event payload |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/metrics` | owner | aggregated turn, timing, and token metrics |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/stream` | owner | live SSE events; resume with `after_seq` or `Last-Event-ID` |
| `GET` | `/agents/{id}/threads/{thread_id}/trajectory/export` | owner | full ledger download as JSONL (default) or JSON |

## Channels

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents/{aid}/channels` | owner | list of channel rows |
| `POST`   | `/agents/{aid}/channels` | owner | body `{kind, name, config}` → `201` |
| `GET`    | `/agents/{aid}/channels/{cid}` | owner | channel row |
| `PATCH`  | `/agents/{aid}/channels/{cid}` | owner | body subset → updated row |
| `DELETE` | `/agents/{aid}/channels/{cid}` | owner | `204` |
| `POST`   | `/agents/{aid}/channels/{cid}/test` | owner | `{ok, error?}` (instantiate → start → stop) |
| `POST`   | `/agents/{aid}/channels/probe` | owner | `{ok, reason?, detail?}` — preflight a candidate config |
| `POST`   | `/agents/{aid}/channels/{platform}/qrcode/generate` | owner | platform-specific bot creator (wecom, weixin, feishu, yuanbao) |
| `POST`   | `/agents/{aid}/channels/{platform}/qrcode/poll` | owner | poll bot creator state |
| `POST`   | `/agents/{aid}/channels/{platform}/bot-creator/start` | owner | start a bot-creator flow |
| `POST`   | `/agents/{aid}/channels/{platform}/bot-creator/poll` | owner | poll progress |
| `POST`   | `/agents/{aid}/channels/{platform}/bot-creator/stop` | owner | stop an in-flight bot creator |

## Cron

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/settings/timezone` | user | process-level `{timezone}` from `default_timezone` |
| `GET`    | `/settings/upload` | user | `{max_upload_mb, max_upload_bytes}` from `max_upload_mb` |
| `GET`    | `/settings/hitl` | user | `{enabled, tool_guard_require_approval, show_approval_ui}` — `show_approval_ui` is true when tool HITL is on or command guard is `require_approval` |
| `GET`    | `/settings/captcha` | `captcha` | `{active, available, providers, source, v3_min_score}`; secrets omitted |
| `PUT`    | `/settings/captcha` | `captcha` | merge `{active?, providers?}`; empty secret keeps ciphertext; `null` removes a pair |
| `GET`    | `/cron/settings` | user | compat alias of `/settings/timezone` |
| `GET`    | `/agents/{aid}/cron/examples` | agent access | `{task_examples}` from workspace `.octop/manifest.json` (`{zh,en}` string arrays, display-normalized to 3 or 6); `null` if the field is absent (dashboard keeps default cards). Prefer `GET /agents/{aid}/chat/welcome` which includes the same field. |
| `GET`    | `/agents/{aid}/cron` | owner only | list cron rows; non-owners (including admin) get `[]` |
| `POST`   | `/agents/{aid}/cron` | owner only | body `{name?, trigger, prompt, session_key?, fresh_thread?, enabled?, model?, task_type?}` → `201` |
| `GET`    | `/agents/{aid}/cron/{cid}` | owner only | cron row |
| `PATCH`  | `/agents/{aid}/cron/{cid}` | owner only | body subset → updated row |
| `DELETE` | `/agents/{aid}/cron/{cid}` | owner only | `204` |
| `POST`   | `/agents/{aid}/cron/{cid}/run-now` | owner only | `204` (fire immediately, off-schedule) |

`task_type` is `"text"` (push prompt directly to the session) or
`"agent"` (run the prompt through the LLM and push the reply).
Default: `"agent"`. `trigger` accepts cron expressions
(`"0 9 * * *"`) plus the `interval:N` / `date:ISO8601` aliases
documented in `infra/cron/trigger.py`. `prompt` must be non-empty and
≤ 2000 characters. `name` is an optional display label; when omitted,
the server derives one from `prompt`.

## Providers

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/providers` | user | providers visible to the user (own + shared) |
| `POST`   | `/providers` | user | body `{name, kind, base_url?, api_key?, model?, ...}` → `201` |
| `PATCH`  | `/providers/{id}` | owner | body subset → updated row |
| `DELETE` | `/providers/{id}` | owner | `204` (refuses if any agent references it) |
| `POST`   | `/providers/{id}/test` | user | `{ok, latency_ms?, error?}` (one-token ping with 10 s timeout) |
| `POST`   | `/admin/providers` | admin | same body as user POST; row has `user_id = NULL` |
| `PATCH`  | `/admin/providers/{id}` | admin | as user PATCH but works on shared rows |
| `DELETE` | `/admin/providers/{id}` | admin | `204` |

## Models

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/models/presets` | user | provider templates from `octop-harness` |
| `GET` | `/models` | user | resolved models across enabled providers |
| `GET` | `/models/active` | user | `{provider_name, model}` |
| `PUT` | `/models/active` | admin | body `{provider_name, model}` |

### Media generation models

These instance-wide endpoints require the `providers` permission (administrators bypass
permission checks). The Ark API key is write-only and encrypted at rest. Saving settings
reloads running agents so the image and video tools receive the new configuration.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/admin/media-generation` | providers | Return Volcengine Ark image/video settings; never returns the API key |
| `PUT` | `/admin/media-generation` | providers | Save enabled tools and Seedream/Seedance model IDs; an included API key is verified before saving |
| `POST` | `/admin/media-generation/test` | providers | Test credentials or a selected image/video model; model tests submit real, potentially billable requests |

## Voice

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`  | `/voice/presets` | user | voice provider presets |
| `GET`  | `/voice/providers` | user | user's voice providers |
| `GET`  | `/voice/active` | user | active TTS / STT configuration |
| `PUT`  | `/voice/active` | user | update active voice configuration |
| `POST` | `/voice/stt` | user | body `{audio, format?, language?}` → `{text, segments?}` |
| `POST` | `/voice/tts` | user | body `{text, voice?, format?}` → audio bytes |
| `GET`/`POST`/`PATCH`/`DELETE` | `/admin/voice/providers` | admin | admin voice provider CRUD |

## MBTI & personas

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/mbti/codes` | user | `[{code: "INTJ", ...}, ...]` (16 codes + `_default`) |
| `GET`    | `/mbti/codes/{code}` | user | full profile (dimensions, behaviour, UI metadata) |
| `GET`    | `/mbti/preview/{code}` | user | rendered persona template (legacy `/api/personas/{code}`) |
| `PUT`    | `/agents/{aid}/mbti` | owner | body `{code}` → apply persona and reload |
| `GET`    | `/personas` | user | `[{code}, ...]` (compat shim) |
| `GET`    | `/personas/{code}` | user | rendered template (compat shim) |

Persona content lives in `src/octop/infra/agents/persona/mbti_profiles.py` —
see [Personas](./personas.md).

## Experts

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/experts` | user | bundled expert catalog (includes `task_examples` `{zh,en}` when present) |
| `GET`    | `/experts/{expert_id}` | user | full expert template (SOUL.md, skills, files, `task_examples`) |
| `POST`   | `/agents/from-expert/{expert_id}` | user | body `{name, locale?, quick_prompts?, ...}` → `201`; optional `quick_prompts` overwrites workspace cards after seed |

Bundled experts live in `src/octop/infra/agents/experts/library/`
(en/zh divisions); the catalog is locale-aware via
`Accept-Language` / user preference.

## Workspace, skills, subagents, memory, files

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/agents/{aid}/workspace/tree` | owner | list dir (default `.`) |
| `GET`    | `/agents/{aid}/workspace/file` | owner | read a content file |
| `PUT`    | `/agents/{aid}/workspace/file` | owner | write a content file (via `BackendWorkspace`) |
| `DELETE` | `/agents/{aid}/workspace/file` | owner | delete a content file |
| `POST`   | `/agents/{aid}/workspace/rename` | owner | rename / move |
| `POST`   | `/agents/{aid}/workspace/upload` | owner | multipart upload → backend (not `max_upload_mb`) |
| `GET`    | `/agents/{aid}/workspace/download` | owner | download a file |
| `GET`    | `/agents/{aid}/workspace/glob` | owner | glob backend paths |
| `GET`    | `/agents/{aid}/workspace/grep` | owner | grep backend files |
| `GET`    | `/agents/{aid}/workspace/...` | owner | see `api/routers/workspace.py` |
| `GET`    | `/agents/{aid}/skills` | owner | list installed skills |
| `PUT`    | `/agents/{aid}/skills/{slug}` | owner | Replace skill content; body `{content?, files?}` |
| `POST`   | `/agents/{aid}/skills/{slug}/enable` | owner | Enable a skill → `204` |
| `POST`   | `/agents/{aid}/skills/{slug}/disable` | owner | Disable a skill → `204` |
| `GET`    | `/agents/{aid}/skills/hub/search` | owner | Skill Hub search |
| `GET`    | `/agents/{aid}/skills/hub/rankings` | owner | Skill Hub rankings |
| `POST`   | `/agents/{aid}/skills/hub/install` | owner | body `{skill_name, enable?, display_name?, icon_url?, label?, summary?}` → `201` |
| `GET`    | `/subagent-catalog/divisions` | user | bundled subagent divisions |
| `GET`    | `/subagent-catalog` | user | bundled subagent catalog |
| `GET`    | `/subagent-catalog/{slug}` | user | full subagent definition |
| `GET`    | `/agents/{aid}/subagents` | owner | installed subagents for an agent |
| `POST`   | `/agents/{aid}/subagents` | owner | install a bundled subagent |
| `GET`    | `/agents/{aid}/heartbeat-config` | owner | read heartbeat YAML |
| `PUT`    | `/agents/{aid}/heartbeat-config` | owner | write heartbeat YAML |
| `GET`    | `/agents/{aid}/memory/daily` | owner | list daily memory files |
| `GET`    | `/agents/{aid}/memory/daily/{filename}` | owner | read one daily memory |
| `DELETE` | `/agents/{aid}/memory/daily/{filename}` | owner | delete one daily memory |
| `GET`/`POST` | `/memory/...` | user | memory API (dashboard memory tab) |

## ACP (Agent Client Protocol)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/acp` | user | current user's global runner list |
| `PUT`    | `/acp` | user | replace global runners |
| `GET`    | `/acp/{runner_name}` | user | one runner |
| `PUT`    | `/acp/{runner_name}` | user | upsert one runner |
| `DELETE` | `/acp/{runner_name}` | user | delete a custom runner (built-ins are protected) |
| `GET`    | `/agents/{aid}/acp` | owner | global runners + this agent's `tool_enabled` flag |
| `PUT`    | `/agents/{aid}/acp` | owner | update `tool_enabled` and optionally the global list |
| `PUT`    | `/agents/{aid}/acp/tool` | owner | toggle the `acp_runner` tool only |

See [ACP integration](./acp.md) for the runner object schema and the
Zed setup example.

## Storage backends

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`POST`/`PATCH`/`DELETE` | `/storage-backends` | user | per-user remote backend connections |
| `GET`/`POST`/`PATCH`/`DELETE` | `/admin/storage-backends` | admin | admin-managed backends |

## Host filesystem (dashboard)

Browse the host OS directory tree when configuring a local backend
`root_dir` (`local_shell` / `filesystem`). Authenticated users only;
sensitive mounts (`/proc`, `/sys`, `/dev`, `/etc`, `/root` on POSIX)
are rejected, except the process home and its subdirectories (so a
server running as root may use `/root` as the default `root_dir`).
Listing is single-level and capped; write probe runs only
for non-`/` paths.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/filesystem/dirs?path=<abs>` | user | `{path, entries: [{path, name}]}` — one directory level |
| `POST`   | `/filesystem/probe` | user | body `{path}` → `{ok, path?}` or `{ok: false, code, detail?}` (`not_directory`, `permission_denied`, `write_failed`, `not_allowed`) |
| `POST`   | `/filesystem/mkdir` | user | body `{path, base_name?}` → `{path, name}` — create child dir (`base_name` defaults to `New Folder`; collisions become `Name (2)`, …) |
| `POST`   | `/filesystem/rename` | user | body `{path, new_name}` → `{path, name}` — rename basename only |

## Connectors & OAuth

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/connectors/catalog` | user | connector catalog (Notion, Figma, …) |
| `GET`    | `/connectors/test-credentials` | user | preflight credentials |
| `GET`    | `/connector-instances` | user | list instances |
| `POST`   | `/connector-instances` | user | create instance |
| `GET`/`PATCH`/`DELETE` | `/connector-instances/{id}` | user | CRUD on an instance |
| `POST`   | `/connector-instances/{id}/test` | user | test a configured instance |
| `POST`   | `/connector-instances/{id}/refresh` | user | refresh OAuth tokens |
| `GET`    | `/connectors/auth/{kind}/info` | user | auth flow info |
| `GET`    | `/connectors/auth/{kind}/authorize-url` | user | build the authorize URL |
| `POST`   | `/connectors/auth/{kind}/exchange-code` | user | exchange auth code |
| `POST`   | `/connectors/oauth/start` | user | start OAuth (catalog or custom MCP via `target`) |
| `POST`   | `/connectors/oauth/{kind}/start` | user | legacy catalog OAuth start |
| `PUT`    | `/connectors/custom-mcp` | user | save custom MCP server map |
| `PATCH`  | `/connectors/custom-mcp/servers/{name}` | user | patch `enabled` / `default_open` on one server |
| `POST`   | `/connectors/custom-mcp/test` | user | probe a custom MCP server (inline spec or saved name) |
| `GET`    | `/connectors/oauth/callback` | public | OAuth redirect target |
| `GET`    | `/connectors/oauth/pending/{state_id}` | user | poll the OAuth result |

Custom MCP OAuth (streamable HTTP, public HTTPS URL only): Octop discovers the authorization
server from the MCP URL (401 / RFC 9728 protected-resource metadata), requires dynamic client
registration (DCR), stores encrypted tokens in the custom MCP spec, and injects `Authorization:
Bearer` when loading tools. Loopback and LAN MCP URLs may use HTTP and do not use remote OAuth
discovery.

## Internal MCP (octop-harness agents)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST`/`GET`/… | `/internal/mcp/*` | public (mTLS / network-isolated) | MCP gateway used by octop-harness agents (not the dashboard) |

## Observability & security

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`PUT` | `/admin/observability` | admin | Langfuse configuration (host, project, env) |
| `GET`    | `/admin/security` | admin | global security policy |
| `PUT`    | `/admin/security` | admin | update global policy |
| `GET`    | `/admin/security/tool-guard/rules` | admin | active command guard rules |
| `GET`    | `/admin/security/tool-guard/rules/raw` | admin | editable YAML |
| `PUT`    | `/admin/security/tool-guard/rules/raw` | admin | save YAML |
| `POST`   | `/admin/security/tool-guard/rules/reset` | admin | reset to shipped defaults |
| `GET`    | `/admin/security/defaults` | admin | defaults + rule catalogs |

## TLS (Let's Encrypt)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`  | `/admin/tls/status` | admin | current cert + issuance task status |
| `POST` | `/admin/tls/preflight` | admin | preflight (port 80, DNS) |
| `POST` | `/admin/tls/issue` | admin | start HTTP-01 issuance |

`GET /.well-known/acme-challenge/{token}` is the HTTP-01 challenge
endpoint (public, mounted directly in `api/app.py`).

## Browser, terminal, uploads

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `WS`/`POST`/`GET`/… | `/agents/{aid}/terminal` | owner | AI-assisted remote PTY |
| `GET` | `/agents/{aid}/terminal/context` | owner | recent terminal context for the AI helper |
| `WS`/`POST`/`GET`/… | `/browser/...` | user | octop-browser sessions, live stream, record/replay |
| `POST` | `/browser/shutdown` | user | stop the current user's Octop-managed Chrome |
| `POST` | `/agents/{aid}/upload` | user | multipart upload → `{workspace}/inbound/` |
| `POST` | `/agents/{aid}/files/access-urls` | user | refresh inbound media URLs (signed) |
| `GET`  | `/agents/{aid}/files/{path}` | owner | read an inbound file |

## Updates, ollama, i18n, plugins, slash, preferences

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`POST` | `/update/status`, `/check`, `/upgrade`, `/progress`, `/restart` | admin | in-place server update flow |
| `GET`/`POST`/`DELETE` | `/ollama-models/...` | `ollama_models` | Ollama model discovery + downloads |
| `GET`/`PUT` | `/ollama-models/service` | `ollama_models` | Local daemon toggle; omit `enabled` to set `models_dir` only |
| `GET` | `/i18n/tools` | user | server-owned tool display names (locale-aware) |
| `GET` | `/i18n/locales` | public | available locales + fallback chain |
| `GET` | `/i18n/locales/{locale}/{namespace}` | public | one namespace bundle (errors, tools, channel, slash) |
| `GET`/`POST` | `/preferences` | user | UI preferences (per-user key/value) |
| `GET` | `/slash/commands` | user | slash command catalog for the composer menu |
| `GET`/`POST` | `/plugins` | user | installed plugin list / install flow |
| `POST` | `/plugins/reload` | admin (`plugins`) | reload plugins from disk into process |
| `PATCH` | `/plugins/{id}` | admin (`plugins`) | enable/disable plugin (`{ "enabled": bool }`) |
| `GET` | `/plugins/{id}/ui/{path}` | user | serve prebuilt plugin UI assets (`ui/dist/…`) |
| `DELETE` | `/plugins/{id}` | admin (`plugins`) | uninstall plugin |

## Usage & admin

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`    | `/usage/summary` | user | 当前用户的 token 用量；管理员可用 `as_user` 查看指定用户 |
| `GET`    | `/admin/usage/summary` | admin | 全部用户的 token 用量；可用 `user_id` 缩小范围 |
| `GET`    | `/usage/export.xlsx` | user | 导出相同用户、时间和资源筛选范围的用量明细与分组 |
| `GET`    | `/admin/usage/export.xlsx` | admin | 导出全局或指定用户的用量明细与分组 |
| `GET`    | `/admin/overview` | admin | `{user_count, agent_count, ...}` |
| `GET`    | `/admin/audit-log` | admin | recent audit rows |
| `GET`    | `/admin/metrics` | admin | `{messages_total, stream_errors_total, cron_runs_total, cron_errors_total, agent_active, ...}` |

用量汇总支持 `granularity=total|by_day|by_agent|by_model|by_project|by_task`，默认 `by_day`；所有用量接口支持 `window` 时间范围，以及 `agent_id`、`project_id`、`task_id` 精确筛选。`by_project`、`by_task` 仅统计独立任务，`by_agent` 仅统计专家会话，其余维度合并两类来源。项目与任务分组以公共 ID 为 `key`、当前项目名或任务标题为 `label`，同名资源分别计量；未关联项目的任务在项目分组中使用空 `key`、空 `label`，客户端显示「未关联项目」。

任务用量读取已有的轮次、标题生成、自动审核记录，包含已记录的上下文压缩与进展说明消耗；标题和审核增加 token 合计，但不增加会话轮次。保留的归档项目仍参与统计。任务的计量记录只有累计输入/输出，因此缓存、推理、模型调用次数字段中的 `0` 表示尚无细分数据；缓存命中率仅按专家会话中已有细分记录的输入计算。轮次含旁路累计消耗时，模型按该轮 `model_ref` 归属。时间范围采用服务端时区；任务按轮次/审核记录的创建时间归属，标题按任务首轮创建时间归属，不能作为逐次模型请求账单使用。已删除记录与没有输入/输出记录的历史对话无法从本接口恢复用量。

分组最多返回 100 个桶：日期取最近 100 天，其余按 token 合计取前 100 组；响应顶层合计始终覆盖筛选范围的全部记录。Excel 包含明细、按天、按专家、按模型、按项目、按任务六张工作表；明细追加项目 ID、项目名称、任务 ID、任务标题。明细最多导出最近 50,000 条计量记录，分组沿用上述 100 桶限制。

## Envs

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET`/`PUT` | `/agents/{aid}/envs` | owner | env-var preset for an agent's tool calls |
| `GET`/`POST`/`DELETE` | `/envs/presets` | user | reusable presets |

## Error envelope

Every non-2xx JSON response uses:

```json
{ "error": { "code": "AGENT_NOT_FOUND", "message": "...", "details": { } } }
```

`code` matches the `ErrorCode` enum in `octop.infra.errors`; the
server-localized `message` is rendered by `OctopError.to_envelope`
with the locale from `Accept-Language` (falling back to `en`). The
dashboard mirrors every code under `apiErrors.*` in
`dashboard/src/locales/{en,zh}.json`.

| Code | HTTP | Meaning |
|------|------|---------|
| `AUTH_FAILED` | 401 | Bad credentials |
| `TOKEN_EXPIRED` | 401 | JWT past its TTL |
| `LOGIN_LOCKED` | 423 | Too many failed attempts — wait `login_lockout_seconds` or call `/users/{id}/unlock-login` |
| `SETUP_REQUIRED` | 409 | Initial admin not yet created (or wizard not finished) |
| `FORBIDDEN` | 403 | Authenticated but not allowed |
| `NOT_FOUND` | 404 | No such row / route |
| `USER_DISABLED` | 403 | Account flag flipped off |
| `USERNAME_TAKEN` | 409 | Conflict on `users.username` |
| `AGENT_NOT_FOUND` | 404 | Agent row missing or owned by another user |
| `AGENT_FAILED` | 500 | Runtime errored during a call |
| `AGENT_BUSY` | 409 | Operation refused while another is in flight |
| `PROVIDER_NAME_TAKEN` | 409 | Conflict on `providers.name` |
| `PROVIDER_NOT_VISIBLE` | 400 | Agent config references a provider the user can't see |
| `PROVIDER_REFERENCED` | 409 | Delete blocked because agents still reference the row |
| `PROVIDER_TEST_FAILED` | 400 | `/providers/{id}/test` failed |
| `CHANNEL_KIND_UNSUPPORTED` | 400 | `kind` not in registered builders |
| `CHANNEL_INVALID_CREDENTIALS` | 400 | Channel config rejected by the platform |
| `CHANNEL_PROBE_INCOMPLETE` | 400 | Probe couldn't reach the platform |
| `CRON_TRIGGER_INVALID` | 400 | Trigger string did not parse |
| `CRON_PROMPT_INVALID` | 400 | Empty or too-long prompt |
| `SLASH_UNKNOWN` | 400 | `/<cmd>` is not a registered handler |
| `SLASH_BAD_ARGS` | 400 | Slash handler rejected its arguments |
| `ATTACHMENT_UNSUPPORTED_TYPE` | 400 | Chat / inbound attachment rejected (media type not allowed) |
| `ATTACHMENT_TOO_LARGE` | 413 | Chat / inbound attachment exceeds the configured size limit (`max_mb` in details) |
| `WORKSPACE_PATH_INVALID` | 400 | Path outside the agent's workspace |
| `STORAGE_BACKEND_UNREACHABLE` | 502 | Remote backend connect / list failed |
| `CONNECTOR_OAUTH_FAILED` | 400 | OAuth flow could not complete |
| `INTERNAL_ERROR` | 500 | Unhandled exception (logged with traceback) |
