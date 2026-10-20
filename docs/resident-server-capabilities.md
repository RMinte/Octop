# 私人智能体：服务端能力与接入顺序

本说明汇总私人助手、专业实例和主动跟进的正式服务端接口。AgentComputer 本轮没有改动；先用 `GET /api/agents/resident-capabilities` 读取实际能力，再接入正式页面。全部入口属于普通 `/api`，需要正常认证，不增加 AgentComputer 专用协议或远程项目任务 Bridge。

## 领域关系

专家模板提供专业身份和工作方法。默认私人助手及每个专业实例分别拥有真实 `agent_id`、固定 `thread_id`、工作区和记忆命名空间。切换专业实例须切换这组真实 ID。模板更新先显示差异，用户明确采用后影响后续轮次，原历史和记忆保留。

会话负责持续交流。项目独立保存工作环境和能力配置，不绑定或修改专家。任务负责具体执行，使用用户明确选择的项目或无项目范围；私人智能体通过持久委派关联接收真实成果。关注事项只保存明确授权，定时工作仍进入普通 Task。

## 接口总览

以下 `B` 表示 `/api/agents/{agent_id}/threads/{thread_id}`；每个入口只允许当前用户自己的私人实例，管理员也不通过这些入口读取其他用户私人数据。

| 范围 | 接口 | 行为 |
| --- | --- | --- |
| 能力 | `GET /api/agents/resident-capabilities` | 类型化能力声明，读取不创建资源或授权。 |
| 默认身份 | `GET/PUT /api/agents/resident-conversation` | 读取或幂等建立唯一 primary。 |
| 专业实例 | `GET/POST /api/agents/resident-profiles` | 列出或幂等创建独立实例。 |
| 模板 | `GET .../resident-profiles/{id}/template-diff`、`POST .../{id}/adopt-template` | 预览内容版本；按版本与修订明确采用。 |
| 归档 | `PATCH .../resident-profiles/{id}/archive` | 保留数据，撤销跟进；恢复不复活旧授权。 |
| 连续会话 | `POST B/messages`、`GET B/requests/{request_id}`、`GET B/conversation`、`POST B/cancel` | 受理收据、部分回复、断线查询和精确停止。 |
| 历史 | `GET B/conversation/history` 及其 `days/search/items/{item_id}` 路由 | 按日、有界分页、搜索、定位，包括回报和主动消息。 |
| 记忆 | `B/conversation/memory` 下的事实、policy、candidates、sources | 查看、更正、废弃、原始采集开关、显式提炼、候选确认与来源。 |
| 委派 | `POST/GET B/conversation/delegations` | 用户显式委派普通 Task，持久关联与成果回流。 |
| 跟进 | `POST/GET B/conversation/followups`、`GET/PUT .../{id}`、`POST .../{id}/cancel` | 定时提醒、安静跟踪任务、明确授权的定时 Task。 |
| 通知 | `GET B/conversation/notifications`、`GET .../{id}` | 持久通知与分阶段投递状态。 |
| 未读 | `GET B/conversation/inbox`、`POST B/conversation/read` | 同账号各设备共享的单调消息读取水位。 |
| 模型上下文 | `GET B/conversation/context`、`POST B/conversation/context:compact` | 用量与空闲时显式整理；完整展示历史保留。 |

字段和完整路径以 `/api/openapi.json`、交互文档 `/api/docs` 以及以下专题文档为准：

- [稳定身份与真实对话](resident-agent-conversation.md)
- [历史与私人记忆](resident-history-memory.md)
- [委派与成果回流](resident-delegation-results.md)
- [专业实例与模板更新](resident-professional-instances.md)
- [授权主动跟进、定时 Task 与通知](resident-followups.md)

## 按轮授权的助手工具

`POST B/messages` 保留 `request_id/prompt`，新增可选字段：

| 字段 | 默认 | 行为 |
| --- | --- | --- |
| `read_tools` | `false` | 允许模型按需检索本实例有效私人记忆、查询本会话已委派任务。 |
| `knowledge_base_ids` | `[]` | 只在本轮检索这些用户明确选择、当前仍可访问的知识库，返回真实片段与引用；不继承模板或全局默认。 |
| `task_context` | `null` | 用户明确授权的一个任务槽：必填 `project_id`（可显式 null）及完整普通 `TaskMessage`。模型只能整理工作说明，项目、设备、附件和执行选项由宿主固定。 |

例如文字咨询并允许查询本实例信息：

```json
{"request_id":"stable-send-id","prompt":"查看我之前确认的写作偏好和相关任务进度。","read_tools":true}
```

需要委派时，宿主明确提供 `task_context={"project_id":null,"message":{"request_id":"selected-intent","prompt":"用户选定的具体任务要求","options":{}}}`；有项目、客户端工具或附件时使用普通 Task 的完整类型与校验。模型不能提供或替换 owner、项目、设备及权限，来源轮次由运行时绑定，不从模型参数读取。一轮最多一个 Task；重复工具调用返回同一任务，改变工作说明不会再创建一个任务。工具受理时再次确认来源轮仍活动、未取消且租约有效。

已由用户确认的少量有效事实仍会沿用既有的有界上下文召回；`read_tools` 控制额外的按需查询，不控制原始观察采集。

所有新增选择与配置快照持久保存，`ResidentTurn.authorization` 回显该原请求的授权。相同请求 ID 改正文或授权返回冲突；服务器模型或模板后来改变不重跑旧请求。未选择这些字段时保持原来的纯文字会话。任意 shell、文件、MCP、电脑操作和未声明工具仍不可用，实际执行继续进入普通 Task。

## 未读与重连

`GET B/conversation/inbox` 返回 `agent_id/thread_id/revision/unread_count/last_message_id/last_read_message_id`。发送 `POST B/conversation/read {"message_id":"最后实际读到的稳定消息ID"}` 推进水位。普通回答使用收据的 `assistant_message_id`，任务回报和主动消息使用自己的 `message_id`。不能提交用户消息、别的会话锚点或客户端自算序号。

较早锚点重传不会回退水位，也不会消耗并发到达的新消息。未知或不属于当前作用域的锚点统一返回 `409 RESIDENT_READ_ANCHOR_INVALID`。已有历史升级时作为已读基线，升级后的新回复、已交付成果和通知各计一次；逐 token 更新不重复计数。

断线后查询原 `request_id`，重新读取同一会话快照、历史和 inbox。客户端不能因网络超时换一个请求 ID 自动重发。通知使用正式归档与上下文各自的阶段收据，重启后补投保持稳定消息 ID。取消或更新关注会撤销旧版本未投递的通知。

## 记忆与上下文

记忆默认关闭自动采集。用户显式开启 `PUT B/conversation/memory/policy {"capture_enabled":true,"expected_revision":1}` 后，后续启动的用户轮次采集原始观察，仍不自动提炼或确认事实。

`POST B/conversation/memory/candidates:extract` 使用服务端选择的模型，把近期捕获的用户原文提炼为待审候选；不提炼助手推断、任务回报或日记。每次最多 200 条近期用户观察、64 KiB 输入；响应明确返回处理数量、待处理数量、截断和更早记录提示。窗口内持久消费收据避免重启后再次提炼已处理内容。旧于该窗口的材料可由用户显式整理为事实，不宣称此接口会全量沉淀全部历史。

候选经过用户 `:approve` 才成为长期事实，`:reject` 保留审计。事实可更正或废弃，来源只展示有界原文和已验证的会话锚点。任务成果进入会话不等于自动写入长期记忆。

`POST B/conversation/context:compact` 是独立的显式整理操作，不自动重试。它使用当前服务端模型，只更新模型上下文摘要，保留原 thread 与完整展示历史；当前轮次活动或专业实例已归档时拒绝。响应不暴露主机文件路径。连接断开后，服务端仍等待已经受理的检查点写入完成。

## AgentComputer 建议接入顺序

1. 能力声明、默认身份、专业实例选择和模板来源。
2. 持久发送收据、最近快照、按日历史与搜索；重连沿用原请求和 thread。
3. 关联任务卡与真实产物跳转。
4. 记忆管理、采集授权、候选与来源查看。
5. 关注事项的明确授权、暂停、取消，以及通知历史与共享已读水位。
6. 模型上下文用量与显式整理。

REST 轮询是当前正式传输。系统推送 APNs、客户端缓存和界面仍属于后续客户端接入工作；服务端会话通知不等于系统后台推送。私人工作区不自动取得项目目录或电脑执行权限，所有实际工作继续由普通 Task 的权限、设备声明与审批约束。
