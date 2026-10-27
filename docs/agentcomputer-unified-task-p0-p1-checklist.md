# AgentComputer 统一 Task：P0/P1 接入与验收清单

日期：2026-10-23。客户端核对基线：`feature/0.0.2/octp @ eb51d07`。服务端统一会话契约基于 `4cd685b7`，Bridge 发送前令牌过滤由 `34e918d5` 提供；项目级联删除与自动停止为其后的本次修订。准确交付提交和运行验证见资料包 `manifest.json`、`deployment-verification.json`。

项目删除修订已于 2026-10-23 20:10（Asia/Shanghai）从隔离工作树重启部署至本机 `127.0.0.1:8088`；该次启动时改动尚未提交或合并，运行来源仍是该工作树。部署验证见[统一接入文档](agentcomputer-unified-conversations-handoff.md)。此前 `34e918d5` 资料包不覆盖该新增行为，其他服务需单独升级。

这份清单是客户端实施要求，未勾选项不代表已完成。本轮只交付服务端补丁与文档，不修改或构建客户端。先完成 P0，再完成 P1；工作台、记忆、批迁、跨主机和语音等 P2 能力随后接入。完整字段和边界见[统一接入文档](agentcomputer-unified-conversations-handoff.md)，REST 模型以部署服务的 `/api/openapi.json` 为准。

正式链路：`OctopWorkspaceDataSource → OctopTaskSessionService → ConversationRunController → TaskConversationView`。由 `WorkspaceTaskRunCoordinator` 装配，macOS/iOS 入口为 `ProjectWorkspaceView`、`MobileWorkspaceView`。不要在旧 `OctopBoxTaskViewModel` UUID Box 路径增加这批功能。

## P0：保障现有使用

### 1. 新发送携带配置版本，网络重传保留原请求

- [ ] 新的 `task.message`、`task.retry` 与对应 REST 请求携带已读取的 `Task.configuration_revision`；有项目时携带已确认的 `Project.revision`，分别编码为 `expected_configuration_revision`、`expected_project_revision`。
- [ ] 首轮创建按对应 create DTO 编码；无项目时不附带项目版本。配置 PATCH 的 `expected_revision` 取 `Task.list_revision`，不能用专家版本或正文版本代替。
- [ ] 配置冲突时停止当前发送队列，刷新状态并保留草稿，让用户确认；不自动替换版本后发送。
- [ ] 网络重传沿用原 `request_id`、请求种类、Task/重试目标、正文、附件、options 和两个原版本。用户发起新一次执行才使用新的 `request_id`。

配置版本省略或 null 按 1 校验；项目版本省略或 null 不进行项目 CAS。因而旧客户端能继续向版本 1 的 Task 发送，但切换专家或模式后可能冲突。服务端不会自动采用最新配置。

已受理请求先做幂等匹配，再检查当前版本、忙碌和归档状态。相同 ID 改请求内容会返回 `TASK_REQUEST_CONFLICT`；客户端升级时也不能给未知提交的旧 body 自动补新版本。恢复观察不等于重新发送。`retry` 只用于最后一个 failed/cancelled 轮次；interrupted 依 `recovery` 走原轮恢复，不混用。

验收：设备 A 打开草稿，设备 B 切换专家/模式或修改项目，A 的新发送被正确拦截；已受理但丢失回执的原请求，在用户显式核对时返回已有受理结果，不新增 Turn。

### 2. 为正式会话新增持久发送意图

- [ ] `OctopTaskSessionService.Submission` 当前只在内存。为正式链路新增持久 pending/outbox，不能以旧 Box 存储代替。
- [ ] 在发出请求前保存账号/服务地址作用域、请求种类、Task/重试目标、原 `request_id`、完整原始请求体及受理状态。未来接远程时进一步增加 host 范围。
- [ ] 收到受理回执后保存服务端 Task/Turn 对应关系；App 强退或切后台后仍能恢复同一意图，不能悄悄生成新 ID。
- [ ] 重启后先恢复权威状态与观察。结果未知时保留待核对项，不自动作为新工作发送；用户显式重传仍使用原体。
- [ ] 退出账号、切 endpoint 或过期连接的迟到回执不能更新新作用域的队列与草稿。

验收：落盘前终止 App，要求请求尚未发出、服务端无副作用；落盘后分别在请求发出、服务端受理但回执丢失、回执已保存时终止 App，要求重开后恢复原 request_id 和原请求体，已受理请求不重复执行，未确认状态可见。

### 3. DTO、正文投影、缓存和历史权限一起升级

- [ ] 摘要/详情保留 `selected_expert_id`、`configuration_revision`、`action_mode`、`pinned` 及 `configuration_events`。
- [ ] Turn 保留本轮实际专家名称/版本、行动模式、两类目录和项目版本；历史显示冻结快照，不用输入框当前选择回填。
- [ ] 保留 `history_only`、全部来源 ID、`source_status`、`legacy_attachments`、`source_trajectory`；缺失附件显示不可用。
- [ ] 内容块保留 `speaker_agent_id`、`speaker_name`；团队成员输出不平铺丢失身份。正文、计划、交互继续按各自版本替换完整快照。
- [ ] `OctopWorkspaceCache` 与 `OctopTaskBodyCache` 同时升级；旧缓存缺少版本时先刷新权威状态再开放新发送。正文事件、历史分页和列表同步游标分别保存。
- [ ] `history_only` 轮次不显示旧执行的 retry/resume；已完成历史仍显示 fork/edit/regenerate，并在当前 Task 直接创建新分支，不要求跳回原 Task。其工具只展示保存的公开结果，不请求 ToolHost。Task 本身仍可接受正常的新消息。

验收：新旧缓存、含团队成员的历史、分支复制的只读轮次和缺失附件均能重开；历史不会变成可执行检查点，成员身份不会在投影或缓存后丢失。

### 4. 全部会话列表与 sync 同步改造

- [ ] `GET /api/project-tasks?scope=all` 和 `/api/project-tasks/sync?scope=all` 同时接入。
- [ ] 给列表增加明确范围，不把现有 `projectID=nil` 的“无项目”缓存直接解释为“全部”。修改 `OctopWorkspaceClient` 的 projectID 响应校验、缓存键、去重和分页。
- [ ] 全部/无项目/单项目、归档和筛选范围分别保存列表状态。范围改变后重建对应游标，不跨筛选复用。
- [ ] 全部列表不要再拼接各项目列表；Task 详情和草稿继续按 Task 身份管理。

验收：全部列表同时包含有项目与无项目 Task；翻页、增量更新、归档移出、重开缓存均无遗漏或重复；单项目页仍只显示本项目记录。

### 5. 项目删除与归档

- [ ] 删除确认明确“停止并永久删除项目及全部任务/历史附件，无法恢复；工作目录文件保留”；不再限制为仅删除空项目。
- [ ] 项目有活动任务时仍可提交 DELETE，由服务端停止并确认退出后删除；提交期间显示处理中。`PROJECT_TASKS_BUSY` 表示停止尚未确认，保留记录并允许稍后重试同一 DELETE，不能先从列表移除。
- [ ] 仅在收到无正文的 204 后移除项目及其全部 Task，并清除对应待发队列；删除请求无 body/版本参数。单 Task 删除的 `TASK_BUSY` 限制保持。
- [ ] 接入 `PATCH /api/projects/{project_id}/archive`，body 为 `{archived,expected_revision}`；恢复用 archived=false。
- [ ] 项目与 Task 的归档状态分别展示和管理；项目恢复不自动恢复已归档 Task。

验收：空项目及有历史/归档 Task 的项目均可永久删除；活动任务确认停止后才返回 204，停止未确认时 409 且全部记录保留、可重试。归档仍要求先停止活动任务，恢复及版本冲突处理正确；项目目录文件和专家记忆不被删除。

## P1：统一会话核心功能

### 6. 可选专家、共享普通专家、本人团队与行动模式

- [ ] 从当前主机 `/api/project-tasks/resources` 读取 experts，提供独立“通用助手”选项；不创建隐含专家。
- [ ] 创建支持 `selected_expert_id` 和 `action_mode=ask|plan|craft`；省略/null 专家表示通用助手。
- [ ] 空闲切换走 Task PATCH，同时带 list/configuration 版本。省略专家表示保持，明确 null 表示切回通用助手；Swift 编码须支持三态。
- [ ] 有活动轮次、待发队列或未知提交时禁用切换；冲突保留原草稿。
- [ ] 区分本人专家、共享普通专家、本人团队；共享团队暂不开放。逐轮实际身份、模式、成员 speaker 和模型来自服务端历史。
- [ ] ask/plan/craft 独立于账号审批策略和结构化计划；不复用审批模式字段保存行动模式。

验收：无/有项目 × 通用/专家四组合均完成首轮和追问；共享专家与本人团队分别完成多轮。切换后旧轮身份不变，新轮采用确认后的配置，执行目录始终跟项目/Task 走。共享撤权或专家不可用时明确阻止，不静默换成通用助手。

### 7. 筛选、置顶和归档管理

- [ ] 服务端列表与 sync 同时携带 `expert_id`、`status`、`q`、`archived`；不能只搜索已加载的摘要。
- [ ] 置顶、改名和 Task 归档走现有 Task PATCH，带正确 list revision；归档恢复后重新校对筛选范围。
- [ ] 筛选/分页/缓存使用相同条件，切换条件不沿用旧同步游标。

验收：未加载分页内的 Task 也可被服务端关键词检索找到；专家/状态筛选正确，置顶排序与跨设备更新一致，归档与恢复不丢正文。

### 8. 分支、编辑与重新生成

- [ ] 对接 `POST /api/project-tasks/{task_id}/forks`，operation 为 fork/edit/regenerate，使用新业务 request_id。
- [ ] 来源 Task 必须空闲，锚点必须 completed，允许 history_only；edit 发送新 prompt，regenerate 不传 prompt。
- [ ] 返回 `202 TaskDetail` 后导航到新的 task_id，保留原分支。普通 fork 可没有新执行；不能因 202 假定已经在生成。
- [ ] 目标项目/专家省略表示继承，显式 null 表示清空；请求不附加未声明的 options 或 expected_revision。

验收：三种操作分别覆盖普通回答和分支继承的历史回答，检查来源关系、公开历史、当前导航与后续追问；原 Task 不改写，复制历史不携带检查点、审批授权或工具执行状态，不能 retry/resume 旧执行；失败不会自动再创建另一条分支。

## 客户端交付证据

P0、P1 分别反馈每项“完成/未完成/受阻”、实际修改文件、定向测试及设备流程结果。至少覆盖升级旧缓存、强退恢复、双设备配置竞争、分页增量、只读历史、共享撤权、团队身份和分支导航。服务端自动化与本机部署通过不能替代这些客户端证据。

未接的 P2 能力按能力与入口明确禁用，不伪装已可用。继续保留现有唯一 WS receiver、Resident 独立会话与本机工具授权；不增加旧协议写入兼容层。
