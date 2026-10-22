# 私人智能体：任务委派与成果回流

本说明补充[常驻会话接口](resident-agent-conversation.md)和[历史与基本记忆](resident-history-memory.md)。本轮只交付服务端，不修改 AgentComputer。用户直接在唯一管家会话中说出工作需求，模型判断是否需要独立执行，按需调用 `resident_create_task` 委派普通 Task，成果回到同一会话。客户端只需发送 `request_id/prompt`，不需要先创建任务或传 `task_context`。可选范围和额外能力见[按轮授权工具](resident-server-capabilities.md#按轮授权的助手工具)。

## 自然语言委派与测试

读取 `GET /api/agents/resident-conversation` 的 `ready` 绑定后，直接在这个管家会话里说：

> 帮我单独整理一份五个工作日的工作与生活安排，按上午、下午、晚上列出，安排完成后把结果发回这里。

客户端只发送普通消息，无需展示任务配置步骤：

```json
{
  "request_id": "steward-test-20261021-01",
  "prompt": "帮我单独整理一份五个工作日的工作与生活安排，按上午、下午、晚上列出，安排完成后把结果发回这里。"
}
```

模型根据实际需求选择是否创建 Task，不按“派发”“任务”等关键词硬触发。闲聊、知识咨询和可在会话中直接完成的问题应直接回答；缺少目标、必要资料或选定执行范围时先追问。新 primary 轮次默认可查询本会话委派任务，因此后续可直接问“刚才那项工作做到哪了”，无需开启 `read_tools`；该开关只控制额外的私人记忆检索。

未提供 `task_context` 时，服务端固定创建无项目普通 Task，使用任务自己的独立工作空间，不附加客户端工具、附件、Skill、MCP、插件、子代理、知识库、长期记忆或团队授权。实际执行仍遵守普通 Task 的现有权限与审批规则，工作空间隔离不代表操作系统沙箱。模型不能因为历史提过某项目、电脑或能力就获得相应执行范围。`task_context` 用于用户明确选择项目、设备、附件和执行选项，是可选高级配置；独立 `POST /conversation/delegations` 也继续提供。

验收时检查事实链路：

1. 查询本会话 `GET {base}/conversation/delegations`，按本次消息的 `source_turn_id` 找到真实 `task_id`，并能读取该任务；返回 202 或管家文字声称“已派发”都不能单独证明创建成功。
2. 等待对应委派 `status="completed"`，其 `reports` 出现 `delivery_status="delivered"`，再从 `GET {base}/conversation/history` 读到 `kind="task_report"` 的回报。失败或取消也应按真实终态回流，不显示为完成。
3. 在原管家会话继续说“根据刚才的结果，帮我缩减成三个重点”，确认后续交流能引用真实成果。
4. 使用原消息 `request_id` 和完全相同正文重传，确认仍是原轮次、原委派和原 Task；发起另一项工作才使用新 ID。每轮最多一个任务槽，同一工作说明的重复工具调用复用原 Task，不重复创建。

`{base}` 为 `/api/agents/{agent_id}/threads/{thread_id}`。以上测试需要服务端已有可用模型，并将新消息发给唯一 primary 管家；升级前已受理的旧消息沿用其原能力快照，不补开自然语言委派。若模型选择先追问，补齐信息后继续原管家会话；若明确工作请求长期没有真实 Task，应检查实际工具调用与模型响应，不能仅看回答措辞。

## 独立委派接口

`{base}` 表示 `/api/agents/{agent_id}/threads/{thread_id}`，所有接口需要 Bearer 登录，只操作当前用户自己的私人绑定，响应 `Cache-Control: no-store`。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `{base}/conversation/delegations` | 同事务创建普通 Task、首轮及来源关联，返回 202。 |
| GET | `{base}/conversation/delegations` | 分页查询本会话委派和结果。 |
| GET | `{base}/conversation/delegations/{delegation_id}` | 读取具体委派、原始轮及其重试链状态和回报。 |

请求示例中的 ID 均为占位示意：

```json
{
  "request_id": "a3297437-3077-4921-b6f1-6142567a51f2",
  "source_turn_id": "01RESIDENTTURN",
  "project_id": null,
  "message": {
    "request_id": "0743c983-9a83-453e-9732-d7abbab0c49a",
    "prompt": "整理已选资料，交付一份比较报告。",
    "attachment_ids": [],
    "options": {}
  }
}
```

使用本独立接口时，`source_turn_id` 必须属于当前私人会话，`project_id` 必须显式提交：选择本人项目，或传 null 创建无项目任务。该要求不适用于上文只提交 `request_id/prompt` 的自然语言消息，后者由服务端固定默认无项目范围。不会根据聊天中提到的名称、记忆或最近打开项目自动选择执行范围。`message` 是已有 `TaskMessage`，附件、模型、Skill、MCP、客户端工具及审批继续按普通 Task 的现有规则校验；`options` 的具体字段以 `/api/docs` 和普通任务接口为准，不增加另一套权限协议。

外层 `request_id` 标识用户这一次委派操作；内层 `message.request_id` 标识任务首轮。两者都应在网络重试时保持不变。同账号、同委派 ID 和完全相同的来源及消息重传，返回原委派；改变内容返回 `409 RESIDENT_REQUEST_CONFLICT`。不能用已有普通 Task 的首轮请求 ID 冒充一次新委派。创建失败不会留下没有来源的半成品任务。

只传本次工作请求所需的任务说明和明确选取的引用，不自动复制私人会话全文、系统提示、私人记忆或凭据。项目独立于智能体和专家；委派不会创建专业助手、绑定专家或修改项目配置。电脑执行仍在 Task 中进行，自然语言委派不替代 Task 的审批和目录授权。

成功响应 `ResidentDelegation`：

| 字段 | 含义 |
| --- | --- |
| `delegation_id`、`request_id` | 稳定委派身份和原操作 ID。 |
| `source_turn_id` | 来源私人会话轮次。 |
| `task_id`、`initial_turn_id` | 创建的普通任务和首个委派轮次。 |
| `latest_task_turn_id`、`status` | 原始委派轮及其显式 retry 链的最新状态；普通追加工作不进入这条链。 |
| `task_exists` | 原任务是否仍存在；任务删除后委派事实保留。 |
| `reports` | 按具体任务轮次冻结的回报。 |
| `created_at` | UTC ISO 8601 时间。 |

列表参数为 `limit`（默认 50，1–100）和 `page_cursor`。返回 `{items,next_page_cursor}`，游标绑定当前账号、实例和来源会话，不能跨作用域复用。未知或非本人绑定、来源轮次、项目或委派返回 404；原 Task 的资源、审批、忙碌等错误继续使用既有错误码。

## 回报与重试范围

初始委派轮及 `retry_of_turn_id` 显式重试链的 `completed / failed / cancelled` 会产生回报。等待、暂停和可恢复中断仍是待处理状态，不伪造完成。Task 页面普通追加的新工作及其重试不会回报原助手。

真正终态和回报 outbox 在同一控制面事务提交，覆盖工作线程结束、排队取消、等待中取消和失败路径。读取的是 SQL 最终持久状态；取消竞争不会生成一条“已完成”的假消息。每个任务轮次至多有一条逻辑回报，后来的成功或失败不会覆盖之前的回报。

`reports` 中每项包含：

- `report_id`、`message_id`、`task_id`、`task_turn_id`：稳定身份和具体结果来源。
- `status`、`text`、`error_code`：实际终态、该轮最终正文前 8000 字符及安全错误码。完整任务正文仍在原 Task 中。
- `artifacts`：该轮实际发布成果的 ID、文件名、媒体类型、字节数、摘要及资料分类。它是事实引用，不复制文件。
- `day_key`、`timezone`、`captured_at`：按委派时冻结的服务端时区确定终态发生日；以后不因客户端时区变化重新分组。
- `delivery_status`：`pending` 表示结果已持久保存、正式历史或模型上下文尚待补齐；`delivered` 表示两阶段均已确认。

任务或成果后来被删除，委派和回报继续保留，`task_exists=false` 或原成果下载不可用。不会补造文件，也不把新的 Task 接到旧引用上。取消私人助手当前轮次不取消已委派 Task；需要停止任务时，调用已有 Task 的定向取消接口。

## 可靠投递与连续追问

后台自动把回报送到原 `origin_thread_id`，不受当前页面或其他任务选择影响，不自动创建新会话。依次执行：冻结规范消息、写正式历史、写 LangGraph 上下文、标记投递完成。三个存储不属于一个数据库事务，各阶段分别记账。

回报是稳定 ID 的 `AIMessage`，带 `source=task_report` 和真实任务来源，不伪装成用户消息或没有对应调用的工具消息。第一次准备的完整消息被持久冻结，重试时语言变化也不重造另一份正文；检查点时间使用回报固定时间。

归档按稳定消息身份去重，涵盖旧格式与 v2 格式以及重启后格式变化；上下文通过 Harness 的 `aappend_messages` 和消息 reducer 按相同 ID 接入。任一阶段退出后，重启可从持久收据补齐。来源失效或暂时无法初始化模型上下文时保留 pending 和内部原因，不能换一个 thread 投递。

回报接入与正常对话共用每个 thread 的串行边界；新一轮模型执行前先消化待回报，确保后续追问能看到结果。回报本身不调用模型、不触发再次委派，也不写入私人长期记忆。仍要求单主机、同一数据目录只运行一个服务进程；停止时等已准入的写入结算后才释放执行锁。

新客户端应使用 `/conversation/history` 读取混合的用户轮次与任务回报。原 `/conversation` 仍是有界用户轮次快照，不能用它替代新增的统一历史。恢复网络后查询原委派 ID 或历史即可，不重发新委派。

## 验证与边界

### 真实模型验收（2026-10-21）

使用当前管家所选的智谱 `GLM-5.3`，在独立临时环境完成真实模型和普通 Task worker 的端到端验证。请求只包含 `request_id/prompt`，正文为“帮我单独整理一份五个工作日的工作与生活安排……安排完成后把成果发回这里”，不提供 `task_context`，也不提示模型调用具体工具。

本轮只创建一个 Task，真实执行状态为 `completed`，报告投递状态为 `delivered`，原管家统一历史中出现对应 `task_report`。Task 自行生成了最初用户提示、任务执行说明及管家首次回复中均未出现的方案代号；随后在同一管家会话追问，模型正确复述该代号，且没有再创建任务。本轮没有发生限流中断或恢复重试；正式用户的会话、任务和模型设置均未改动。

此前以系统回退的 `GLM-4.7-Flash` 测试时，模型能够创建任务，但执行阶段收到供应商 `429`（业务码 `1302/1305`），未完成回流。这两轮不记为闭环通过，也不通过改变正式默认模型来规避；上述通过记录对应实际选择的 `GLM-5.3`。

### 自动化回归

```bash
uv run pytest tests/integration/test_resident_closed_loop.py -q
uv run pytest tests/integration/test_resident_turn_tools.py -q
uv run pytest tests/unit/db/test_resident_delegations.py tests/unit/db/test_resident_closed_loop_migration.py -q
```

验证使用真实 Gateway、任务入队、Task worker 终结逻辑、数据库与归档路径，模型输出由 FakeHarness/受控 Task runner 替代，覆盖权限、幂等、记忆隔离、回报连续追问、重试范围、取消、删除、阶段丢失回执、重启去重、格式切换及关停排空。自然语言委派工具的针对性验证使用 `tests/integration/test_resident_turn_tools.py`；上述 FakeHarness 检查不替代真实模型的语义判断验收。迁移为 SQLite/PostgreSQL 成对提供，现有数据原样保留；尚未进行 PostgreSQL 实库或客户端接线验收。委派工具与主动通知见[完整能力汇总](resident-server-capabilities.md)；客户端缓存和界面仍待接入。
