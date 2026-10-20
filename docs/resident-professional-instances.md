# 私人专业实例接口

专业实例把专家模板的身份和工作方法保存为用户自己的独立 Agent。每次创建得到真实且稳定的 `profile_id`、`agent_id`、`thread_id`，分别保存会话历史、记忆、委派来源和关注事项。默认私人助手的绑定保持原样；选择另一个专业实例时，客户端使用它自己的 Agent 和 thread 地址。

这些接口均要求登录，只能操作当前用户的实例。专家模板来自现有内置专家库或已发布专家库。已发布模板按专家库现有的公开读取规则使用；模板发布者的 Agent、私人历史、记忆和权限不会被复用。

## 创建和读取

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/agents/resident-profiles` | 返回当前用户的 `{ "profiles": [...] }`，读取不创建实例。 |
| POST | `/api/agents/resident-profiles` | 幂等创建专业实例，返回 `201` 和实例。 |
| GET | `/api/agents/resident-profiles/{profile_id}` | 读取实例、绑定和模板版本。 |
| GET | `/api/agents/resident-profiles/{profile_id}/template-diff` | 预览当前来源模板与已采用快照的差异。 |
| POST | `/api/agents/resident-profiles/{profile_id}/adopt-template` | 明确采用已审阅的模板版本。 |
| PATCH | `/api/agents/resident-profiles/{profile_id}/archive` | 归档或恢复实例，保留所有数据。 |

创建请求示例：

```json
{
  "request_id": "create-writer-01",
  "name": "我的写作助手",
  "template_kind": "builtin",
  "template_id": "general-assistant"
}
```

`template_kind` 为 `builtin` 或 `published`；`template_id` 使用对应专家库返回的标识。`name` 是 1 至 100 个字符的实例名称。`request_id` 使用 1 至 200 个字母、数字、下划线或连字符。丢失响应后，重用原标识及原始输入，返回原实例；同一标识更改输入返回 `RESIDENT_REQUEST_CONFLICT`（409）。不同创建标识代表不同实例，包括从同一模板创建多个实例。

实例返回字段包括：

| 字段 | 含义 |
| --- | --- |
| `profile_id` | 专业实例标识，用于模板采用和归档。 |
| `agent_id`、`thread_id` | 真实私人 Agent 和常驻会话标识，用于已有对话、历史、记忆、委派和跟进接口。 |
| `name` | 实际实例名称；同名时服务端增加短后缀以避免 Agent 名称冲突。 |
| `status`、`error_code` | 工作区初始化状态 `initializing`、`ready`、`failed` 及错误码。 |
| `template_kind`、`template_id` | 来源模板，不是执行中的专家绑定。 |
| `template_version` | 已保存模板内容快照的 SHA256 版本。 |
| `revision` | 专业实例修订，采用和归档操作必须提交预期修订。 |
| `archived` | 是否归档。 |
| `tools_enabled` | 本版恒为 `false`，表示实例不持有常驻的 Shell、文件或 MCP 执行工具；本轮的受限宿主能力由消息授权单独决定。 |
| `created_at`、`updated_at` | 带 UTC 时区的 ISO 时间；展示时采用服务端配置时区。 |

Agent、thread 和专业实例在同一事务中保留身份，再初始化专用工作区。初始化失败后用原创建请求重试，继续原绑定，不创建替代实例。重启后从数据库读取同一身份。来源模板后来下架也不会删除已创建实例；原创建请求仍可幂等重试。

## 专业方法和模板更新

实例只采用根目录人格和方法 Markdown 文档，以及模板名称、说明。用户资料、长期记忆、工具说明、主动计划、Skills、MCP 和子代理不随模板复制。来源模板不会获得实例工作区的写入权限，也不会自动更新实例。

差异接口返回 `profile_id`、`revision`、`current_version`、`available_version`、`has_changes` 和 `changes`。每条差异包含 `path`、`before`、`after`；新增或删除内容的一侧为 `null`。读取差异不改 Agent 提示、历史或记忆。

明确采用时提交：

- `expected_revision`：差异响应中的 `revision`。
- `template_version`：差异响应中的完整 `available_version`。

来源模板在审阅后又变化，返回 `RESIDENT_TEMPLATE_CHANGED`（409），需要重新预览。实例被另一次采用或归档操作修改，返回 `RESIDENT_PROFILE_CONFLICT`（409），需要重新读取。采用版本与当前版本相同且修订仍匹配时，不增加修订。

采用只更新实例保存的模板快照和专业提示，不重建 Agent 或 thread，也不覆盖历史、记忆、工作区和权限。每轮对话在受理时冻结提示及模板版本；已经受理的轮次沿用原快照，新轮次使用新版本。

## 对话和各类数据的范围

拿到实例的 `agent_id` 和 `thread_id` 后，使用 `/api/agents/{agent_id}/threads/{thread_id}` 下的现有 resident 接口。专业实例与默认助手遵守相同的持久受理、重连查询、指定轮次取消、历史分页和结果回流约定，详见 [私人助手会话接口](resident-agent-conversation.md)。

切换实例就是切换这两个真实标识。不同实例的历史和记忆不混合；委派 Task 保存发起实例及来源 thread，成果回到原处。项目仍独立保存工作环境，Task 仍是独立执行会话，创建专业实例不关联或修改项目。

专业对话默认保持纯文字能力。每轮消息可明确授权只读查询当前实例记忆和已委派任务，或选择本轮可检索的知识库。只有明确提供 `task_context` 时，模型才能通过宿主工具创建这一轮唯一的 Task；项目、附件、模型、设备和执行选项均来自用户提供的上下文，模型只能整理任务说明。模板方法不会增加这些权限，也不开放 Shell、MCP 或任意文件访问。

基本记忆管理与原始记忆采集开关按实例独立管理；模板创建或更新不自动打开采集，也不自动把候选内容变成长期事实。只读记忆工具也不能保存、修改或推广记忆。

## 归档和恢复

归档请求包含 `archived: true` 和当前 `expected_revision`；恢复使用 `archived: false` 和最新修订。其他用户的实例返回 404。归档时仍有排队或运行中的私人会话轮次，返回 `AGENT_BUSY`（409），应先等待完成或明确取消该轮。

归档阻止新对话、新委派和新关注，历史与记忆保留供读取。实例的所有关注授权被取消并递增授权版本，旧版本尚未发出的通知失效。恢复使用原 Agent 和 thread，不恢复过去的关注授权。

已经委派的 Task 独立继续，归档不取消、重建或删除 Task。终态成果仍可作为来源会话的历史事实保存，不因此启动助手模型。归档、恢复和模板采用都使用同一个实例修订，避免多个页面相互覆盖。

普通 Agent 配置接口不能把这些实例共享、转移、改成团队或删除；普通 session reset 和 thread 删除也不能替换常驻身份。需要恢复服务时使用显式恢复接口，不通过创建同名 Agent 代替。
