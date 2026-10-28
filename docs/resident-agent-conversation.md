# 私人智能体常驻会话：服务端接入说明

本说明以 2026 年 10 月 21 日的服务端会话实现为基础，已补齐内置搜索、图片与普通文件输入及成果下载。服务端自动准备用户的唯一管家，客户端读取绑定后即可发送文字、查询受理结果、读取最近会话快照和停止指定轮次；无需用户创建或配置管家，也无需客户端先调用初始化接口。本轮没有修改 AgentComputer 客户端。

完整能力汇总见[服务端能力与接入顺序](resident-server-capabilities.md)。用户通过唯一 `primary` 身份与生活工作管家持续交流，专业工作通过普通 Task 委派，结果回到同一会话；授权主动跟进使用独立接口。

首版闭环现已追加[历史与基本记忆接口](resident-history-memory.md)和[任务委派与成果回流](resident-delegation-results.md)。下列六个接口继续保留原职责；新 primary 消息默认支持自然语言任务委派，统一历史使用新增 `/conversation/history`，显式委派与记忆管理仍保留各自入口。

2026-10-23 增加正文实时观察：`GET /api/agents/resident-capabilities` 的 `conversation_stream` 声明共享 `/api/project-tasks/ws` 和协议版本 3。通过 `resident.subscribe` 订阅已保存的累计正文和状态，使用 `resident.snapshot` 建立、更新及恢复最近 50 轮快照；发送、原请求查询和取消仍使用以下 REST 接口。`ResidentTurn` 的 `revision` 统一出现在 REST 回执、快照和历史，用于阻止迟到旧状态覆盖新正文。完整接入与验收见 [App 共用正文 WebSocket 交接](agentcomputer-conversation-stream-handoff.md)，本轮未修改客户端。

## 领域关系与本期范围

| 对象 | 职责与关系 |
| --- | --- |
| 智能体 | 用户自己的统一生活工作管家，持有稳定 Agent 身份与常驻会话；每个用户只有一个 `primary` 绑定。 |
| 专家 | 提供专业方法的既有能力，独立于管家身份。管家不通过专家模板创建不同助手，委派也不把项目或 Task 绑定到专家。 |
| 会话 | 由 `thread_id` 标识，归属于该用户和私人 Agent；跨页面、跨项目、重连和重启沿用原绑定。 |
| 项目 | 独立保存工作目录、行动指南及能力引用，不因私人智能体初始化而创建或修改。 |
| 任务 | 独立的 `project_tasks` 多轮工作记录。私人会话本身不是 Task；管家按工作请求调用专用工具，或通过显式委派接口创建普通 Task，独立关联记录把结果回到来源 thread。 |

私人实例复用现有 `agents`，内部 `kind="expert"` 用于沿用现有实例类型约束；这不代表发布了专家，不创建 `published_experts`，也不把项目或 Task 绑定到专家。初始化只创建唯一管家，不从用户已有专家中挑选第一个或最近使用者，也不创建专业助手集合。

对话支持普通文字、图片与文件输入。附件通过统一任务附件接口上传，发送时提供 `attachment_ids`，回执通过 `attachments` 返回；委派任务发布的文件通过回报 `artifacts` 提供鉴权下载，完整约定见[能力对照](resident-task-capabilities.md)。新 primary 消息默认提供 `resident_create_task` 和本会话委派任务查询工具；用户说出工作需求即可由模型按语义判断是否派发，闲聊和咨询直接回答，信息不足先追问。无需客户端先提交 `task_context`；省略时固定为无项目普通 Task，使用任务自己的独立工作空间，不附加客户端工具、附件、Skill、MCP、插件、子代理、知识库、长期记忆或团队授权。实际执行继续遵守普通 Task 的权限与审批规则，独立工作空间不等于操作系统沙箱。

管家会话支持内置网络搜索、直接图片输入及专用工具只读本会话上传文件；不直接开放 shell、任意文件访问、MCP 或电脑操作工具。斜杠指令、自动长期事实沉淀和随机关怀仍未开放。额外私人记忆查询、知识库选择和可选 `task_context` 的项目、设备、附件范围见能力汇总，不从历史提及或默认配置继承。当前可通过独立接口管理手工记忆并有界召回。模型会话上下文和检查点用于追问，与长期记忆分别管理。既有普通专家和 Task 的能力不因这些限制而关闭。

## 认证、身份与时间

- 六个接口均使用 `Authorization: Bearer <access_token>`，响应带 `Cache-Control: no-store`。
- 资源仅限当前用户自己的私人 Agent 与绑定 thread。未知资源、其他用户的资源或不匹配的 Agent/thread 返回 `404 NOT_FOUND`；管理员也不能借这些入口读取其他用户的私人绑定。
- `agent_id`、`thread_id`、`turn_id` 和消息 ID 都是服务端字符串，按原值保存，不能假定它们是 UUID。
- `created_at`、`updated_at` 是带 UTC 时区的 ISO 8601 时间。展示时间使用绑定响应中的服务器 `timezone`，其来源为 `default_timezone`；不以用户电脑的本地时区替代。
- 错误文案沿用用户语言偏好和请求语言解析。客户端应按 `error_code` 判断行为，不解析本地化文案。

## 六个接口

以下路径均为普通 `/api` 接口，不使用 `/api/agentcomputer/v1` 或 Bridge 转发。

| 方法 | 路径 | 成功响应 |
| --- | --- | --- |
| GET | `/api/agents/resident-conversation` | `200 ResidentBinding`，只读取绑定。 |
| PUT | `/api/agents/resident-conversation` | `200 ResidentBinding`，显式幂等重试或修复，不是客户端首次使用的前置步骤。 |
| GET | `/api/agents/{agent_id}/threads/{thread_id}/conversation?limit=50` | `200 ResidentSnapshot`，最近轮次的完整快照。 |
| POST | `/api/agents/{agent_id}/threads/{thread_id}/messages` | `202 ResidentTurn`，持久受理或返回原请求当前状态。 |
| GET | `/api/agents/{agent_id}/threads/{thread_id}/requests/{request_id}` | `200 ResidentTurn`，查询原发送请求。 |
| POST | `/api/agents/{agent_id}/threads/{thread_id}/cancel` | `200 ResidentTurn`，记录对指定轮次的取消意图。 |

### 服务端自动准备与读取绑定

管家身份由服务端用户生命周期自动准备：`UserManager` 启动时为已有启用用户补齐唯一 `primary` 绑定；新用户创建、邀请注册、SSO/LDAP 接入，以及用户启用或成功登录时，均幂等准备同一身份。重复准备沿用原 Agent/thread，不清空历史，不创建第二位管家。这一步不调用模型或执行任务，也不授予记忆采集、主动跟进或额外执行资源权限；自然语言委派仅在后续新消息受理时冻结为该轮能力。

客户端正常接入只需调用 `GET /api/agents/resident-conversation`，取得 `ready` 绑定后进入原会话。`GET` 始终只读，不因读取而创建资源。自动准备可能失败，不能将 GET 的 HTTP 200 等同于绑定一定可用；客户端仍应检查 `status` 与 `error_code`。尚未建立绑定时，读取仍返回 200：

```json
{
  "status": "not_created",
  "agent_id": null,
  "thread_id": null,
  "name": null,
  "timezone": "Asia/Shanghai",
  "error_code": null
}
```

`PUT` 保留为显式运维重试或修复入口，无需请求体，也不要求 `Idempotency-Key`，客户端首次使用不依赖它。它按当前用户的 `primary` 身份幂等准备；并发调用只能建立一个绑定。重复调用返回同一组 Agent/thread ID，不重置会话。初始化异常会保留 `failed` 状态及错误码；修复原因后，服务重新启动、用户成功登录或显式调用 PUT 均复用原身份继续准备，不自动创建另一个实例。

```http
PUT /api/agents/resident-conversation
Authorization: Bearer <access_token>
```

正常自动准备后，GET 返回以下结构；名称和 ID 仅供说明，以服务端实际返回为准：

```json
{
  "status": "ready",
  "agent_id": "01RESIDENTAGENTEXAMPLE",
  "thread_id": "thr_01RESIDENTTHREADAMPLE",
  "name": "生活工作管家",
  "timezone": "Asia/Shanghai",
  "error_code": null
}
```

`ResidentBinding` 的六个字段固定如上，`status` 为 `not_created / initializing / ready / failed`；`agent_id`、`thread_id`、`name` 和 `error_code` 可为 null。绑定状态与模型是否可用分别处理：即使未配置 Provider 或没有可用模型，仍能建立私人身份和空会话；建立身份不会调用模型、下载模板或启用主动关怀。

### 发送文字与查询原请求

请求体为 `ResidentMessage`，文字消息使用以下两个字段；附件消息还支持 `attachment_ids`，有图片或文件时可省略 `prompt` 或传空字符串。只传它们即可普通交流，并由模型按工作需求自主判断是否委派普通 Task；默认可查询本会话委派任务。另支持可选的 `read_tools`、`knowledge_base_ids` 和 `task_context`，用于额外记忆查询、知识库与高级执行范围选择，不是默认委派的前置条件，详细规则见[服务端能力汇总](resident-server-capabilities.md#按轮授权的助手工具)。

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `request_id` | string | 去除首尾空白后 1–200 字符，仅限 `A–Z a–z 0–9 _ -`；建议使用 UUID，每个新的用户发送操作生成一次，网络重试复用。路径分隔符、百分号和点等不接受，避免受理后无法按原 ID 查询。 |
| `prompt` | string | 最多 32768 字符；没有附件时必须包含非空白文字；去除开头空白后以 `/` 开始的输入不支持。 |
| `attachment_ids` | string[] | 默认空数组；最多 9 个当前用户上传的附件 ID，受理后绑定本私人会话。 |

```http
POST /api/agents/01RESIDENTAGENTEXAMPLE/threads/thr_01RESIDENTTHREADAMPLE/messages
Authorization: Bearer <access_token>
Content-Type: application/json

{"request_id":"e1842840-23a6-4015-a60b-97b53d9698a0","prompt":"请记住本次对话的代号是 ORCHID。"}
```

示例 `ResidentTurn`：

```json
{
  "turn_id": "01RESIDENTTURNEXAMPLE",
  "revision": 1,
  "request_id": "e1842840-23a6-4015-a60b-97b53d9698a0",
  "user_message_id": "resident:01RESIDENTTURNEXAMPLE:user",
  "assistant_message_id": "resident:01RESIDENTTURNEXAMPLE:assistant",
  "prompt": "请记住本次对话的代号是 ORCHID。",
  "reply": "",
  "status": "queued",
  "created_at": "2026-10-20T02:00:00Z",
  "updated_at": "2026-10-20T02:00:00Z",
  "error_code": null,
  "error_message": null
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `turn_id` | string | 服务端受理轮次的稳定身份。 |
| `revision` | integer，≥1 | 此轮单调递增的持久版本；REST 回执、WebSocket 快照和历史共用，用于拒绝迟到旧状态。 |
| `request_id` | string | 原用户发送请求身份。 |
| `user_message_id` | string | 宿主分配的稳定用户消息 ID，也写入正式聊天历史。 |
| `assistant_message_id` | string | 受理记录的稳定展示 ID，用于客户端合并本轮回复；不等同于 Provider/Harness 产生的正式 AI 消息 ID。 |
| `prompt`、`reply` | string | 原始用户文字与当前已保存的完整回答；仅附件消息的 `prompt` 可为空，`reply` 可为空或为部分正文。 |
| `attachments` | array | 本轮上传图片或文件的 `TaskInputAttachment` 元数据，文字消息为空数组。 |
| `status` | string enum | `queued / running / completed / failed / cancelled / interrupted`。 |
| `created_at`、`updated_at` | datetime | UTC ISO 8601 时间。 |
| `error_code`、`error_message` | string 或 null | 安全错误码及本地化说明；无错误时为 null。 |

服务端先保存受理记录，再返回 202。新请求的响应可能已经是 `queued` 或 `running`；原请求重传还可能返回其已有终态。202 表示请求已受理或找到了原受理记录，不保证模型已成功回答。

`ResidentTurn.authorization` 回显原发送请求中的额外显式选择；自然语言委派和本会话任务查询以受理时冻结的能力配置为准，不要求额外字段为 true。升级前已受理的旧请求保留原能力，不因恢复或重传而升级。发送幂等按同一 owner 的 `request_id` 处理：同一 thread、相同 `prompt`、附件 ID 和授权重传返回原轮次当前状态，不增加用户消息、不重复执行或创建任务。相同 ID 换 thread、文字、附件或授权选择返回 `409 RESIDENT_REQUEST_CONFLICT`。`prompt` 保留原值，空白差异也属于输入变化；不得重试时擅自改写文字。已结束的原请求重传也不会重跑；确实需要新的对话轮次时使用新 `request_id`。

同一常驻 thread 最多有一个 `queued/running` 轮次。活动轮未结束时，另一个新的发送请求返回 `409 AGENT_BUSY`；相同发送请求的幂等重传不受此限制。

查询受理结果使用发送时的原请求 ID：

```http
GET /api/agents/01RESIDENTAGENTEXAMPLE/threads/thr_01RESIDENTTHREADAMPLE/requests/e1842840-23a6-4015-a60b-97b53d9698a0
Authorization: Bearer <access_token>
```

返回同一 `ResidentTurn` 结构；例如完成后 `status="completed"`，`reply` 是完整正文。查询不启动模型。若发送响应丢失，先查原 ID；确实没有受理记录时，可用原 ID 和完全相同的正文重发，不能换新 ID 猜测重试。

### 读取最近轮次快照

```http
GET /api/agents/01RESIDENTAGENTEXAMPLE/threads/thr_01RESIDENTTHREADAMPLE/conversation?limit=50
Authorization: Bearer <access_token>
```

`ResidentSnapshot`：

```json
{
  "agent_id": "01RESIDENTAGENTEXAMPLE",
  "thread_id": "thr_01RESIDENTTHREADAMPLE",
  "revision": 8,
  "active_turn_id": null,
  "has_earlier": false,
  "turns": []
}
```

`turns` 中每项为前述 `ResidentTurn`，按受理先后正序排列，包含最近 `limit` 轮。示例省略了轮次内容；真实回复会包含当前窗口内的记录。`limit` 默认为 50，范围为 1–100，非法值返回 422。`active_turn_id` 为当前排队/执行轮次，没有活动轮时为 null。

`revision` 与这份快照来自同一读取视图，会随受理、正文和状态更新而变化，可用来丢弃迟到的旧快照。轮询返回整份快照和每轮完整 `reply`，不是 token 增量；不要把收到的 `reply` 再追加到本地正文。

`has_earlier=true` 仅说明窗口之前还有受理轮次。这个快照没有分页游标，增大 `limit` 也最多返回最近 100 轮。更早轮次、按日查询、搜索和任务回报通过新增 `/conversation/history` 获取，不能只用这个受理窗口拼成完整历史。

本期没有 resident WebSocket 或 SSE。客户端可在有活动轮时串行轮询快照，恢复前台或恢复网络后重新读取；按原发送 ID 查询某一轮也不触发重跑。避免同一查询多个并行轮询后按到达顺序覆盖新状态。

### 取消指定轮次

`ResidentCancel` 请求使用一个独立的取消意图 ID：

```http
POST /api/agents/01RESIDENTAGENTEXAMPLE/threads/thr_01RESIDENTTHREADAMPLE/cancel
Authorization: Bearer <access_token>
Content-Type: application/json

{"request_id":"2631db6b-cc7e-4569-9eb3-03068c045811","turn_id":"01RESIDENTTURNEXAMPLE"}
```

`request_id` 是本次取消操作的幂等身份，字符和长度规则同发送 ID；`turn_id` 是明确要停止的轮次，长度为 1–200 字符。取消请求和用户发送请求分别记账，客户端应分别生成并保存操作 ID。重复同一取消 ID 和目标安全返回；同一取消 ID 换目标返回 `409 RESIDENT_REQUEST_CONFLICT`。

响应为被取消目标的 `ResidentTurn`，其中 `request_id` 仍是原用户发送 ID，不是取消意图 ID。排队轮次可立即变为 `cancelled`；执行中先保存取消意图，响应可能仍为 `running`，随后查询原发送 ID 或快照确认终态。已有部分 `reply` 保留，不回滚已完成事实。

对已经结束的目标取消时返回其原有状态，不把完成改为取消。旧取消请求重传只作用于旧 `turn_id`，不会停止后来新发的一轮。取消不是一条新用户消息。

## 模型选择与执行恢复

每个新的发送请求在受理时解析并保存模型引用，顺序为：

1. 私人 Agent 已设置且当前可用的默认模型（实例字段优先于配置字段）。
2. 当前用户偏好中已设置且可用的模型。
3. 全局选中的可用模型。
4. 可用 Provider 列表中的第一个可聊天模型。

内部受理配置仅保存 `model_ref` 和实例更新时间，不保存 API 密钥，不在这些 DTO 中返回 Provider 凭据。执行时再次检查已保存模型是否可用；绑定 ready 不代表模型可用。没有可用模型的轮次会失败，客户端读取 `error_code/error_message`，配置好模型后再由用户发起新一轮，原请求不自动重跑。

后台执行独立于发送 HTTP 连接。关闭页面、断网或关闭发送端连接不会取消已受理轮次；其他设备使用同一绑定与请求 ID 可以查询已保存结果。

运行租约为 30 秒，正常执行持续续约。服务进程崩溃后，已领取而失去续约的轮次在租约到期后变为 `interrupted`，保留已保存的部分正文，不自动重跑原消息。若已保存取消意图，则回收为 `cancelled`。尚未领取的 `queued` 请求仍可被后台 worker 领取；它们没有已派发模型调用需要重放。优雅停服也会结束本进程活动执行并保留中断事实。

首期要求单主机、同一 `OCTOP_HOME` 只运行一个 Octop 服务进程。启动时取得数据目录的操作系统生命周期锁；第二个进程会以 `RESIDENT_WORKER_CONFLICT` 拒绝启动，暂停或超过 30 秒也不会让另一个进程抢占该锁。锁在全部活动执行结束后或进程退出时释放，不能手动删除锁文件来绕过。运行中若某轮租约失效，后续已受理轮次仍等待旧图执行彻底退出后才派发，避免检查点并发写入。租约用于持久收据恢复，不能替代图执行的独占约束；跨主机共享数据库的多实例部署本期不支持。

`interrupted` 是首期对外终态，不提供 resume 或 retry 接口。复用旧发送 ID 只是取回原中断记录。需要继续时，用户明确发送新的文字和新 ID；不能在重连时自动复制上一轮请求。后续普通追问仍使用同一 thread 的已有模型上下文，不将中断解释为先前工作已成功完成。

## 错误与能力限制

HTTP 业务错误沿用标准 envelope，例如：

```json
{
  "error": {
    "code": "RESIDENT_REQUEST_CONFLICT",
    "message": "此请求编号已用于不同的会话内容或操作。",
    "details": {}
  }
}
```

| 情况 | 返回或轮次结果 |
| --- | --- |
| 缺失、无效或过期身份 | HTTP 401/403，沿用现有认证。 |
| 非本人绑定、Agent/thread 不匹配、未知发送请求或取消目标 | HTTP `404 NOT_FOUND`。 |
| 发送或取消 ID 与原内容/目标冲突 | HTTP `409 RESIDENT_REQUEST_CONFLICT`。 |
| 当前会话已有活动轮次 | HTTP `409 AGENT_BUSY`。 |
| 绑定未 ready 或私人实例未启用 | HTTP `409 AGENT_NOT_RUNNING`。 |
| 没有附件的空白正文、以 `/` 开始的正文 | HTTP `422 RESIDENT_TEXT_ONLY`。 |
| 字段缺失、长度非法、未声明的附件/工具等请求字段、limit 越界 | HTTP 422，请求 schema 校验失败。 |
| 已受理轮次执行时模型不可用 | 轮次 `failed`，常见错误码 `RESIDENT_MODEL_UNAVAILABLE`；受理响应 202 不替代此结果。 |
| 租约失效或停服中断 | 轮次 `interrupted`，`RESIDENT_INTERRUPTED`。 |
| 未知内部异常或未识别的流错误码 | 安全映射为 `INTERNAL_ERROR`，不返回原始异常和凭据。 |

轮次错误位于 `ResidentTurn.error_code/error_message`，不表示轮询 HTTP 本身失败。例如原轮次已经 failed，其查询仍正常返回 HTTP 200。私人对话的附件上传复用统一任务附件接口，文件只读工具限定当前会话；不直接执行工具审批或客户端本机操作。实际工作进入普通 Task，附加资源须由用户明确选择。长期记忆通过独立接口管理，原始采集需显式开启，候选需用户确认；主动跟进同样需要独立明确授权，详见能力汇总。

## 验证与未验收范围

定向集成验证覆盖初始化无读取副作用、并发绑定唯一、并发相同发送幂等、异体冲突、活动轮互斥、发送端连接关闭后继续执行、同 thread 追问、有界快照、跨用户拒绝、旧取消隔离、错误流终态，以及认证和类型化 OpenAPI。

正式链路验证仅替换 `FakeHarnessAgent.stream`，保留真实 Gateway、请求组装、身份校验和历史记录路径，核对两轮上下文连续、用户稳定消息 ID，以及旧格式和 v2 正式历史读取。它不是通过 mock 整个 processor 来证明历史正确，也不代表调用过真实外部模型。

相关验证入口：

```bash
uv run pytest tests/integration/test_resident_conversations.py -q
uv run pytest tests/unit/agents/test_resident_identity.py tests/unit/db/test_resident_requests.py -q
uv run pytest tests/integration/test_resident_runtime_recovery.py tests/unit/agents/test_resident_worker_lock.py -q
```

自动化验证使用受控模型，不能替代真实模型对自然语言工作请求的判断验收。尚未进行 PostgreSQL 实库或 AgentComputer 客户端接线验收；SQLite/PostgreSQL 提供成对迁移，但 SQL 成对存在不等于 PostgreSQL 实库已经通过。历史、基本手工记忆、自然语言及显式委派和可靠回报的验证见本页顶部两份接入说明；默认委派工具、额外按轮选择、原始采集与候选、主动跟进见能力汇总。客户端接线仍待后续实施。
