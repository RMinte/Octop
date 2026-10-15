# API Reference

## 独立项目和任务接口（2026-10-15）

本文分别说明 Octop 网页接口与 AgentComputer 专用协议。二者共用独立项目/多轮任务服务和数据，HTTP 入口保持各自职责。原生 App 适配和后续接口改造尚未完成。完整交付证据见 [本轮进度](agentcomputer-progress.md#2026-10-15-agentcomputer-专用协议边界修正)。

Octop 网页使用 `/api/projects` 与 `/api/project-tasks`；AgentComputer 使用专用 `/api/agentcomputer/v1/projects` 与 `/api/agentcomputer/v1/tasks`。专用协议调用同一份 `infra/projects` 服务与数据表，保留请求头 Bearer 认证、协议错误信封、能力查询和受限 Bridge 转发。旧专家绑定、UUID 准备回执、Run、事件、文档与命令回执实现不重新接入新模型；旧文字 worker 不自动派发，历史 `ac_*` 数据保留，不迁移、不兼容。保留专用路径不表示旧客户端已兼容新契约，App 仍需适配字符串 ID、snake_case 项目/任务字段及新的能力声明。

「项目和任务」页面的项目管理使用 `/api/projects`。项目自己保存配置，没有专家或 Agent 外键。创建项目不要求模型服务，不会创建专家。此前的托管专家项目原型不自动转换、不做兼容同步；已有原型数据保留。

| 接口 | 用途 | 当前验证 |
|---|---|---|
| `GET /api/projects` | 获取当前账号的最新项目列表，按最近修改排序 | 服务端自动化通过 |
| `POST /api/projects` | 新建项目，保存名称、简介、服务端/客户端工作目录、Skill/MCP 和行动指南 | 服务端自动化通过 |
| `GET /api/projects/{project_id}` | 获取项目完整配置和 `revision` | 服务端自动化通过 |
| `PATCH /api/projects/{project_id}` | 局部修改；未提交字段保持原值，以 `expected_revision` 防止覆盖修改 | 定向自动化通过 |
| `PUT /api/projects/{project_id}` | 显式完整替换；必须提交全部项目字段，局部请求返回 422 | 定向自动化通过 |
| `GET /api/projects/resources` | 获取可选 Skill、当前用户 MCP 连接和目录浏览范围 | 服务端自动化通过 |

以上接口均需登录，响应 `Cache-Control: no-store`。其他账号的项目返回 404。项目名称在账号内唯一，重名或编辑版本冲突返回 409。新建与修改仅保存配置；发送任务消息时才实际加载项目选定的 Skill/MCP。

请求字段：`name`（必填，最多128字符）、`description`、`directory`（服务端工作目录，在运行 Octop 的主机上验证，可为 null）、`client_directory`（客户端工作目录，用户电脑上的绝对路径，可为 null）、`instructions`、`skills`（多个 `{package_id, slug}`）、`mcp_servers`（多个连接引用）。`PATCH`/`PUT` 另外必填 `expected_revision`，成功后版本加一。`PATCH` 至少包含一个修改字段；省略保持原值，目录传 null 解除关联，Skill/MCP 传 [] 清空，文字字段清空使用空字符串。除两类目录外不接受 null。`PUT` 必须显式包含全部项目字段，不能用仅改名的请求清空其他配置。服务端工作目录遵守当前用户的目录访问范围，不创建、不移动、不删除目录。MCP 引用按账号校验，接口不返回连接密钥。

创建项目必须传 `Idempotency-Key` 请求头（8–128 位字母、数字、下划线或短横线，推荐 UUID）。同一账号、同一标识、相同规范化请求重试，返回第一次创建的完整 201 响应，即使项目之后已被编辑；同一标识换内容返回 `409 PROJECT_REQUEST_CONFLICT`。该记录与项目在同一数据库事务中保存，跨重启保留；创建失败不消耗标识。不同用户的标识互不影响。网页保存失败后保留本次内容对应的标识以便重试。创建或编辑成功后再 GET 获取最新状态。

例如改名仅发送 `PATCH /api/projects/{project_id}`，请求体 `{"name":"新名称","expected_revision":3}`；已有目录、Skill/MCP、指南都保持不变。版本过期返回 `409 PROJECT_REVISION_CONFLICT`，客户端应读取最新详情并让用户处理冲突。

两类目录独立存储。`client_directory` 支持 macOS/Linux 绝对路径、Windows 盘符绝对路径和 UNC 路径，按原字符串保存，不使用服务端的文件系统或用户根目录策略解析该路径；空值可为 null。该字段仅关联位置，不代表目录已授权访问，也不上传或同步目录文件。新建、修改、详情和列表均读写该字段，旧项目新增字段为 null。

项目通过“添加技能”打开与专家复用的技能目录：提供为你推荐、近期飙升、下载热榜、最近上新、搜索、详情和已安装技能选择。榜单复用 `GET /api/skills/hub/rankings?type=all`，搜索复用 `GET /api/skills/hub/search`。SkillHub 选择项以 `{source: "skillhub", slug, name, description}` 保存，已安装项使用 `{package_id, slug}`。两种来源可多选并回显；失败显示重试入口。保存 SkillHub 引用不会下载或执行技能，也不会创建专家。

页面每次进入、恢复到前台和手动刷新时获取服务端数据；创建或修改成功后重新获取列表和详情。项目详情默认打开任务会话，可直接输入需求创建任务、在同一任务内追问、打开历史和停止回复。项目配置保留在同名页签中。


### 任务对话接口（独立项目模型）

Task 是一段持久的多轮对话。发送首条消息时创建 Task；后续消息继续同一个 Task，无需先选择专家。`project_id=null` 代表无项目任务。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/projects/{project_id}/tasks` | 列出该项目的任务，按最近活动排序 |
| POST | `/api/projects/{project_id}/tasks` | 在项目中发送首条消息，原子创建任务和首轮，返回 202 |
| GET | `/api/project-tasks` | 列出无项目任务 |
| POST | `/api/project-tasks` | 创建任务并发送首条消息，`project_id` 可省略或为 null |
| GET | `/api/project-tasks/{task_id}` | 读取全部消息、增量回复、当前工具名称和执行状态 |
| POST | `/api/project-tasks/{task_id}/messages` | 发送下一轮消息，继续相同任务 |
| POST | `/api/project-tasks/{task_id}/cancel` | 停止排队或运行中的回复，保留已有正文 |

发送体：`{"request_id":"每次发送唯一的UUID","prompt":"用户消息"}`。通用创建接口另接受 `project_id`。网络重试必须复用 `request_id` 和同一条消息；标识与内容冲突返回 409。执行中发送新消息返回 `TASK_BUSY`，停止或结束后可继续。所有读写均按当前登录用户隔离；其他用户的资源返回 404。读取带 `Cache-Control: no-store`。

每轮采用发送时项目配置的快照：行动指南进入系统提示；服务端目录作为实际工作目录；所选已安装技能和 SkillHub 包通过 Harness 工作区加载；仅装配所选 MCP 的工具，凭据沿用连接管理。项目修改在下一轮生效，已保存轮次的 `project_revision` 保留原值。客户端目录仍然只记录路径。无项目任务使用服务端自动分配的独立工作区。

当前使用全局已配置的可用对话模型（优先启用的全局默认模型，未设时采用已有文本执行模型配置，最后回退到第一个可用模型）。未配置模型不创建空任务。执行遵循现有全局安全策略和 Token 配额；模型用量直接记在任务轮次的 `input_tokens/output_tokens` 中，并纳入用户累计配额；原专家 Token 统计页面暂未合并这部分展示。需要审批的工具当前会结束本轮并提示失败，不绕过审批；本轮提供文字对话、项目文件/脚本、Skill 和所选 MCP；浏览器、桌面、审批恢复和附件上传未接入。

状态为 `queued/running/completed/failed/cancelled`。运行时约每 0.5 秒保存增量正文；网页约每秒读取当前状态。已完成轮次的用户消息和回复用作后续上下文，不自动重放工具操作。取消不会回滚已发生的工具副作用；优雅停服标记中断，进程异常退出后约 90 秒释放失效执行，不自动重试。回复失败不向客户端返回原始模型/MCP 异常或凭据。

验证：任务 HTTP/持久化/多轮/幂等/用户隔离/取消/崩溃恢复集成用例通过；真实 Harness 配合本地模拟模型完成项目文件读取及模拟 MCP 调用，并验证 Skill/行动指南进入提示。8088 已登录正式页面使用现有 GLM-5 完成项目内首轮、同 Task 追问及刷新后历史恢复；无项目任务同样返回真实回复。真实 MCP 供应商连接、PostgreSQL 与 Ubuntu 盒子尚未实机点验。

### AgentComputer 专用协议与接入顺序

AgentComputer 直连运行 Octop 的盒子/主机，统一使用 `/api/agentcomputer/v1` 前缀。下面的路径均相对此前缀；网页仍使用上文的 `/api/projects`、`/api/project-tasks`。共享的是服务和数据库，不要求 App 改用网页入口。

| 方法 | 专用协议路径 | 用途 |
| --- | --- | --- |
| GET | `/capabilities` | 确认盒子身份、协议契约、当前功能和执行可用性 |
| GET / POST | `/projects` | 列出 / 创建同一份独立项目；创建需要 `Idempotency-Key` |
| GET | `/projects/resources` | 读取项目可选目录、Skill、MCP |
| GET / PATCH / PUT | `/projects/{project_id}` | 读取 / 局部修改 / 完整替换项目配置 |
| GET / POST | `/projects/{project_id}/tasks` | 项目任务列表 / 首条消息创建任务 |
| GET / POST | `/tasks` | 无项目任务列表 / 创建任务并发送首条消息，创建体可指定 `project_id` |
| GET | `/tasks/{task_id}` | 当前任务、全部历史和增量回复 |
| GET / POST | `/tasks/{task_id}/turns` | 读取全部轮次 / 同一任务发送下一轮 |
| POST | `/tasks/{task_id}/cancel` | 停止当前排队或运行中的回复 |

共 15 个方法与路径组合，均有类型化请求/响应和 OpenAPI 说明。项目、任务、轮次请求与响应使用上文的共享模型；项目列表、任务列表和历史轮次直接返回数组。发送体为 `{"request_id":"每次发送唯一的UUID","prompt":"用户消息"}`，创建 `/tasks` 可另传 `project_id`。

1. 使用一个 Octop 地址登录，保存该实例返回的用户 token。所有专用协议请求携带 `Authorization: Bearer <access_token>`；禁止 `access_token` 查询参数。收到 `X-Octop-Access-Token` 时更新本地令牌。
2. 先读取 `/capabilities`，确认 `contract="independent-project-tasks"`、`contractVersion=1`，再按本节契约解析；不能继续使用旧准备专家/Run 回执模型。
3. App 打开或回前台读取 `/projects`，进入项目读取详情及 `/projects/{project_id}/tasks`。最近无项目任务读取 `/tasks`，不是全部项目任务汇总。
4. 新建项目为本次创建生成 `Idempotency-Key`；网络重试复用原标识与内容。编辑读取最新 `revision`，PATCH 携带 `expected_revision`；版本冲突先读取再处理。
5. 首条消息创建任务，后续 POST `/tasks/{task_id}/turns` 继续相同任务。每条新消息生成新的 `request_id`，网络重试复用原标识和原消息。
6. 轮询 `/tasks/{task_id}` 恢复增量正文和当前状态；打开历史只读取，不重新发送。时间字段为 UTC ISO 8601，界面使用能力响应中的服务端 `timezone`。

`/capabilities` 保留稳定 `boxID`、`restoreEpoch` 和服务端时区；`executionAvailable` 根据共享服务实际选用的模型与 worker 状态计算。缺模型或 worker 未就绪分别给出 `unavailableReason=modelUnavailable/runtimeUnavailable`，实际发送还会校验本人配额和项目资源。`capabilities.projects/taskConversations/incrementalReplies/cancellation=true` 表示结构支持；`events/files/approvals/questions/commandReceipts/runs=false`，不表示旧 Run、文件或审批能力开放。

专用错误格式为 `{"error":{"code":"revisionConflict","retryable":false,"reasonCode":"PROJECT_REVISION_CONFLICT"}}`。`code` 使用专用协议的稳定错误分类；独立项目/任务业务原因放在 `reasonCode`，不返回原始异常或请求内容。版本冲突为 `revisionConflict`，标识冲突为 `idempotencyConflict`，任务忙为 `activeRunConflict`，缺模型为 `runtimeUnavailable`。HTTP 状态沿用业务状态（例如缺模型 409），客户端应同时读取 code/reasonCode，不能只看状态。变更请求上限为 65,536 字节。

可选 Bridge 路径为 `/api/bridge/connections/{connection_id}/agentcomputer/v1` 加上表中专用路径。仅转发本人连接与表中明确方法，远端仍以连接账号授权；创建项目转发 `Idempotency-Key`，不转发调用方凭据、任意响应头或未声明的错误详情。直接连接盒子不依赖 Bridge。

项目、任务和轮次 ID 都是不透明字符串，例如 `proj_…`、`task_…`、`turn_…`；App 应原样保存、传回，不能转成 UUID。客户端内部 UUID 不能替代服务端 ID。

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
  "prompt": "请先概括项目目标，再列出下一步。"
}
```

创建无项目任务时，仍使用上述入口，把 `project_id` 传 null 或省略。也可使用 `POST /api/projects/{project_id}/tasks`，此时请求体只含 `request_id` 和 `prompt`，不再重复传 `project_id`。

这两个创建入口都返回 HTTP 202 和 `TaskDetail`，表示任务及首轮已保存并排队，不表示模型已执行完成。执行不要求预先创建专家或单独创建会话。

继续同一任务使用 `POST /api/project-tasks/task_example/messages`：

```json
{
  "request_id": "a803c45d-c397-4a3f-b1a7-91bd8ca642a2",
  "prompt": "继续细化第二步。"
}
```

消息只支持文字：`prompt` 去首尾空白后为 1–32768 字符，`request_id` 为 8–128 位字母、数字、短横线或下划线。当前请求不接受附件、模型选择、权限模式或临时 Skill/MCP 字段；任务继承发送时的项目配置和当前模型选择规则。

通过 `GET /api/project-tasks/task_example` 读取 `TaskDetail`。以下是单轮已完成时的示意，创建响应的轮次通常处于 queued：

```json
{
  "task_id": "task_example",
  "project_id": "proj_example",
  "title": "请先概括项目目标，再列出下一步。",
  "active_turn_id": null,
  "created_at": "2026-10-15T05:01:00Z",
  "updated_at": "2026-10-15T05:01:05Z",
  "turns": [
    {
      "turn_id": "turn_example",
      "request_id": "9fca322b-4884-4acd-bdfd-67428aeac735",
      "prompt": "请先概括项目目标，再列出下一步。",
      "reply": "项目目标是……下一步是……",
      "status": "completed",
      "error_code": "",
      "activity": "",
      "model_ref": "provider/model",
      "input_tokens": 120,
      "output_tokens": 80,
      "project_revision": 1,
      "created_at": "2026-10-15T05:01:00Z",
      "updated_at": "2026-10-15T05:01:05Z"
    }
  ]
}
```

任务列表返回 `TaskSummary[]`，即上例移除 `turns` 后的字段；没有 `status` 或最近回复摘要。要展示某轮完整状态和正文，当前必须读详情。

| 轮次状态 | 含义 | 当前客户端处理 |
| --- | --- | --- |
| `queued` | 已保存，等待执行 | 显示排队，继续读取详情 |
| `running` | 正在生成回复或调用工具 | 使用已保存的 reply 和 activity 展示进度 |
| `completed` | 本轮完成 | 停止本轮轮询，可以继续发送下一条消息 |
| `failed` | 本轮失败或中断 | 保留已存正文，读取 error_code，不自动重发工具操作 |
| `cancelled` | 本轮已取消 | 保留历史，可以继续发送消息 |

`active_turn_id=null` 表示当前没有活动轮次，不等于该任务永久结束。Task 仍是一段可以继续的会话。`activity` 只是当前工具名，结束后会清空，不能用它生成完整工具历史。

停止使用 `POST /api/project-tasks/{task_id}/cancel`，当前无请求体，返回 HTTP 200 和任务详情；请求停止后应继续读取状态，直到活动轮次结束。它目前按任务的当前轮次取消，尚未加入目标 turn_id 校验；客户端不能给延迟到达或跨轮次的取消请求做自动重试。精确取消仍是后续改造项。

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
| 409 / `TASK_REQUEST_CONFLICT` | 消息标识已用于不同内容或作用域 | 核对原发送结果，禁止换内容复用标识 |
| 409 / `TASK_MODEL_UNAVAILABLE` | 没有可用模型 | 保留草稿，配置模型后重新发送；不会创建空任务 |
| 409 / `CONTROL_PLANE_BUSY` | 任务 worker 尚不可受理 | 保留发送标识和草稿，待服务恢复后重试 |

创建项目丢失响应时，用原 key 和原内容重试。PATCH 丢失响应后先 GET 核对：原请求可能已成功，重复旧 revision 会返回冲突；不能不经核对就换成最新 revision 覆盖。任务创建/发送丢失响应时，用原 request_id 和原消息重试，不先创建第二个任务。

### 已验证范围与未交付项

| 范围 | 当前证据 |
| --- | --- |
| 项目创建/读取/修改、PATCH 保留未提交字段、空值规则、版本冲突 | 服务端定向自动化通过 |
| 同 key 并发创建、不同内容冲突、用户隔离、首次响应回放、失败不消耗 key | 服务端定向自动化通过；创建回执持久化在数据库 |
| AgentComputer 专用协议和 Bridge | 15 个专用操作及对应受限转发已接共享服务；旧专家绑定/Run/文档协议未挂载；定向 HTTP/OpenAPI 验证，未部署到 8088 |
| 项目与无项目任务、首轮和追问、幂等发送、历史恢复、取消基础行为 | 现有新模型自动化通过；本轮未改精确取消语义 |
| 正式网页创建重试复用 key、改用 PATCH、保存后重新读取 | 前端自动化通过 |
| 本机 8088 | 已更新到 schema 32；升级前已备份，既有项目/任务/轮次及旧 ac_* 数据核对保留；真实 GLM 双轮记录来自前一轮 |
| 当前原生 AgentComputer App | 本轮未修改，尚未验收新独立接口适配 |
| PostgreSQL、目标 Ubuntu 盒子、真实 MCP 供应商 | 迁移文件/接口实现不等于实机验证，目前未验收 |

专用协议边界修正：项目/任务、专用协议、Bridge、隧道策略、OpenAPI、认证与异常处理共 75 项通过；严格 mypy 8 个相关源文件和 Ruff/格式检查通过，隔离 `/api/docs` 返回 200。未重启或部署现有 8088，原生 App 未改动。此前网页接口迭代：项目/任务、路由契约、OpenAPI 与隧道策略 55 项通过；正式网页 11 项通过；i18n、严格 mypy、Ruff、目标 ESLint、前端类型检查和构建通过。扩展数据库/备份检查为 528 passed、6 failed，6 项在改造前快照复现；完整 `make all` 按约定后置，不宣称全套通过。

后续仍需改造：按目标轮次精确取消、项目/任务分页及摘要、分页消息与可续接事件流、失败/取消后的上下文与重试语义、任务执行选项、任务改名和归档、附件产物与审批。当前 `/api/agentcomputer/v1/capabilities` 明确声明独立模型能力，不能按历史能力结构推断旧 Run/文档/审批协议可用。

专用协议实现为 [AgentComputer 路由](../src/octop/api/routers/agentcomputer_projects.py)、[Bridge 转发](../src/octop/api/routers/agentcomputer_project_bridge.py)；网页对应实现为 [`projects` 路由](../src/octop/api/routers/projects.py)、[项目请求模型](../src/octop/infra/projects/models.py)、[项目服务](../src/octop/infra/projects/service.py)、[任务服务](../src/octop/infra/projects/tasks.py)；验证依据为[项目集成用例](../tests/integration/test_projects.py)、[任务集成用例](../tests/integration/test_project_tasks.py)、[公开路由契约用例](../tests/integration/test_project_api_contract.py)。


Every route is mounted under `/api`. JSON unless otherwise noted; SSE
endpoints emit `text/event-stream`; chat turns use a WebSocket
described in [Chat (WebSocket)](#chat-websocket).

> **Interactive docs** (Scalar) live at `/api/docs` when
> `enable_api_docs=true` (or `OCTOP_ENABLE_API_DOCS=1`). The schema
> is at `/api/openapi.json`.

网页项目和任务接口在 `/api/docs` 的 `projects` 分组中，AgentComputer 专用接口和对应 Bridge 在 `agentcomputer` 分组中。下面独立模型的自动化证据与历史原型测试分开；旧 25 条接口的测试记录不表示当前接口或原生 App 已验收。

## Authentication

AgentComputer 直接连接一个 Octop，使用 `POST /api/auth/login` 返回的用户 Bearer token 调用 `/api/agentcomputer/v1` 专用接口，不创建专家，也不依赖第二个 Octop。Bridge 是可选转发方式。响应中的 `X-Octop-Access-Token` 是滑动续期令牌，客户端收到后应更新保存的 token。项目和任务 ID 是不透明字符串，不应转换为 UUID。

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

Skill copy operations create snapshots; they do not keep the source and
destination synchronized after the request completes.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
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
| `PUT`    | `/agents/{aid}/skills/{slug}` | owner | enable / disable a skill |
| `GET`    | `/agents/{aid}/skills/hub/search` | user | Skill Hub search |
| `GET`    | `/agents/{aid}/skills/hub/rankings` | user | Skill Hub rankings |
| `POST`   | `/agents/{aid}/skills/hub/install` | owner | body `{slug, version?}` → `201` |
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
| `GET`    | `/usage/summary` | user | token usage summary for the current user |
| `GET`    | `/admin/usage` | admin | global token usage summary |
| `GET`    | `/admin/overview` | admin | `{user_count, agent_count, ...}` |
| `GET`    | `/admin/audit-log` | admin | recent audit rows |
| `GET`    | `/admin/metrics` | admin | `{messages_total, stream_errors_total, cron_runs_total, cron_errors_total, agent_active, ...}` |

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
