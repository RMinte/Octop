# 私人智能体常驻会话：服务端接入说明

本说明对应 2026 年 10 月 20 日的服务端文字会话首期实现。客户端可通过六个已认证的 REST 接口建立私人身份、发送文字、查询受理结果、读取最近会话快照和停止指定轮次。本轮没有修改 AgentComputer 客户端。

完整能力汇总见[服务端能力与接入顺序](resident-server-capabilities.md)。专业实例与授权主动跟进现已追加；下述默认身份仍唯一，专业实例使用各自独立的 Agent/thread。

首版闭环现已追加[历史与基本记忆接口](resident-history-memory.md)和[任务委派与成果回流](resident-delegation-results.md)。下列六个接口继续保留原职责；统一历史使用新增 `/conversation/history`，显式委派和记忆管理使用各自新增入口。

## 领域关系与本期范围

| 对象 | 职责与关系 |
| --- | --- |
| 智能体 | 用户自己的长期助手实例，持有独立 Agent 身份与常驻会话绑定；每个用户只有一个 `primary` 绑定，另可创建彼此隔离的专业实例。 |
| 专家模板 | 可复用的专业身份与方法。模板和实际 Agent 实例不是同一个对象；本期初始化不创建或发布专家模板。 |
| 会话 | 由 `thread_id` 标识，归属于该用户和私人 Agent；跨页面、跨项目、重连和重启沿用原绑定。 |
| 项目 | 独立保存工作目录、行动指南及能力引用，不因私人智能体初始化而创建或修改。 |
| 任务 | 独立的 `project_tasks` 多轮工作记录。私人会话本身不是 Task；显式委派通过独立关联记录创建普通 Task，并把其结果回到来源 thread。 |

私人实例复用现有 `agents`，内部 `kind="expert"` 用于沿用现有实例类型约束；这不代表发布了专家，不创建 `published_experts`，也不把项目或 Task 绑定到专家。初始化创建专用私人实例，不从用户已有专家中挑选第一个或最近使用者。

对话正文支持普通文字；专用实例默认关闭执行工具、Skill、MCP、对话附件、斜杠指令、子代理、团队、自动长期事实沉淀和随机关怀。可选的本轮宿主只读工具和单任务委派授权见能力汇总；只在自然语言中提出工具要求不会授予额外能力。当前可通过独立接口管理手工记忆并有界召回，明确委派的工作由普通 Task 执行。模型会话上下文和检查点用于追问，与长期记忆分别管理。既有普通专家和 Task 的能力不因这些限制而关闭。

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
| PUT | `/api/agents/resident-conversation` | `200 ResidentBinding`，幂等初始化。 |
| GET | `/api/agents/{agent_id}/threads/{thread_id}/conversation?limit=50` | `200 ResidentSnapshot`，最近轮次的完整快照。 |
| POST | `/api/agents/{agent_id}/threads/{thread_id}/messages` | `202 ResidentTurn`，持久受理或返回原请求当前状态。 |
| GET | `/api/agents/{agent_id}/threads/{thread_id}/requests/{request_id}` | `200 ResidentTurn`，查询原发送请求。 |
| POST | `/api/agents/{agent_id}/threads/{thread_id}/cancel` | `200 ResidentTurn`，记录对指定轮次的取消意图。 |

### 读取与建立绑定

`GET` 没有副作用。尚未建立绑定时仍返回 200：

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

`PUT` 无需请求体，也不要求 `Idempotency-Key`。它按当前用户的 `primary` 身份幂等初始化；并发首次调用只能建立一个绑定。重复调用返回同一组 Agent/thread ID，不重置会话。初始化失败后再次调用使用原身份继续初始化，不自动创建另一个实例。

```http
PUT /api/agents/resident-conversation
Authorization: Bearer <access_token>
```

成功示例中的名称和 ID 仅供说明，以服务端实际返回为准：

```json
{
  "status": "ready",
  "agent_id": "01RESIDENTAGENTEXAMPLE",
  "thread_id": "thr_01RESIDENTTHREADAMPLE",
  "name": "私人助手",
  "timezone": "Asia/Shanghai",
  "error_code": null
}
```

`ResidentBinding` 的六个字段固定如上，`status` 为 `not_created / initializing / ready / failed`；`agent_id`、`thread_id`、`name` 和 `error_code` 可为 null。绑定状态与模型是否可用分别处理：即使未配置 Provider 或没有可用模型，仍能建立私人身份和空会话；建立身份不会调用模型、下载模板或启用主动关怀。

### 发送文字与查询原请求

请求体为 `ResidentMessage`，必填以下两个字段。另支持可选的 `read_tools`、`knowledge_base_ids` 和 `task_context`，详细授权规则见[服务端能力汇总](resident-server-capabilities.md#按轮授权的助手工具)。

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `request_id` | string | 去除首尾空白后 1–200 字符，仅限 `A–Z a–z 0–9 _ -`；建议使用 UUID，每个新的用户发送操作生成一次，网络重试复用。路径分隔符、百分号和点等不接受，避免受理后无法按原 ID 查询。 |
| `prompt` | string | 1–32768 字符，必须包含非空白文字；去除开头空白后以 `/` 开始的输入不支持。 |

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
| `request_id` | string | 原用户发送请求身份。 |
| `user_message_id` | string | 宿主分配的稳定用户消息 ID，也写入正式聊天历史。 |
| `assistant_message_id` | string | 受理记录的稳定展示 ID，用于客户端合并本轮回复；不等同于 Provider/Harness 产生的正式 AI 消息 ID。 |
| `prompt`、`reply` | string | 原始用户文字与当前已保存的完整回答；`reply` 可为空或为部分正文。 |
| `status` | string enum | `queued / running / completed / failed / cancelled / interrupted`。 |
| `created_at`、`updated_at` | datetime | UTC ISO 8601 时间。 |
| `error_code`、`error_message` | string 或 null | 安全错误码及本地化说明；无错误时为 null。 |

服务端先保存受理记录，再返回 202。新请求的响应可能已经是 `queued` 或 `running`；原请求重传还可能返回其已有终态。202 表示请求已受理或找到了原受理记录，不保证模型已成功回答。

`ResidentTurn.authorization` 还会回显原发送请求中明确选择的能力，默认为未授权。发送幂等按同一 owner 的 `request_id` 处理：同一 thread、相同 `prompt` 重传返回原轮次当前状态，不增加用户消息、不重复执行。相同 ID 换 thread、文字或授权选择返回 `409 RESIDENT_REQUEST_CONFLICT`。`prompt` 保留原值，空白差异也属于输入变化；不得重试时擅自改写文字。已结束的原请求重传也不会重跑；确实需要新的对话轮次时使用新 `request_id`。

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
| 空白正文、以 `/` 开始的正文 | HTTP `422 RESIDENT_TEXT_ONLY`。 |
| 字段缺失、长度非法、额外附件/工具等请求字段、limit 越界 | HTTP 422，请求 schema 校验失败。 |
| 已受理轮次执行时模型不可用 | 轮次 `failed`，常见错误码 `RESIDENT_MODEL_UNAVAILABLE`；受理响应 202 不替代此结果。 |
| 租约失效或停服中断 | 轮次 `interrupted`，`RESIDENT_INTERRUPTED`。 |
| 未知内部异常或未识别的流错误码 | 安全映射为 `INTERNAL_ERROR`，不返回原始异常和凭据。 |

轮次错误位于 `ResidentTurn.error_code/error_message`，不表示轮询 HTTP 本身失败。例如原轮次已经 failed，其查询仍正常返回 HTTP 200。私人对话不直接执行工具审批、附件上传或客户端本机操作；这些工作使用明确委派后的普通 Task。手工长期记忆通过独立接口管理，自动采集和主动跟进仍未开放。

## 验证与未验收范围

定向集成验证覆盖初始化无读取副作用、并发绑定唯一、并发相同发送幂等、异体冲突、活动轮互斥、发送端连接关闭后继续执行、同 thread 追问、有界快照、跨用户拒绝、旧取消隔离、错误流终态，以及认证和类型化 OpenAPI。

正式链路验证仅替换 `FakeHarnessAgent.stream`，保留真实 Gateway、请求组装、身份校验和历史记录路径，核对两轮上下文连续、用户稳定消息 ID，以及旧格式和 v2 正式历史读取。它不是通过 mock 整个 processor 来证明历史正确，也不代表调用过真实外部模型。

相关验证入口：

```bash
uv run pytest tests/integration/test_resident_conversations.py -q
uv run pytest tests/unit/agents/test_resident_identity.py tests/unit/db/test_resident_requests.py -q
uv run pytest tests/integration/test_resident_runtime_recovery.py tests/unit/agents/test_resident_worker_lock.py -q
```

本轮未验证真实 Provider/模型调用、PostgreSQL 实库或 AgentComputer 客户端接线。SQLite/PostgreSQL 提供成对迁移，但 SQL 成对存在不等于 PostgreSQL 实库已经通过。历史、基本手工记忆、显式委派和可靠回报的新增验证见本页顶部两份接入说明；自动采集、模型自主委派工具、主动服务和客户端接线仍留待后续阶段。
