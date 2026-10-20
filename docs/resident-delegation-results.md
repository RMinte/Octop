# 私人智能体：任务委派与成果回流

本说明补充[常驻会话接口](resident-agent-conversation.md)和[历史与基本记忆](resident-history-memory.md)。本轮只交付服务端，不修改 AgentComputer。委派是用户明确发起的操作；尚未给助手模型开放自动创建 Task 的工具。

## 委派接口

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

`source_turn_id` 必须属于当前私人会话。`project_id` 必须显式提交：选择本人项目，或传 null 创建无项目任务。不会根据聊天中提到的名称、记忆或最近打开项目自动选择执行范围。`message` 是已有 `TaskMessage`，附件、模型、Skill、MCP、客户端工具及审批继续按普通 Task 的现有规则校验；`options` 的具体字段以 `/api/docs` 和普通任务接口为准，不增加另一套权限协议。

外层 `request_id` 标识用户这一次委派操作；内层 `message.request_id` 标识任务首轮。两者都应在网络重试时保持不变。同账号、同委派 ID 和完全相同的来源及消息重传，返回原委派；改变内容返回 `409 RESIDENT_REQUEST_CONFLICT`。不能用已有普通 Task 的首轮请求 ID 冒充一次新委派。创建失败不会留下没有来源的半成品任务。

只传明确选取的任务正文和引用，不自动复制私人会话全文、系统提示、私人记忆或凭据。项目独立于智能体和专家；委派不会创建专家或修改项目配置。电脑执行仍在 Task 中进行，聊天中的同意不替代 Task 的审批和目录授权。

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

```bash
uv run pytest tests/integration/test_resident_closed_loop.py -q
uv run pytest tests/unit/db/test_resident_delegations.py tests/unit/db/test_resident_closed_loop_migration.py -q
```

验证使用真实 Gateway、任务入队、Task worker 终结逻辑、数据库与归档路径，模型输出由 FakeHarness/受控 Task runner 替代，覆盖权限、幂等、记忆隔离、回报连续追问、重试范围、取消、删除、阶段丢失回执、重启去重、格式切换及关停排空。迁移为 SQLite/PostgreSQL 成对提供，现有数据原样保留；尚未进行真实 Provider、PostgreSQL 实库或客户端接线验收。自动委派工具、主动通知、客户端缓存和界面不包含在本轮。
