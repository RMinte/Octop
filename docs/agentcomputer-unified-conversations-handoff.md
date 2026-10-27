# AgentComputer 统一会话接入交接

更新日期：2026-10-27。统一 Task 初始接口参考基线：`feature/agentcomputer-foundation @ 4cd685b7`，Bridge 安全修复已由 `34e918d5` 提供；2026-10-23 后续修订恢复项目及全部 Task 的永久删除，并在服务端先停止活动轮次。该次交付提交及重启验证记录见资料包的 `manifest.json` 与 `deployment-verification.json`。APP 代码切点的只读核对基线：`feature/0.0.2/octp @ eb51d07`；客户端 Swift/UI 未改动。接口已实现不等于其他运行服务已升级，也不代表真实用户旧会话已迁移。

此前部署记录（2026-10-23）：Bridge 修复已在本机部署；项目删除修订于 2026-10-23 20:10（Asia/Shanghai）从当时工作树重启至本机 `127.0.0.1:8088`，进程为 `27383`。该次启动使用基于 `34e918d5` 的工作树源码，启动时改动尚未提交、未合入目标分支；运行来源为该隔离工作树，不能只用启动时的 HEAD 判断当时行为。健康、在线文档、新删除接口 OpenAPI、数据库完整性及原有记录校验通过，重启前后仍为 17 个项目、181 个 Task、785 个 Turn。验证记录：`/private/tmp/octop-project-deletion-restart-20261023T121038Z/result.json`。此前 `34e918d5` 资料包不包含这项后续修订，其他服务需单独升级。

历史部署记录（2026-10-26）：该次修订允许继承的已完成历史直接 fork/edit/regenerate，网页不再因 `history_only` 隐藏这些入口，APP 仍需接线。请求、执行边界及验收见[继承历史再次分支说明](agentcomputer-history-branching.md)。本机 8088 于当日 21:55（Asia/Shanghai）从 `release/1.0.0 @ 36500aa2` 加当时未提交的历史分支修改平滑重启，当时 PID 为 `53795`；该次健康、在线契约、网页构建和原记录一致性核验通过，数据库版本保持 `57`。历史运行记录为 `.runtime/8088/history-branching-latest.json`。这些信息仅描述当时状态，不代表当前进程或运行版本；2026-10-27 执行引擎部署另见[部署与提交范围](task-execution-engines.md#本机服务部署与提交范围)，本次文档整理未重新部署服务，其他实例需单独升级。

本机 `127.0.0.1:8088` 首次部署 `4cd685b7` 时已于 2026-10-23 将数据库结构从 v55 升至 v56；健康检查、在线文档及新接口 OpenAPI 检查通过。原有 181 个 Task、785 个 Turn 和其他核心记录逐表校验一致。后续 Bridge 安全修复的运行版本与复验结果已附 `34e918d5` 资料包；结构升级不代表旧 thread 已执行批量迁移。APP 新契约接线与设备验收仍未完成。

当前 REST 与 WebSocket 契约以服务端实现、`GET /api/openapi.json` 和[任务 WS Schema](project-task-websocket.schema.json)为准；可在 `/api/docs` 查看交互式 REST 文档。本文说明已实现接口、客户端接线和明确未开放的边界；本机已有 Task 跨主机转交仍为 proposal。不恢复 `/api/agentcomputer/v1`。

Bridge WS 的 query 鉴权过滤由 `34e918d5` 交付：发送桥接 open 帧前移除本机 token/access_token 及非白名单参数，接收端过滤继续保留；不改变公开接口。仅部署 `4cd685b7` 不包含这项发送前过滤，不能用协议号 3 判断修复是否上线。原生客户端继续优先使用 Bearer，无法设置请求头时使用 authenticate 首帧；令牌不应放入 Bridge URL。三类远端 WS 的回归分别检查出站参数隔离、合法参数保留及对端使用自身鉴权。

## 0. 客户端先核对这几项

1. 用当前账号 Bearer 令牌请求 `GET /api/project-tasks/capabilities`，核对 `contract="unified-task-conversations"`、`version=1`、`protocol_version=3`。再读取 `/resources` 和需要的项目列表；协议号 3 本身不能证明所有新增能力已经部署。
2. 普通会话统一使用 Task。以 `scope=all` 读取全部会话；项目和专家均可选。通用助手用 `selected_expert_id=null`，不能为它偷偷创建专家。
3. 首条消息创建 Task，保存返回的 `task_id` 和原始发送体；追问沿用该 Task。工作台、附件、记忆、草稿与队列均跟随 Task，跨主机再加 host 范围。
4. 建立唯一 WS receiver，认证成功后再提交或订阅。按命令 `id` 匹配回执，允许 `approval.policy`、正文快照和工具通知与回执交错；断线只恢复观察。
5. 所有 ID 原样保存。业务 `request_id` 和 WS 命令 `id` 都是 8–128 位 `[A-Za-z0-9_-]`；业务重传保留原 request_id、请求体与版本。请求 DTO 禁止额外字段；响应 DTO 应兼容新增字段。
6. 先接通用/本人专家的多轮闭环，再接团队、工作台和远端 Task。是否展示能力同时取决于部署 capability、当前账号资源和每个 Task 的 workbench capability，不能只按客户端版本判断。

部署 capabilities 只有 tasks/projects/task_ws/files/browser/terminal/recording/tool_ui/memory/migrations；没有 teams 或 voice 标志。团队从同一 host 的 resources.experts[].kind/member_ids 发现，语音另读账号级配置。

本文表格列的是关键字段，不能作为完整响应 DTO 替代 JSON Schema/OpenAPI。Task 时间通常为 UTC ISO 8601；迁移批次 created_at/updated_at、远端引用 observed_at 为 epoch 毫秒，cron.last_run_at 为 epoch 秒，显示时统一使用服务端时区。

## 1. 本次接入范围

| 能力 | 服务端现状 | APP 接入要求 |
| --- | --- | --- |
| 普通会话、项目任务 | 同一 Task、多轮、可无项目 | 继续使用原 Task 页面和服务，统一列表入口。 |
| 通用助手、本人专家、本人团队、共享普通专家 | 统一 Task 已支持 | 创建时选专家，空闲时显式更换；不另建专家 thread。 |
| 计划、思考、审批、问题、暂停恢复、客户端工具、附件、产物 | 原 Task 协议继续有效 | 复用现有实现，补版本、来源及身份字段。 |
| 专家记忆、Task 文件/浏览器/终端/录制/工具结果 | Task 范围 REST/WS 已实现，网页已接入 | APP 按能力发现逐项接入；未接 UI 不代表服务端无接口。 |
| 普通本地旧网页会话续接 | 显式 `from-thread` 已实现 | 用户确认目标项目后接入，原历史保持可读。 |
| 远程 Bridge 专家 | Task REST/WS 及工作台桥接、TaskRef 索引和能力协商已实现 | 按第 11 节接线；对端不支持时阻止统一会话新写入，不降级旧 chat。 |
| 私人常驻智能体 Resident | 保留独立身份、会话与记忆 | 同一 socket 的 `resident.*` 与 Task 订阅并存，不转成可选普通专家。 |
| 旧会话批量迁移 | 预检、幂等批次、暂停恢复、自动开关及旧链接解析已实现 | 按第 11.4 节展示进度和不可用附件；迁移不调用模型或执行工具。 |
| IM、CLI、cron | 入口适配独立于 APP，参见统一入口实施计划 | 不在 APP 另建旧 thread 写入路径；保留 Resident 专用协议。 |

旧文档中“项目准备时创建默认专家”、`/api/agentcomputer/v1` 或“团队/共享/工作台尚未接入 Task”的阶段描述，不应用于本轮新接线。Resident 的独立协议仍按[共用正文流交接](agentcomputer-conversation-stream-handoff.md)执行。

## 2. 客户端对象和缓存身份

Task 就是普通工作会话。首条消息创建 `task_id`，追问沿用该 ID；不把每条消息建成一个 Task，也不因换专家重建 Task。`turn_id` 定位一轮执行，`request_id` 定位一次持久受理意图。

项目持有 `directory`、`client_directory`、行动指南、Skill/MCP/插件等选择；专家持有人设、默认能力配置和长期记忆。创建或修改项目不会创建、绑定或修改专家。所选专家不决定本次项目工作目录。

APP 现阶段继续用已有的部署地址、登录账号和连接代次隔离缓存。在该作用域内，以 `task_id` 保存详情、草稿、发送队列、订阅游标及工作台状态，不能以 `expert_id` 作为会话主键。退出账号或更换服务地址后，旧 socket、旧请求回执和异步媒体结果不得写入新作用域。

跨主机使用 `TaskRef(host_id, task_id)`，见第 11 节。当前远端 TaskSummary/TaskDetail 仍保留远端原始 ID；客户端按所选 transport 附加 host 范围，Bridge 已观察引用接口另返回 host_id。不能要求普通 Task DTO 自带本机 host 字段。

## 3. 需要新增的 DTO 字段

| 对象 | 当前字段 | APP 展示与持久化 |
| --- | --- | --- |
| `TaskResources.experts[]` | `expert_id,name,description,config_revision,kind,is_shared,is_owner,member_ids` | `kind=expert\|team`；使用服务端完整 ID，列表之外另提供通用助手选项。 |
| Task 摘要/详情 | `selected_expert_id,configuration_revision,action_mode,pinned` | `selected_expert_id=null` 为通用助手；`action_mode=ask\|plan\|craft`。 |
| `TaskDetail.configuration_events[]` | `event_id,previous_expert_id,selected_expert_id,selected_expert_name,previous_action_mode,action_mode,configuration_revision,created_at` | 展示配置变化；不能改写过去轮次的身份。 |
| `TaskTurn` | `selected_expert_id,expert_name,expert_config_revision,action_mode,server_directory,client_directory,project_revision` | 本轮实际身份和目录快照。 |
| `TaskTurn` 来源 | `history_only,source_task_id,source_turn_id,source_thread_id,source_agent_id,source_status,legacy_attachments` | 标记分支/旧历史的只读副本及原资源链接。 |
| `TaskTurn.source_trajectory[]` | `source_event_id,source_sequence,source_turn_id,source_agent_id,kind,created_at,summary,payload,speaker_agent_id,speaker_name,source_status,mapping,history_only,restorable` | 只读公开来源轨迹；history_only 恒 true、restorable 恒 false，不能作为工具回执或检查点恢复。 |
| `content_blocks[]` | `speaker_agent_id,speaker_name` | 团队成员公开输出的真实来源，与主持人的普通 reply 分开。 |
| `TaskTurn` 模型 | `model_ref,routing,options` | 展示本轮实际模型、路由决定及冻结选项，不能使用输入框当前选择回填历史。 |

已存在的 `import_source/external_session_id/imported_history` 用于外部记录导入，继续按原契约解码；它们不等于本轮新增的旧专家 thread 续接来源字段。不能把 Codex/Claude 导入来源覆盖成所选专家。

旧 APP 缓存可缺少新增字段，但缺失与明确 `null` 必须区分。恢复旧缓存后先刷新 Task/资源目录，取得真实配置版本，再开放切专家或发队列；不能把本地猜测的 `configuration_revision=1` 当成已读服务端版本。WS 仍为协议 3，不能仅凭协议号断定服务已部署所有业务扩展。

## 4. 已实现的 REST 接线

REST 使用 Octop 登录令牌 `Authorization: Bearer <access_token>`；JSON 请求使用 `Content-Type: application/json`。示例 ID、模型和版本均为占位值。

### 4.1 列表和资源

| 目的 | 当前请求 |
| --- | --- |
| 模型、可选专家、路由与审批能力 | `GET /api/project-tasks/resources` |
| 项目、项目资源 | `GET /api/projects`、`GET /api/projects/resources` |
| 全部会话第一页 | `GET /api/project-tasks?scope=all` |
| 全部会话增量 | `GET /api/project-tasks/sync?scope=all&sync_cursor=...` |
| 筛选 | 全局列表及对应 sync 支持 `expert_id`、`status`、`q`，另有 `archived`。 |
| 仅指定项目 | `GET /api/projects/{project_id}/tasks` 及对应 `/tasks/sync` |
| 打开详情/较早轮次 | `GET /api/project-tasks/{task_id}`，历史续页使用其 `next_page_cursor`。 |

省略 `scope` 的全局列表仍只返回无项目 Task。不要同时拼接“全部项目列表”和 `scope=all` 造成重复。历史分页游标、列表增量游标、Task 正文 `event_cursor` 分别保存；筛选、归档或作用域改变后不能复用旧列表游标。完整规则见[增量同步设计](project-task-incremental-sync.md)。

### 4.2 首条消息创建

```http
POST /api/project-tasks
```

```json
{
  "request_id": "app-create-000001",
  "project_id": "proj_example",
  "selected_expert_id": "expert_example",
  "action_mode": "craft",
  "expected_project_revision": 7,
  "prompt": "检查当前项目并说明需要修改的部分。",
  "web_search_enabled": false,
  "options": {"model_ref": "provider/model"}
}
```

`project_id:null` 为无项目；`selected_expert_id:null` 或省略为通用助手。也可使用 `POST /api/projects/{project_id}/tasks`，此时 body 不包含 `project_id`。成功均返回 `202 TaskDetail`；保存响应的 Task ID 后继续同一会话。项目创建本身仍走 `POST /api/projects` 与 `Idempotency-Key` 请求头，不能套用消息的 request_id 代替项目幂等头。

`ask/plan` 在服务端限制写工具，`craft` 按审批规则执行；行动模式与审批模式、模型思考开关是不同设置。创建中的旧 `approval_mode` 字段已忽略，新任务继承账号审批策略，不能靠创建 body 请求更高权限。

### 4.3 追问、重试与切专家

```http
POST /api/project-tasks/task_example/messages
```

```json
{
  "request_id": "app-message-00002",
  "prompt": "继续处理第一个问题。",
  "expected_configuration_revision": 3,
  "expected_project_revision": 7,
  "options": {"model_ref": "provider/model"}
}
```

`expected_configuration_revision` 省略或 null 按 1 校验，不是自动采用最新配置；`expected_project_revision` 省略或 null 则不进行项目 CAS。客户端应发送实际读到的版本，避免陈旧草稿跨配置执行。

因此旧客户端在 Task 的 configuration_revision 仍为 1 时可以继续发送；切换专家或行动模式后版本增加，省略配置版本的新发送才会被拒绝。该冲突保护不能通过服务端自动采用最新版本来绕过。已受理的相同 request_id 会先校验原始请求内容并返回原受理结果，早于当前配置版本、忙碌和归档检查；客户端升级时不能为未知是否已受理的旧 pending body 补上新版本后沿用旧 ID，否则返回 TASK_REQUEST_CONFLICT。

重试仅允许当前最后一个 failed/cancelled 轮次，使用 `POST /api/project-tasks/{task_id}/turns/{turn_id}/retry`，提交新的 request_id、当时确认的两个版本及 options。它继承原消息和附件，按当前配置创建新轮，工具可能再次执行；与 interrupted 原轮的断点恢复 `/resume` 不同。结果未知或 `needs_reconciliation=true` 时不能盲重试。

```http
PATCH /api/project-tasks/task_example
```

```json
{
  "expected_revision": 12,
  "expected_configuration_revision": 3,
  "selected_expert_id": null,
  "action_mode": "ask"
}
```

这里 `expected_revision` 取 Task 的 `list_revision`。省略 selected_expert_id 为保持不变，明确 null 才是切回通用助手；Swift 编码器需要能表达“省略/显式 null/有值”三态。`pinned`、`title`、`archived` 等编辑也走同一 PATCH；这些字段及 action_mode/web_search_enabled 显式 null 返回 422，不能照搬专家字段的清空语义。

专家/行动模式只可在服务端空闲时更换。APP 有未确认提交、待发送队列或活动/等待审批轮次时禁用切换；提交前服务端仍会校验。409 `TASK_REVISION_CONFLICT`、`PROJECT_REVISION_CONFLICT` 或 `TASK_BUSY` 应刷新后让用户确认原草稿，不能刷新版本后自动沿用旧意图重发。网络重传必须保留原 request_id、原始选项及原版本；修改内容应形成新的明确发送意图。

### 4.4 分支、编辑、重新生成

`POST /api/project-tasks/{task_id}/forks` 接受：

```json
{
  "request_id": "app-fork-0000001",
  "operation": "edit",
  "turn_id": "turn_example",
  "prompt": "这是编辑后的用户消息。"
}
```

`fork` 复制包含锚点的公开历史；`edit/regenerate` 从锚点前建立分支，regenerate 沿用原用户消息且不传 prompt。省略目标项目/专家表示继承，显式 null 表示清空。带新消息的 fork 或 edit/regenerate 会受理新轮；普通无 prompt 的 fork 返回空闲分支。APP 导航到返回的新 task_id，保留原分支。

分支名称直接使用返回的 `title`：服务端生成 `原标题 (2)`、`原标题 (3)`，跳过同账号（包含其他项目及归档）已占用的标题，总长不超过 128 字符。自动命名分支再次分支时不叠加后缀；手动改名保留用户文字。相同请求重放沿用已创建分支的名称，APP 无需自行编号，既有 Task 名称不批量改写。

来源 Task 必须空闲；锚点必须 completed，允许 `history_only=true` 的历史轮次，省略 turn_id 取最后一轮。成功始终返回 `202 TaskDetail`，即使分支暂时没有新执行。此请求不接受 options 或 expected_revision，后续执行使用分支当前配置。

历史副本 `history_only=true` 不携带可执行检查点、审批授权或工具执行状态，不能 retry/resume 旧执行；已完成的副本可以作为 fork/edit/regenerate 锚点，在新 Task 中按当前配置开始新工作，也可以正常发送后续新轮。APP 应保留这些分支按钮，不要求回到原 Task 操作。工作目录文件不会因分支自动复制。

## 5. 项目、目录和版本边界

两类路径独立：`Project.directory` 是 Octop 服务主机的工作目录，服务端校验；`client_directory` 是用户设备上的绝对路径，服务端只保存，不探测、不读取、不上传。APP 本地工具继续通过明确的 `client_id/client_tools` 和客户端授权执行，不能因为项目保存了路径就自动授予本机能力。

无项目或项目未配置服务端目录时，Task 使用自己的工作空间。专家和团队成员均跟随此 Task 的项目目录，不转到专家原 workspace。运行中使用本轮冻结目录，空闲后新轮采用当前项目配置；APP 应清楚展示两者差异。

| 版本/游标 | 作用 | 不能混用为 |
| --- | --- | --- |
| `Project.revision` | 项目配置 CAS，本轮 `project_revision` 快照 | Task 配置版本。 |
| `list_revision` | 摘要更新与 Task PATCH | 正文、计划或审批版本。 |
| `configuration_revision` | 所选专家/行动模式和草稿防陈旧 | 专家人设版本。 |
| `expert_config_revision` / 目录 `config_revision` | 本轮专家配置快照/当前可选资源版本 | Task PATCH 的 expected_revision。 |
| `content_revision/plan_revision/interaction_revision` | 本轮各组完整数据的独立版本 | 列表同步位置。 |
| `scope_revision` | 当前工作台目录、配置、权限的不透明范围标识 | 可比较大小的数字或可长期授权的 token。 |

项目 DELETE 会永久删除项目及其中全部 Task、历史附件和公开记录；服务端先持久阻止新执行，再请求停止并确认活动轮次退出。停止尚未确认或超时返回 `PROJECT_TASKS_BUSY`，保留全部数据，可重试同一 DELETE；成功返回 204，无请求体或版本参数。确认框应明确“停止并永久删除项目及全部任务/历史附件，工作目录文件保留”，不能再显示仅空项目可删。归档仍保留历史，并在有活动任务时拒绝；单 Task 删除也仍受 `TASK_BUSY` 保护。专家被当前或历史记录引用时仍返回 `EXPERT_IN_USE`，项目删除不清空专家记忆。此项目删除行为是 `34e918d5` 之后的修订，不能以旧部署的空项目限制推断新版本行为。

## 6. 已实现的 WS、合并与重连

主连接仍为 `/api/project-tasks/ws`，`ready.protocol_version=3`。原生客户端在升级请求添加 Bearer 头；无法加头的浏览器在 10 秒内首帧发送 `authenticate`。两种方式只用其一，且这个主 socket 不读取 URL token 参数。

```json
{"type":"task.create","id":"command-create-01","data":{"request_id":"app-create-000001","project_id":null,"selected_expert_id":null,"action_mode":"craft","prompt":"你好"}}
```

```json
{"type":"task.message","id":"command-message-1","task_id":"task_example","data":{"request_id":"app-message-00002","prompt":"继续","expected_configuration_revision":3,"expected_project_revision":7}}
```

`task.create/message/retry` 的 data 复用对应 REST 请求模型。配置 PATCH、fork、from-thread 和专家记忆管理目前使用 REST；**没有新增 `task.update/task.fork/task.migrate` WS 命令**。

创建后不会自动订阅。明确进入已有详情时发送 `task.open`，用返回 `event_cursor` 发 `task.subscribe`。重连仅重新认证、恢复订阅；不用 task.open 代替恢复，更不能因为断线重发消息。socket 命令 id 只关联本次回执，重传时可换；持久 request_id 保持原值。

单个 receiver 处理 `result/error/snapshot/turn/session/heartbeat/approval.policy`，声明客户端工具后还接收 `tool.call`，本机 Resident 观察另接收 `resident.snapshot`。全局 approval.policy 无需订阅就可能推送；任何异步事件都可能与命令回执交错。`snapshot.data` 是 TaskDetail；`turn.data` 是 `{task,turn,cursor}`。正文为累计状态，不能逐帧追加。内容块、计划、交互按各自版本替换完整数组，摘要按 list_revision；应用事件后再保存 cursor。历史分页不推进实时游标。

订阅成功的 `result.data=null`，不能把它当成详情。遇到 `PROJECT_SYNC_RESET`，只清理对应 Task 的实时 cursor，重新发送不带 cursor 的 task.subscribe 取得 snapshot；不清其他订阅，不重发消息，也不因恢复观察触发 task.open。

状态完整枚举为 queued/running/waiting_approval/waiting_answer/waiting_client_tool/paused/interrupted/completed/failed/cancelled。waiting、paused、interrupted 不表示成功结束；pause_requested 只是请求意图，status=paused 才表示已安全暂停。

没有单独的 done 消息；以 turn.status=completed 判定完成，连接继续承载后续轮次。

`session.access_token` 是滑动续期，按账号及连接代次保存；heartbeat 不需回复。Task 和 Resident 合计最多 32 个订阅。一个任务的失权/失效只移除该观察，不清空其他 scope；断线、切页、后台或 unsubscribe 均不取消执行。

审批/问题继续复用 `task.interaction.respond`，携带 turn_id、interaction_id，以及 data 中原 interaction revision、request_id 和精确回应；审批还核对 action_digest。暂停/继续沿用 `task.pause/resume`；停止沿用 `task.cancel` 指定轮次。`client.attach`、`tool.get/claim/review/result` 保持原客户端工具协议。APP 的本机执行权限不能由手机或远端审批模式自动提升。完整报文见[交互交接](agentcomputer-task-interactions.md)和[主 WS 文档](project-task-websocket.md)。

respond.expected_revision 取单项 interaction.revision，不能取轮次 interaction_revision。pause/resume 的 data 严格为 `{request_id,turn_id}`，不带 expected_revision；cancel 的 data 只有 `{turn_id}`。resume 不替代审批，也不伪造客户端工具结果。

全局审批策略通过 `GET/PUT /api/project-tasks/approval-policy` 读写，PUT body 为 `{request_id,expected_revision,mode}`，mode 为 ask_for_approval/approve_for_me/full_access；expected_revision 取账号级 approval_policy.revision。也可发 `task.approval.set`，其 data 复用上述 body，task_id 指向本人 Task。账号全部 Task 共用该策略，不能用行动模式 ask/plan/craft 或 Task.list_revision 替代它。

## 7. 实际专家、模型与团队来源

发送框选择的是下一轮意图。历史显示使用 TaskTurn 的 expert_name、expert_config_revision、action_mode 和 model_ref；团队成员内容使用 speaker 字段，不能全部标为主持人，也不能从当前选中的专家名称反推旧轮。

`options.model_ref` 使用资源目录的完整值，每轮显式选择优先于 routing。省略模型且省略 routing 时，按本轮专家默认模型、服务端默认模型的顺序解析，不继承上一轮；解析出的引用不可用时返回错误，不静默换模型。混合路由的实际结果取 `turn.routing`；恢复同轮使用冻结结果，手动 retry 是新轮选择。不要把路由中的 `target=remote` 解释成“远程专家”：这是模型服务商路由，与第 11 节跨 Octop 主机执行不同。详见[模型路由接入](project-task-model-routing.md)。

团队 roster 和各成员人设、能力、记忆按轮冻结；调用时继续核对授权。共享普通专家可以选择，其他人的私人记忆/legacy/凭据不会因此开放；共享团队目前不支持。可选项以当前账号 resources.experts 为准，不从旧 experts 页面自行拼出可执行权限。

## 8. 旧记录续接与来源映射

显式单条续接使用 `POST /api/project-tasks/from-thread`；后台批量迁移使用第 11.4 节的独立接口。两者不能混作同一个幂等操作：

```json
{
  "request_id": "app-continue-0001",
  "agent_id": "expert_example",
  "thread_id": "thread_example",
  "project_id": "proj_example"
}
```

project_id 必填，可明确传 null；省略 selected_expert_id 继承来源专家，显式 null 切回通用助手。成功返回 `201 TaskDetail`，没有模型执行；用户下一次发消息才开始新轮。只支持当前用户所有的本地 dashboard 来源 thread，来源专家必须已加载且可用；停止的来源专家可能返回 AGENT_NOT_RUNNING，不会自动启动。包括普通、已授权共享专家及本人团队。活动执行、待审批/中断或不可读来源拒绝，不能通过复制绕过。

服务端保存幂等接入映射和来源摘要；同 request_id、同原请求重发返回同一结果，不持续同步旧 thread。APP 保存新 task_id 及返回的 source_*，原旧会话仍可访问，不删除原记录、不修改原文件。`legacy_attachments` 使用原鉴权链接及 available 标记，不当成已拷入项目的文件；不可用时明确显示缺失。

| 来源 | 当前处理 | 客户端要求 |
| --- | --- | --- |
| 当前 Task | 原 ID 继续；不需要迁移 | 保留所有请求、轮次、附件、审批和游标身份。 |
| 本地旧 dashboard thread | 显式 from-thread 或后台批迁 | 先解析旧链接；不重复创建副本，不把显式续接当成批迁进度。 |
| 普通 IM/CLI/cron 旧 thread | 已支持后台批迁；from-thread 不支持 | 使用第 11.4 节，不能伪装 channel_type=dashboard。 |
| Bridge 远端旧 thread | 本机迁移列为 remote_adapter_required；Bridge 未开放迁移白名单 | 禁用跨桥迁移/续接；需直接连接来源主机管理其本地迁移。 |
| Resident、运行中或有未知工具结果的来源 | 保留专属执行/恢复 | 不自动启动第二执行器，不把未决授权复制成可执行 Task。 |

批迁已提供预检、幂等创建、进度、暂停/恢复和自动排队开关，没有 rollback/cancel 迁移 API。原历史不删除；迁移不运行模型、不重放工具或审批。APP 升级只更新客户端，不等于服务端迁移完成；以服务端来源映射及逐条结果为准。

## 9. 媒体、记忆和工作台

### 9.1 输入附件、产物与语音

输入上传继续使用 `/api/project-tasks/attachments`，取得 attachment_id 后放入创建/消息的 attachment_ids。原始附件与 Task 产物分开保存；详情/流只含元数据，预览下载走返回的鉴权 URL。新历史来源附件另看 legacy_attachments。所有返回链接按对应部署解析，不能拼到另一台主机，也不能把附件路径当作 client_directory 下的文件。详见[输入附件](project-task-input-attachments.md)和[成果库](project-task-artifact-library.md)。

网页 Task 已复用账号级 STT/TTS；APP 可复用自身已有录音/朗读体验，识别文本进入该 Task 草稿，再通过统一消息链路提交。语音配置不从 experts/models 主模型列表推断。文本、媒体和工具结果仍受各自鉴权与范围约束。

| 语音操作 | 接口与返回 |
| --- | --- |
| 当前配置 | `GET /api/voice/active` → `{stt,tts}`；配置发现另见 `/api/voice/providers`、`/api/voice/presets`。 |
| 识别 | `POST /api/voice/stt`，multipart audio 必填、language 默认 zh-CN、provider 可选 → `{text,confidence}`。 |
| 朗读 | `POST /api/voice/tts`，JSON `{text,provider?,voice_id?,speed?}`，text 1–8000 字符、speed 0.5–2 → 音频字节流，按 Content-Type 解码，不按 JSON/SSE 解码。 |

语音是账号级服务，没有 Task ID，也未开放在 Bridge Task HTTP 白名单内；远端 Task 的识别文本仍通过当前 Task transport 发送。

工作台图片/PDF 预览已完成真实页面验证，语音输入/朗读已接入但真实 STT/TTS 供应商尚待验收；会话内音视频附件和产物已接入鉴权 Blob 内嵌播放，播放器离开页面释放对象 URL。完整公开会话/轨迹导出、无专家 Task 独立记忆管理、提示词润色现已接线；它们的具体协议见下文，不能将这些 HTTP/UI 定向验证视为 APP 真机或真实音视频编解码验收。用户可见时间使用服务端 `GET /api/settings/timezone` 的时区，不以设备默认时区改写历史显示。

### 9.2 所选专家的记忆

`GET /api/project-tasks/{task_id}/expert-memory?source=scope|general|legacy&q=...` 返回 expert_id、source、entries、enabled 和 extraction_jobs。entries 包含 path/content/layer，以及可空 occurred_at/source_id。APP 显示本项目/本任务、专家通用、旧记忆三个范围；不向用户暴露内部 namespace/storage 配置。

显式保存使用同路径 POST，body 为 `{request_id,source:scope|general,content,topic?,expected_expert_id}`。响应 status=completed 可重放同请求；uncertain 提示核对，不能自动换 request_id 重存。legacy 只读、默认不召回。

策略 `GET/PATCH .../expert-memory/policy` 返回 expert_id、expert_name、config_revision、enabled、capture_enabled、legacy_recall_enabled、auto_extract_enabled、general_promotion_enabled、policy_editable。PATCH 必填 expected_revision 和 legacy_recall_enabled，建议同时传 expected_expert_id 防止切专家竞态；capture_enabled/auto_extract_enabled/general_promotion_enabled 可选。显示“会影响此专家以后会话”，共享使用者 policy_editable=false 时禁用全局策略编辑。单轮 `options.expert_memory_enabled=false` 可禁用该轮专家记忆，不等于关闭 Task 独立记忆。

后台抽取状态 pending/running/completed/cancelled/failed/uncertain、阶段 queued/extracting/ready/applying/done 是已保存状态，不是让 APP 自己执行抽取。general 自动晋升仅限有可核对原句且明确跨项目的用户偏好；项目事实仍 scope，团队委派不能伪装用户原句。未选专家时隐藏这个面板；无专家 Task 另使用独立记忆接口，不与专家通用记忆混用。

### 9.3 Task 工作台与范围发现

先读 `GET /api/project-tasks/{task_id}/workbench`：

```json
{
  "task_id": "task_example",
  "scope_revision": "opaque-scope-revision",
  "server_directory": "/host/project",
  "client_directory": null,
  "capabilities": {"files":true,"browser":true,"terminal":true,"recording":true,"tool_ui":true},
  "read_only": false
}
```

仅按当前 capabilities 和 read_only 开启功能。工作台写请求携带 expected_scope_revision；终端 DELETE 是明确例外，见下表。目录或权限变化导致冲突时刷新范围并让用户处理未保存内容，不给旧动作自动换新 scope。读请求也建议携带旧面板看到的范围，避免读到切换后的项目。

以下均以前缀 `/api/project-tasks/{task_id}` 定位：

| 功能 | 当前接口与接线 |
| --- | --- |
| 文件 | `/workspace/tree-children`、`GET/PUT/DELETE /workspace/file`、`POST /workspace/mkdir`、`POST /workspace/move`；读到 revision 后写回 expected_revision，首次创建用 `missing`。 |
| 上传/预览 | `POST /workspace/upload?path=完整目标文件路径`，multipart file 和 expected_scope_revision；`GET /workspace/media`、`/workspace/download-file` 支持单段 Range。 |
| 浏览器 | `GET /browser/sessions`、`POST /browser/handoff`、`WS /browser-stream/ws`；同一 Task/范围独立 profile，人工接管后才发控制输入。 |
| 终端 | `WS /terminal/ws` 传 session_id、token、expected_scope_revision；input/resize，接收 output/history/exit。非空 session_id 是持久会话，断开后最多保留 300 秒；省略/空白是临时 shell，断开即销毁。`DELETE /terminal/sessions/{session_id}` 返回 204，不接收 scope，可明确清理本人本 Task 的旧范围会话；不能重连或输入旧范围。 |
| 录制 | `GET /browser/record-replay/status`、`POST /start`、`POST /stop-and-generate-skill`；后两项使用相同 `/browser/record-replay` 前缀。停止生成草稿，不自动应用。 |
| 应用 Skill | `GET /browser/record-replay/skill-content?recordingId=...` 恢复草稿；审阅实际返回的 skillName/skillContent 后 `POST /recordings/{recording_id}/apply`，body 为 request_id、name、content、expected_scope_revision。下一轮和团队成员资源快照纳入已应用 Skill。 |
| 工具 UI | `POST /tool-ui/{turn_id}/{call_id}/request`，验证已保存工具来源并限 Task 工作区白名单。无自定义 renderer 时显示实际参数/结果；不能通用代理任意专家 API。 |

注意三类认证不能混淆：主 Task WS 用 Bearer 头或 authenticate 首帧；工作台浏览器/终端 WS 当前用 query `token`；HTTP 媒体直链用 `access_token`，不是 `token`。原生下载可继续用 Bearer。文件 path 是工作区相对路径，不能给专家旧 workspace 接口传 Task ID。

文件请求关键字段：GET tree/file、DELETE file 使用 query `path` 和 `expected_scope_revision`；PUT file 的 JSON 为 `{path,content,expected_scope_revision,expected_revision?}`，返回 `{path,content,size,revision}`；mkdir 使用 `{path,expected_scope_revision}`，move 使用 `{path,destination,expected_scope_revision}`。DELETE 成功 204。上传的 path 在 query，file 和 expected_scope_revision 在 multipart Form，返回 `{path,size}`。Range 成功为 206，非法范围为 416。

浏览器 handoff 使用 `{target:"agent"|"user",reason?,expected_scope_revision}`，返回 `{ok:true,session}`，以 session.control_owner 判断控制权。录制 start 使用 `{expected_scope_revision,name?,objective?,privacy?}`，privacy 为 mask-sensitive/strict；stop 使用 `{expected_scope_revision,recordingId?,name?,generateSteps?}`，注意 camelCase。skill-content 返回 `{ok,recordingId,skillContent,skillName,skillExists}`；apply 返回 `{task_id,path,name,recordingId}`。

工具 UI 请求体为 `{path,method,body,expected_scope_revision}`；实际只开放 GET /workspace/file、GET /workspace/tree-children、PUT /workspace/file。不能因为 method DTO 支持更多枚举就放行其他方法；history_only 工具副本不能调用这个宿主接口。

浏览器 start 消息可参考当前网页 `{type:"start",url:"",width:1280,height:800,reuse_session:true}`，空 URL 为接回当前页面；frame.data 是 JPEG base64。原生 APP 若实现 canvas 交互，继续按现有 BrowserStreamSession 输入协议适配，不能发起另一套绕过 Task scope 的 CDP/profile 连接。Task 没有原生桌面“打开浏览器窗口”能力声明。

本机浏览器 WS 可选 listen_only=1 只观察，不创建或控制浏览器；控制连接须携带 expected_scope_revision。本机终端 WS 可选 cols=80、rows=24；两类本机工作台 WS 不读取 Authorization 头，不能把 Bridge 的扩展认证方式套用到本机端点。

## 10. APP 代码切点与实施顺序

下列是只读核对位置，不表示这些文件已修改：

正式会话链路是 `OctopWorkspaceDataSource → OctopTaskSessionService → TaskConversationView`，由 `Features/Workspace/WorkspaceTaskRunCoordinator.swift` 装配。`OctopBoxTaskViewModel` 属于旧 UUID Box 路径，不作为新版统一会话的接入点；新能力应落在当前共享会话链路。

| 位置（相对仓库内 AgentComputer/） | 具体改动 |
| --- | --- |
| `Services/Box/OctopTaskSummary.swift` | 增加 Task 配置/专家/置顶字段，缓存 schema 升级后刷新缺失版本。 |
| `Services/Box/OctopTaskDetail.swift` | 增加本轮专家、目录、history_only、来源、成员 speaker 和 configuration_events；公开历史不恢复执行能力。 |
| `Services/Box/OctopTaskSocketFrame.swift` | create/message/retry payload 增加专家/模式及两个 expected revision；不添加不存在的 WS 命令。 |
| `Services/Box/OctopWorkspaceClient.swift` | 对接 resources.experts、全局 scope=all 列表、配置 PATCH、fork、from-thread、记忆与工作台 REST。 |
| `Services/Box/OctopWorkspaceDataSource.swift` | 统一 Task 列表投影和筛选游标；保留 endpoint/账号/connection generation fencing。 |
| `Services/Box/OctopTaskSessionService.swift` | 新增持久 pending/outbox，当前正式 lane 的 Submission 仅存内存；保存原请求体、配置版本与选项，冲突停队列，未知提交先核对原请求。旧 Box 的持久存储不能当作此链路已经具备持久化。 |
| `Services/Box/OctopTaskSocket.swift`、`OctopTaskProjection.swift` | 复用现有唯一 receiver；新增字段进入各自正确版本组的投影，保持 Resident 独立观察。 |
| `Data/Persistence/OctopWorkspaceCache.swift`、`OctopTaskBodyCache.swift` | 升级摘要/正文缓存格式；全部会话使用明确列表范围，不能把现有 projectID=nil 缓存直接解释为 scope=all。旧解码和 projectID 校验一并调整。 |
| `Features/Workspace/WorkspaceTaskRunCoordinator.swift`、`WorkspaceAgentViewModel.swift`、`Core/ConversationRunController.swift` | 沿当前共享会话装配接入可选专家、空闲切换、ask/plan/craft 和实际身份投影；保留 Resident 的独立装配。 |
| `Features/Workspace/TaskConversationView.swift`、`ProjectWorkspaceView.swift`、`MobileWorkspaceView.swift` 及 composer | 在 macOS/iOS 的正式入口展示逐轮身份、成员来源、只读历史和工作台；不接回旧 UUID Box 页面。 |
| 既有项目编辑与本机工具执行层 | 项目目录独立；client_directory 与本机授权仍分开，绝不继承专家旧目录。 |

客户端静态审查后的优先级如下。P0 是现有使用的保护项，应先完成再开放专家/模式切换及跨主机新写入；P1/P2 是新增能力接线，不计为已完成的 APP 功能。

当前先完成 P0/P1，逐项实施与验收使用[客户端 P0/P1 必做清单](agentcomputer-unified-task-p0-p1-checklist.md)。P2 不作为这两批交付的前置工作；本轮服务端提交、部署与文档交付不修改客户端代码。

| 优先级 | 交付 | 验收条件 |
| --- | --- | --- |
| P0 | 发送/重试版本与持久发送意图 | 落盘后才发送；保存作用域、请求种类、Task/重试目标、原 request_id、完整原始请求体与选项及受理状态；App 退出再打开仍能核对未知提交。网络重传保留原体，用户新重试使用新 ID 和已确认版本；跨设备配置冲突停队列。 |
| P0 | DTO、正文缓存与历史权限 | 新增字段贯通解码、投影、缓存和 UI；旧缓存升级后刷新权威状态；speaker 不合并丢失；history_only 不显示旧执行的重试、恢复，已完成历史仍显示 fork/edit/regenerate 分支操作。 |
| P0 | 全部会话与同步作用域 | 列表和 sync 一起使用 scope=all，调整项目校验、去重及分页；筛选/归档/host 改变后重建对应游标，不把列表游标当正文游标。 |
| P0 | 项目删除与归档 | 明确确认会停止并永久删除项目及全部 Task/历史附件；PROJECT_TASKS_BUSY 表示停止尚未确认，保留数据并允许重试 DELETE。归档保留历史且仍要求先停止；单 Task 删除 busy 限制不变。 |
| P1 | 可选专家、共享普通专家、本人团队与行动模式 | 无/有项目 × 通用/专家四组合真实多轮，空闲切换配置；逐轮实际身份/目录与成员来源正确；ask/plan/craft 不替代审批模式。 |
| P1 | 会话筛选、置顶归档与分支 | 服务端筛选/分页完整；fork/edit/regenerate 导航至返回的新 Task，保留原分支；旧历史单条续接保持原记录只读。 |
| P2 | Task 工作台、记忆、导出、润色与本机迁移 | 按 scope_revision 和能力开放工作台；记忆范围独立；润色应用前审阅；迁移预检、逐项进度、暂停恢复、自动开关与旧链接可核对，不把 Resident 记忆或外部导入代用。 |
| P2 | 跨主机 Task、媒体、账号语音和频道/cron | TaskRef 与账号/主机隔离队列、草稿、游标、媒体；拒绝失配 peer；Bridge HTTP 改写链接与 WS 原始路径都按所选 host 解析。语音和迁移等不在 Bridge 白名单内的能力只走当前直连主机。 |

Bridge 令牌过滤修复已纳入本次交付；部署方仍须按文首版本边界确认运行版本。共享团队、已有 Task 改主机、Bridge 迁移/from-thread/Resident/语音代理以及迁移 rollback/cancel 继续按明确未开放处理，不列入客户端本轮必做项。

服务端真实 GLM、团队、记忆和工作台验收证据见[本轮验收记录](unified-task-live-acceptance-2026-10-23.md)。这不代替 APP 真机、后台/恢复、双设备竞争及媒体内存测试。

## 11. 已实现的远程 Task 协议与迁移接线

### 11.1 执行权威、身份与缓存

远端 Task 是唯一执行权威；本地只保存已观察的 TaskRef 和摘要，不再创建第二份可执行 Task。客户端统一身份为 `{host_id: connection_id|null, task_id: 远端原始ID}`，本机 host_id 为 null。项目、专家、正文游标、草稿、队列、PTY 及媒体结果全部按 host 范围隔离。网页详情路由为 `/conversations/{task_id}?host_id={connection_id}`；它是客户端路由，不是新的服务端业务路径。

`GET /api/bridge/connections/{connection_id}/task-capabilities` 返回：

```json
{"host_id":"bridge_id","peer_identity":"opaque-binding-hash","available":true,"protocol_version":3,"capabilities":{"tasks":true,"projects":true,"task_ws":true,"files":true,"browser":true,"terminal":true,"recording":true,"tool_ui":true,"memory":true,"migrations":true},"max_request_bytes":16777216}
```

Bridge 会访问对端 `GET /api/project-tasks/capabilities` 并校验 `contract=unified-task-conversations`、`version=1`、`protocol_version=3`，同时校验 instance_id/account_id。peer_identity 是不透明绑定字符串，不是展示名或可由客户端重算的账号 ID。已绑定连接指向不同实例、账号或连接配置时拒绝旧引用，需新建 Bridge 连接。客户端每次重新连接/恢复焦点核对能力和身份，变更时丢弃旧视图及订阅。能力不支持或离线时不开放新写入，也不改投本机或旧 chat。

`GET /api/bridge/connections/{connection_id}/task-references` 返回 `{host_id,peer_identity,items:[{host_id,task_id,summary:TaskSummary,observed_at}],cached:true}`。离线可展示这些只读摘要并标注缓存；打开详情仍先验证当前 capability/identity。统一目录合并引用时用 host+task_id 去重，选定主机后的普通列表继续使用远端分页及 sync，不拼接其他主机游标。

远端专家必须使用远端项目/Task 工作目录。界面显示所属 host；directory 由远端校验。本机项目目录、client_directory、本机 MCP/Skill/凭据和客户端工具授权不自动传到远端。人设、记忆、凭据留在专家所在主机。

### 11.2 已实现的传输映射

| 本机桥端点 | 执行端协议 |
| --- | --- |
| `/api/bridge/connections/{cid}/task-api/{peer_path}` | 对端 `/api/{peer_path}`，例如 task-api/project-tasks/resources。按方法/路径白名单代理 JSON、multipart 和 Blob/Range。 |
| `WS /api/bridge/connections/{cid}/project-tasks/ws` | 对端统一 Task WS。发送原始 task/turn/request ID、cursor 和 revision。 |
| `WS /api/bridge/connections/{cid}/project-tasks/{id}/browser-stream/ws` | 对端 Task 浏览器流，携带 expected_scope_revision。 |
| `WS /api/bridge/connections/{cid}/project-tasks/{id}/terminal/ws` | 对端 Task PTY，携带 session_id 和 expected_scope_revision。 |

客户端只提交本机登录令牌；Bridge 自己管理对端鉴权。三个 Bridge WS 实际均支持 Bearer 头、query token 或 10 秒内 authenticate 首帧；原生 APP 统一建议 Bearer，浏览器可用首帧认证。本机直连主 Task WS 仍不接受 URL token，两者不要混淆。对端 session 刷新帧被丢弃，远端 socket 不负责本机令牌续期；不能覆盖本机 access_token。断线后恢复订阅并读取远端权威状态，不自动重发未知提交；用户重试保持原 request_id。

Bridge WS 的发送端和接收端只保留 expected_scope_revision/session_id/listen_only/width/height 这些 query 业务参数；终端 cols/rows 不在该白名单，远端尺寸通过 resize 帧设置。公开认证方式保持不变，发送前令牌隔离的修复版本边界见文首；支持 query token 不等于旧版本已经具备发送前过滤。

HTTP 白名单限统一 projects/project-tasks、附件、独立/专家记忆、工作台、settings/timezone 和只读知识库引用。认证、管理、任意 URL、专家旧工作区、`/project-task-migrations` 及 POST `/project-tasks/from-thread` 均未开放。capability 的 migrations=true 只表示对端服务能力，不能据此开启跨桥迁移。

capability 同时返回 max_request_bytes=16777216（16 MiB）；单次请求和响应均受限制，multipart 包含封装开销，超大导出也返回 413。媒体端点可用 Range 分段读取，export 不具备通用分块下载协议。HTTP JSON 响应中的允许媒体 URL 会改写到本机桥前缀；WS payload 不改写 URL，APP 必须带当前 host 将其中的 `/api/project-tasks/...` 媒体路径映射到对应 task-api 前缀，不能误读本机同名任务。媒体鉴权只使用本机令牌，不把令牌附到任意外链。

能力探测成功时 available=true；离线返回 `503 BRIDGE_NOT_CONNECTED`，不支持返回 `403 BRIDGE_REMOTE_UNSUPPORTED`，对端鉴权或身份改变返回 `403 BRIDGE_AUTH_FAILED`。这些远端错误不等于本机登录失效。task-references 只记录成功 HTTP 列表/详情/创建请求观察到的摘要；WS 单独创建后需再读取一次对应 HTTP 详情或列表，才可依赖离线索引。

文件/浏览器/PTY/录制分别使用对端工作台 capabilities；scope_revision 仍由执行端生成。客户端工具 UI 复用结构化/内置 renderer；不加载远端插件代码到本机。工具 host 的请求仍限定当前 Task 保存的调用来源和工作区白名单，不能用本机同名插件冒充远端工具所有者。

### 11.3 导出、独立记忆与润色

| 接口 | DTO/行为 |
| --- | --- |
| `GET /api/project-tasks/{id}/export?format=json|markdown` | Blob + Content-Disposition，冻结全部公开轮次、工具/来源及附件 manifest；不导出检查点或私有思考。 |
| `GET /api/project-tasks/{id}/memory?q=&limit=50` | `{task_id,enabled,entries:[{id,content,layer,editable,created_at?,updated_at?}]}`。limit 默认 50、范围 1–100；layer 为 atom/raw，原始证据只读。 |
| `POST /api/project-tasks/{id}/memory` | `{request_id,content,id?}`；新增 atom，指定 atom/id 时修正；未知回执手动重试沿用原请求，不自动重复保存。 |
| `DELETE /api/project-tasks/{id}/memory?id=atom/…&request_id=…` | 幂等停用 canonical 项，不能删除 raw 或改写历史/检查点。 |
| `POST /api/project-tasks/polish` | `{text,task_id?,selected_expert_id?,model_ref?}` → `{text}`。不创建 Task；显示可编辑审阅结果，用户应用后只替换草稿。请求期间草稿变化则阻止覆盖。 |

无专家 Task 显示独立记忆管理；选专家的 Task 显示第 9.2 节专家记忆管理。独立记忆 raw/id 与 atom/id 是不透明标识，编辑/删除发送服务端原值，不能截掉前缀。远端调用这些接口仍经 task-api 代理，不能读写本机 Task 的同名记忆。

独立记忆不接受工作台 scope_revision 或 Task expected_configuration_revision；POST 返回单个 TaskMemoryEntry，DELETE 成功 204。export 默认 json，JSON 顶层为 `{schema_version:1,task,snapshot_revision,configuration_events,turns}`，附件名为 TaskID.json/md。polish 实际调用模型并记录用量，不是幂等写操作；不要自动重试或自动覆盖用户已改动的草稿。

### 11.4 旧会话迁移

迁移基路径 `/api/project-task-migrations`，所有操作按当前账号校验，以下成功响应均为 HTTP 200（创建批次不是 201/202）：

| 操作 | 协议 |
| --- | --- |
| 预检/创建 | `POST /preflight`、`POST` 基路径，body `{request_id,thread_ids:[],project_id:null}`。thread_ids 可省略、最多 10,000 项；空列表选择本人历史并对特殊来源列原因。project_id 可省略/null 表示无项目，不猜测专家旧目录。 |
| 预检返回 | `{candidates:[{thread_id,agent_id,channel_type,disposition}],total,eligible,requires_model:false}`。 |
| 批次进度 | `GET` 基路径返回 MigrationBatch[]；`GET /{batch_id}` 返回一个完整批次。批次含 batch_id/status/created_at/updated_at/items，无 counts 字段；status 为 queued/running/waiting/paused/completed。items 含来源、target_task_id、pending/running/completed/blocked/failed/skipped、phase、error_code、source_digest 和 manifest。 |
| 控制 | `POST /{batch_id}/control {action:pause|resume}`，不回退已发布 Task。waiting 表示活跃源等待重试。 |
| 自动迁移 | `GET/PUT /automatic`，PUT `{enabled:boolean}` 是用户明确设置的目标态。 |
| 旧链接解析 | `GET /legacy/{thread_id}` → `{thread_id,task_id,state,url}`。state=task 时替换导航到统一 Task；draining 等待并禁旧写入；target_deleted 不复活。 |

批次 completed 可以包含 failed/skipped 项，不表示全部成功；按每项 status/error_code/manifest 展示结果。manifest.missing_attachment_count 大于零时仍展示缺失警告，不能凭本机同名文件补齐。resident/prototype/remote adapter_required 等跳过项不能显示为已迁移。自动开关停用只阻止新批次排队，已有批次需分别暂停；PUT 开关不需要 expected_revision。

迁移不调用模型、不执行工具、不删除源历史。当前 Bridge 白名单未开放迁移 API，客户端只管理当前直连主机来源；需要管理另一主机时，直接连接该主机并重新认证，不能把本机 thread_id 提交远端或根据 migrations=true 猜测代理支持。

### 11.5 已验证链路与剩余验收边界

本机已有 Task 跨主机转交仍是 **proposal**：用户明确创建有关联来源的新远端 Task，原 Task 保留；不得静默改变原 Task 执行主机。公开导出不能被当成可恢复检查点或工具授权。

服务端切点为 `infra/bridge/tasks.py`、`task_streams.py`、`tunnel_policy.py`、`api/routers/remote_tasks.py` 和 federated_tasks 引用仓库；Dashboard 的 taskTransport/TaskHostContext/TaskHostBoundary 提供主机隔离。APP 按 WorkspaceClient/DataSource/TaskSocket 接入同一 TaskRef，保持唯一 receiver 与本机连接代次 fencing，无需恢复 `/api/agentcomputer/v1`。

当前前端已做定向验证：同名 Task ID 不串队列/草稿、远端 session 不覆盖本机 token、peer_identity 改变销毁旧视图，能力不足不降级。双实例真实 Bridge socket + GLM-5 已验证远端 Task WebSocket 创建、远端模型与项目读写、附件上传读取、导出及断开后的只读缓存索引，本机模型调用为 0。主流同时支持 resident 订阅，但 Bridge 双侧仅允许 Task 命令，不代理私人 Resident；初始化失败关闭并清理 stream。

远端浏览器/PTY 实际画面、断网后重连执行、共享撤权的真实双主机流程、APP 真机后台/双设备竞争仍应单独验收；网页本机工作台证据不代替这些跨主机验收。

## 12. 现代频道入口与本工作树验证记录

频道入口已新增 owner-scoped `GET/POST /api/channels` 和 `GET/PATCH/DELETE /api/channels/{channel_id}`。创建 body 为 `{kind,name,config,selected_expert_id:null,project_id:null,action_mode:"craft",enabled:true}`，config 仅放频道适配器配置。专家/项目不再是 URL 前提；PATCH 省略绑定时保留，显式 null 清空。服务端持久保存并立即重载频道，新消息经统一 Task ingress；历史 Task 不因频道修改而被改写。需要 channels 权限且只可访问本人频道，不能通过此入口选择或修改 Resident 专用绑定。旧 agent-scoped API 保留。

同一专家下的频道名按 owner 隔离；本人重复绑定名和本人通用频道重名返回 `CHANNEL_NAME_TAKEN` 409，包含并发唯一约束冲突。归档/他人项目不能绑定，未知字段及非法 null 返回验证错误。APP 读取顶层 selected_expert_id/project_id/action_mode，不解析服务端内部的配置存储布局。

2026-10-23 本工作树验证：前端受影响主组合 249 项通过；专家入口路由 8 项另行复测通过（含 1 项新增跨主机路由断言）。覆盖同名 Task 的队列/草稿隔离、离线引用、peer 身份变更、媒体鉴权、记忆保存、润色审阅、迁移预检及旧会话功能回归。TypeScript 通过，定向 ESLint 无 error（原专家编辑器 2 条 warning 保留）。现代频道 HTTP 13 项通过，含实际 SQLite 持久化/清空、owner 隔离、Resident 专用边界、共享同名和查重竞态；频道 router 严格 mypy、Ruff 检查和格式检查通过，`/api/docs` 及 typed OpenAPI schema 已检查。

网页构建执行 `cd dashboard && npm run build`，成功产物与当前源代码对应。该记录不宣告 APP Swift/UI 已接线，也不替代真实 STT/TTS 供应商、音视频编解码兼容性或 APP 真机后台/双设备验收。远程真实双服务结果另由本轮验收文档记录。

### 12.1 定时入口

客户端若管理定时任务，使用 owner-scoped `GET/POST /api/cron`、`GET/PATCH/DELETE /api/cron/{cron_id}`、`POST /api/cron/{cron_id}/run-now`。POST 创建返回 201 CronResponse，GET/PATCH 返回 CronResponse，DELETE/run-now 返回 204。CLI 和 IM 的内部受理收据不是 APP 的消息 API。

创建最小示例：

```json
{"name":"每日项目回顾","trigger":"cron:0 9 * * *","prompt":"总结项目变化","project_id":null,"selected_expert_id":null,"conversation_policy":"per_job"}
```

新增顶层入口默认 per_job；策略可选 per_run/per_job/task/continue_target。per_run 每次独立建 Task，per_job 复用此作业的 Task；task 必须指定本人 target_task_id，其他策略不能附 target_task_id。continue_target 保留已有来源绑定规则，不是客户端自行寻找“最近会话”。PATCH 省略可空绑定字段表示保留，明确 null 才清空；触发时间使用服务端时区。

## 13. 客户端审阅清单

请先逐项标记“已兼容 / 需要改造 / 服务端缺口”，并给出对应客户端文件或 DTO；不以服务端自动化通过代替客户端已接线：

- 无/有项目 × 通用/专家四种组合，首轮和追问共用同一个 Task，身份与目录按轮显示。
- 省略/null/有值三态，配置 CAS、原请求持久化、未知回执核对，以及全局审批版本。
- 唯一 receiver、异步帧交错、累计正文替换、独立游标、单任务 reset 与后台重连。
- 团队 speaker 来源、共享撤权、Resident 专用观察，以及禁止把其当普通可选专家。
- 文件/浏览器/终端的 scope 与授权，Skill 草稿审阅，记忆/语音/导出的实际返回类型。
- TaskRef 主机隔离、远端媒体 URL 解析、peer 身份变更、离线只读缓存和错误局部处理。
- 本机旧链接解析、显式单条续接、批迁逐项结果，以及跨桥迁移暂不开放。
## 执行引擎接线增量（2026-10-27）

服务端新增独立于专家的 Task.engine 和每轮 execution_engine；详情、创建、PATCH、Fork 与 WebSocket schema 已同步。读取执行盒子的 execution_engines 能力和引擎目录后，按其 capabilities 显示入口，不能使用控制端本机 ACP 状态。模型须区分 Octop Provider/model_ref 与 ACP connection/model_id；历史身份优先使用当轮 actual_model_id。APP Swift/UI 本轮未修改，后续接线按 [执行引擎协议](task-execution-engines.md) 和 [WebSocket 增量](project-task-websocket.md) 实施。

Codex 的一键连接接口为管理员调用执行盒子的 `POST /api/acp/codex/connect`，返回 `202 ACPSetupJob`，`kind="connect"`。盒子连续完成缺失组件安装、已有授权检测、必要时设备码授权、连接检测及启用；APP 只需轮询 `GET /api/acp/jobs/{job_id}`、显示设备码，并用系统浏览器打开 `authorization.url`。用户关闭连接面板或重进 APP 后，可从 `GET /api/acp/setup.active_jobs` 恢复进度；取消沿用 `/api/acp/jobs/{job_id}/cancel`。成功以 `status="succeeded"` 且 `setup.connection="ready", setup.enabled=true` 为准，不另发启用请求，不在用户电脑下载安装 Codex，不搬运宿主凭据。进度 job 不跨服务重启恢复，重启后重新点击连接会检查现有安装和授权。APP 的 Swift/UI 接线仍留待后续实施。
