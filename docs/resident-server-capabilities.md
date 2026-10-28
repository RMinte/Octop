# 私人智能体：服务端能力与接入顺序

本说明汇总统一生活工作管家的正式服务端接口，包括持续交流、内置网络搜索、图片与文件输入、成果下载、私人记忆、任务委派和主动跟进。AgentComputer 本轮没有改动；先用 `GET /api/agents/resident-capabilities` 读取实际能力，再接入正式页面。全部入口属于普通 `/api`，需要正常认证，不增加 AgentComputer 专用协议或远程项目任务 Bridge。

## 领域关系

每个用户通过唯一 `primary` 绑定获得一位生活工作管家，沿用真实 `agent_id`、固定 `thread_id`、工作区和私人记忆。管家负责理解需求、整理安排、持续追问和跟进结果；专业工作通过任务委派处理，用户始终回到同一管家会话。专业方法和执行能力由任务选定的配置提供，不创建或切换独立专业助手。

会话负责持续交流，历史和私人记忆跟随同一管家。项目独立保存工作环境和能力配置，不绑定或修改专家。普通 Task 负责具体执行：用户直接说出工作需求，管家按语义判断是否需要委派；未选择执行范围时创建无项目任务，显式选择时使用该项目或无项目范围。管家通过持久委派关联接收真实成果，后续交流可基于这些结果继续整理和追问。关注事项只保存明确授权，定时工作仍进入普通 Task。

## 接口总览

以下 `B` 表示 `/api/agents/{agent_id}/threads/{thread_id}`；接入时使用当前用户的唯一管家绑定，各入口均校验用户与 Agent/thread 的归属，管理员也不通过这些入口读取其他用户私人数据。

能力响应以 `identity_initialization: "server_managed"` 声明身份由服务端自动准备，以 `assistant_role: "life_work_steward"` 和 `professional_work: "delegated_tasks"` 明确角色及分工方式，不再提供 `profiles` 或 `explicit_template_adoption` 字段。`task_delegation: "natural_language"` 与 `model_task_delegation: "one_per_turn"` 表示新 primary 消息默认支持自然语言委派，每轮至多创建一个任务。已有主管家的新轮次在受理时冻结当前管家提示和能力配置；不会重写身份记录、历史或已受理轮次的配置，旧消息不因能力升级获得新权限，重连与恢复仍沿用原收据。

| 范围 | 接口 | 行为 |
| --- | --- | --- |
| 能力 | `GET /api/agents/resident-capabilities` | 类型化能力声明，读取不创建资源或授权。 |
| 管家身份 | `GET /api/agents/resident-conversation`；`PUT` 同一路径用于显式重试或修复 | 服务端自动准备唯一 primary，客户端读取绑定即可；GET 不创建资源。 |
| 连续会话 | `POST B/messages`、`GET B/requests/{request_id}`、`GET B/conversation`、`POST B/cancel` | 受理收据、部分回复、断线查询和精确停止。 |
| 会话设置 | `GET/PATCH B/conversation/settings` | 持久保存内置搜索开关；后续新轮继承，已受理轮次保持原值。 |
| 历史 | `GET B/conversation/history` 及其 `days/search/items/{item_id}` 路由 | 按日、有界分页、搜索、定位，包括回报和主动消息。 |
| 记忆 | `B/memory` 下的事实、policy、candidates、sources | 查看、更正、废弃、原始采集开关、显式提炼、候选确认与来源。 |
| 委派 | `POST B/messages`；`POST/GET B/conversation/delegations` | 自然语言工作请求可由模型按需创建普通 Task；独立接口也可显式创建，均持久关联并回流成果。 |
| 跟进 | `POST/GET B/conversation/followups`、`GET/PUT .../{id}`、`POST .../{id}/cancel` | 定时提醒、安静跟踪任务、明确授权的定时 Task。 |
| 通知 | `GET B/conversation/notifications`、`GET .../{id}` | 持久通知与分阶段投递状态。 |
| 未读 | `GET B/conversation/inbox`、`POST B/conversation/read` | 同账号各设备共享的单调消息读取水位。 |
| 模型上下文 | `GET B/conversation/context`、`POST B/conversation/context:compact` | 用量与空闲时显式整理；完整展示历史保留。 |

字段和完整路径以 `/api/openapi.json`、交互文档 `/api/docs` 以及以下专题文档为准：

- [稳定身份与真实对话](resident-agent-conversation.md)
- [历史与私人记忆](resident-history-memory.md)
- [委派与成果回流](resident-delegation-results.md)
- [统一管家与专业工作委派](resident-professional-instances.md)
- [授权主动跟进、定时 Task 与通知](resident-followups.md)

## 内置搜索与双向附件

私人助手复用普通任务已配置的搜索服务与免配置搜索工具；查询事实和近期信息可直接回答，无需仅为搜索创建任务。搜索开关与普通任务采用相同语义：默认开启、持久保存，后续新轮继承当前值，每轮在受理时冻结；修改不影响已受理、执行中或原请求重试的轮次。此能力不代表放开浏览器、任意网页或主机文件读取、命令或 MCP。

`ResidentCapabilities.web_search=true`，`message_types` 包含 `text/image/file`。附件上传和预览或下载复用 `/api/project-tasks/attachments`，发送时把附件 ID 放入 `ResidentMessage.attachment_ids`；每条最多 9 个、单个最多 25 MiB，仍受服务端上传限制。可仅发附件；JPEG、PNG、WebP、GIF 图片需视觉模型，普通文件不要求视觉。`ResidentTurn.attachments` 在回执、快照、实时更新与历史中返回同一份附件元数据。重传原请求须保留正文与附件 ID；附件不自动转交委派任务。

新轮次默认提供 `resident_read_attachment`，按 ID 只读当前私人会话的上传文件；省略 ID 列出普通附件，`offset/limit` 控制分页。支持文本、PDF、Word、Excel、PowerPoint 等可提取文字的文档，无 OCR；不支持的格式、损坏文件或无文字内容返回明确错误。文件内容不构成执行授权，工具不能读取模型提供的主机路径。

委派任务发布的真实成果通过回报 `artifacts` 回到原会话与历史。Dashboard 直接展示文件名、大小和下载按钮，使用回报 `task_id`、`artifact_id` 调用鉴权的 `GET /api/project-tasks/{task_id}/artifacts/{artifact_id}/download`。文件产出仍由普通 Task 完成；私人会话不直接执行任意文件写入或命令。

完整差异与示例见[私人助手与任务能力对照](resident-task-capabilities.md)。

### 搜索开关与 App 菜单接线

`ResidentCapabilities.web_search=true` 表示具备内置搜索能力，`conversation_settings=true` 表示可读取和修改持久会话设置。App 只在后一个能力明确为 true 时开放菜单操作；旧服务端没有此声明时继续沿用原行为，不发送未支持的请求。

`GET B/conversation/settings` 返回：

```json
{"web_search_enabled":true,"revision":1}
```

关闭搜索使用 `PATCH B/conversation/settings`：

```json
{"expected_revision":1,"web_search_enabled":false}
```

实际改变后返回新版本，例如 `{"web_search_enabled":false,"revision":2}`。两个字段均严格校验类型；`web_search_enabled` 只接受 JSON 布尔值，`expected_revision` 是大于等于 1 的整数。旧版本写入返回 `409 RESIDENT_SETTINGS_CONFLICT`；当前版本写相同值不增加版本。读取和更新均限本人绑定，不创建会话，也不调用模型。

`ResidentSnapshot.settings` 与当前会话 `revision` 在同一数据库快照读取；设置变化也会通过已有 `resident.snapshot` 推送。菜单以 `settings.web_search_enabled` 显示当前选择，并用 `settings.revision` 修改；它独立于随回答增量更新的会话版本。切换成功后采用服务器回执；冲突或写入结果未知时重新读取设置核对，不用旧菜单状态自动覆盖另一个设备的更新。

发送 `ResidentMessage` 无须携带开关参数，与普通任务追问一样，服务端在受理事务中读取当前设置。`ResidentTurn.web_search_enabled` 是该轮实际冻结值，在受理回执、原请求查询、快照、历史和实时更新中一致；不能用当前菜单状态改写历史。升级前没有搜索能力快照的旧轮次仍返回 false，不因重传或恢复增权。

关闭控制私人助手本轮的内置搜索工具，保留此前资料上下文；委派任务继续遵守自己的任务设置，开关不跨任务隐式继承。App Swift/UI 菜单接线仍是后续工作，本轮提供服务端接口、类型和文档。

## 按轮授权的助手工具

`POST B/messages` 只需 `request_id/prompt` 即可开始。新 primary 轮次默认向管家提供 `resident_create_task` 和本会话任务查询工具：管家理解用户实际工作请求，按需整理交付要求并委派，不按“任务”“派发”等关键词硬触发。闲聊、知识咨询和可直接回答的问题继续在会话中处理；信息不足时先追问，不为显示派发而创建 Task。

例如直接向管家发送：

```json
{"request_id":"stable-send-id","prompt":"帮我做一份五个工作日的工作与生活安排，分上午、下午、晚上，整理好后把结果发回这里。"}
```

无需客户端先创建任务或提供 `task_context`。未显式选择范围时，服务端固定为无项目普通 Task，使用任务自己的独立工作空间；不附加客户端工具、附件、Skill、MCP、插件、子代理、知识库、长期记忆或团队授权。Task 实际执行仍遵守普通任务现有权限与审批规则，独立工作空间不等于操作系统沙箱。

以下字段用于额外的按轮选择：

| 字段 | 默认 | 行为 |
| --- | --- | --- |
| `read_tools` | `false` | 额外允许模型按需检索本管家有效私人记忆；新 primary 轮次查询本会话已委派任务不依赖此开关。 |
| `knowledge_base_ids` | `[]` | 只在本轮检索这些用户明确选择、当前仍可访问的知识库，返回真实片段与引用；不继承全局默认。 |
| `task_context` | `null` | 可选高级范围选择；提供时须含 `project_id`（可显式 null）及完整普通 `TaskMessage`。项目、设备、附件和执行选项由宿主固定，模型只能整理工作说明；省略时仍可自然语言委派无项目任务。 |

例如文字咨询并允许查询管家记忆与任务进度：

```json
{"request_id":"stable-send-id","prompt":"查看我之前确认的写作偏好和相关任务进度。","read_tools":true}
```

需要选择特定项目、设备、附件或执行选项时，宿主可提供 `task_context={"project_id":null,"message":{"request_id":"selected-intent","prompt":"用户选定的具体任务要求","options":{}}}`，使用普通 Task 的完整类型与校验。模型不能从聊天提及、历史、记忆或全局默认猜测这些范围，也不能提供或替换 owner、项目、设备及权限；来源轮次由运行时绑定，不从模型参数读取。每轮只有一个 Task 槽；相同工作说明的重复工具调用返回同一任务，改变说明返回冲突，不再创建第二个任务。工具受理时再次确认来源轮仍活动、未取消且租约有效。

已由用户确认的少量有效事实仍会沿用既有的有界上下文召回；`read_tools` 控制额外的按需查询，不控制原始观察采集。

所有额外选择与实际能力配置快照持久保存，`ResidentTurn.authorization` 回显该原请求的显式选择；默认自然语言委派能力以受理时冻结的配置为准。相同请求 ID 改正文或授权返回冲突；服务端模型、提示或能力后来改变不重跑或升级旧请求。管家会话只读自己的上传附件，不直接获得 shell、任意文件访问、MCP 或电脑操作工具，实际执行进入普通 Task。原始记忆采集、知识库访问及主动跟进继续独立授权，不因提出一个工作请求而开启。

## 未读与重连

`GET B/conversation/inbox` 返回 `agent_id/thread_id/revision/unread_count/last_message_id/last_read_message_id`。发送 `POST B/conversation/read {"message_id":"最后实际读到的稳定消息ID"}` 推进水位。普通回答使用收据的 `assistant_message_id`，任务回报和主动消息使用自己的 `message_id`。不能提交用户消息、别的会话锚点或客户端自算序号。

较早锚点重传不会回退水位，也不会消耗并发到达的新消息。未知或不属于当前作用域的锚点统一返回 `409 RESIDENT_READ_ANCHOR_INVALID`。已有历史升级时作为已读基线，升级后的新回复、已交付成果和通知各计一次；逐 token 更新不重复计数。

断线后查询原 `request_id`，重新读取同一会话快照、历史和 inbox。客户端不能因网络超时换一个请求 ID 自动重发。通知使用正式归档与上下文各自的阶段收据，重启后补投保持稳定消息 ID。取消或更新关注会撤销旧版本未投递的通知。

## 记忆与上下文

记忆默认关闭自动采集。用户显式开启 `PUT B/memory/policy {"capture_enabled":true,"expected_revision":1}` 后，后续启动的用户轮次采集原始观察，仍不自动提炼或确认事实。

`POST B/memory/candidates:extract` 使用服务端选择的模型，把近期捕获的用户原文提炼为待审候选；不提炼助手推断、任务回报或日记。每次最多 200 条近期用户观察、64 KiB 输入；响应明确返回处理数量、待处理数量、截断和更早记录提示。窗口内持久消费收据避免重启后再次提炼已处理内容。旧于该窗口的材料可由用户显式整理为事实，不宣称此接口会全量沉淀全部历史。

候选经过用户 `:approve` 才成为长期事实，`:reject` 保留审计。事实可更正或废弃，来源只展示有界原文和已验证的会话锚点。任务成果进入会话不等于自动写入长期记忆。

`POST B/conversation/context:compact` 是独立的显式整理操作，不自动重试。它使用当前服务端模型，只更新模型上下文摘要，保留原 thread 与完整展示历史；当前轮次活动时拒绝。响应不暴露主机文件路径。连接断开后，服务端仍等待已经受理的检查点写入完成。

## AgentComputer 建议接入顺序

服务端负责准备管家：`UserManager` 启动为已有启用用户补齐唯一身份，新用户创建、邀请注册、SSO/LDAP 接入、用户启用及成功登录时均幂等准备。不会调用模型或新增执行授权。准备异常仍可返回 `failed`；修复后由服务启动、成功登录或显式运维重试继续使用原 Agent/thread，历史保留。

1. 读取能力声明及 `GET /api/agents/resident-conversation` 的唯一管家绑定；`ready` 后将生活与工作需求送入同一会话。客户端不依赖先调用 PUT，也不展示创建或配置管家的步骤。GET 始终只读，仍须处理 `not_created / initializing / failed` 等状态；PUT 仅保留作显式幂等重试或修复。
2. 持久发送收据、最近快照、按日历史与搜索；重连沿用原请求和 thread。
3. 接入图片和普通文件上传，直接发送自然语言工作请求，展示关联任务卡与真实成果下载，结果回到原管家会话；高级范围选择按需接入 `task_context`。
4. 记忆管理、采集授权、候选与来源查看。
5. 关注事项的明确授权、暂停、取消，以及通知历史与共享已读水位。
6. 模型上下文用量与显式整理。

正文实时观察已支持共享 WebSocket，能力响应新增 `conversation_stream={transport:"websocket",path:"/api/project-tasks/ws",protocol_version:3}`；`resident.subscribe` 推送有界累计正文和状态，原 REST 接口继续用于受理、恢复与历史读取。旧 `conversation_transport` 字段保留，避免尚未接线的 App 因能力门禁失效；新客户端应依据新增声明接入。客户端本轮未改动，具体协议和验收见 [App 共用正文 WebSocket 交接](agentcomputer-conversation-stream-handoff.md)。系统推送 APNs 仍属于后续工作；会话 WebSocket 不等于系统后台推送。私人工作区不自动取得项目目录或电脑执行权限，实际工作继续由普通 Task 的权限、设备声明与审批约束。
