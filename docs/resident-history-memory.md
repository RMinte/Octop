# 私人智能体：历史与基本记忆接口

本说明补充 [私人智能体常驻会话](resident-agent-conversation.md)，对应服务端首版闭环。本轮不修改 AgentComputer。所有接口使用现有 Bearer 身份认证，只允许当前用户自己的 resident Agent 和绑定 thread；管理员也不能通过这些接口读取其他用户的私人数据。成功响应均带 `Cache-Control: no-store`。

下文的 `{base}` 为 `/api/agents/{agent_id}/threads/{thread_id}`。历史只读请求、记忆查看和手工管理不启动模型。无绑定、Agent/thread 不匹配或其他用户资源返回 `404 NOT_FOUND`。

## 统一历史

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `{base}/conversation/history/days` | 最近七个非空服务端会话日。 |
| GET | `{base}/conversation/history` | 按日或全时间线读取有界历史页。 |
| GET | `{base}/conversation/history/search` | 直接搜索用户轮次与任务回报。 |
| GET | `{base}/conversation/history/items/{item_id}` | 定位稳定历史项及前后文。 |

原有 `{base}/history` 继续表示正式聊天消息归档，响应 schema 不变。新的统一时间线同时包含受理轮次和已持久保存的任务回报，不覆盖旧路由。

### 会话日与日期跳转

```http
GET {base}/conversation/history/days?limit=7
```

```json
{
  "agent_id": "AGENT_ID",
  "thread_id": "THREAD_ID",
  "history_revision": 14,
  "days": [
    {"day_key":"2026-10-20","item_count":4,"timezones":["Asia/Shanghai"]}
  ],
  "next_cursor": null
}
```

`limit` 默认 7，范围 1–31。`days` 按日期倒序，只返回有内容的日子，空白日不占七日额度。每个用户轮次算一项，每份任务回报算一项。`next_cursor` 非空时，保留其他查询参数并提交 `cursor` 获取更早日期。

`at=2026-10-18` 从该日期或最近的更早非空日开始，不需要先下载全部历史。日期必须是真实的 `YYYY-MM-DD`，非法日期返回 422。

用户轮次的 `day_key` 和 `timezone` 在受理时按服务端 `default_timezone` 固定。跨午夜完成、迟到正文和后续状态更新仍属于原开始日，不按客户端时区重新分组。任务回报保存自己的固定日期和时区。服务端日后改变时区，也不重新计算已有归属；同一个 `day_key` 如涉及多个既有时区，`timezones` 会完整列出。

升级前没有日期字段的首期收据，在启动时按升级当时的服务端时区一次性回填并冻结。服务端无法推断当时未记录的旧配置，不把回填时区冒充已知的原始时区。

### 有界正文页

```http
GET {base}/conversation/history?day_key=2026-10-20&limit=80
```

响应为 `agent_id`、`thread_id`、`history_revision`、`items`、`next_cursor`。`day_key` 可省略，省略时读取全时间线的最新窗口。`limit` 默认 80，范围 1–100。每页内部按时间正序，`next_cursor` 指向更早窗口；不要把分页后的整页当成追加到末尾的新消息。

每个 `items` 元素包含：

| 字段 | 含义 |
| --- | --- |
| `item_id` | 稳定定位身份：`turn:<turn_id>` 或 `report:<report_id>`，按原值保存。 |
| `kind` | `turn` 或 `task_report`。 |
| `day_key`、`timezone` | 服务端固定的日期归属。 |
| `created_at`、`updated_at` | UTC ISO 8601 时间，展示使用固定的服务端归属。 |
| `turn` | `kind=turn` 时为首期 `ResidentTurn`，含用户正文、完整当前回答和状态；否则为 null。 |
| `report` | `kind=task_report` 时为持久任务回报；否则为 null。 |

`report` 包含 `report_id`、`message_id`、`task_id`、`task_turn_id`、`status`、`text`、`error_code`、`error_message`、`artifacts` 和 `delivery_status`。`status` 为 `completed / failed / cancelled`。`delivery_status=pending` 表示向正式归档及会话上下文的投递尚在恢复；回报正文已经持久，可以展示。`delivered` 表示投递阶段已经完成，不代表任务成果仍可从已删除的文件或任务中下载。

`history_revision` 与该页正文来自同一数据库读取视图，可用于丢弃迟到的旧页。分页采用时间与稳定 ID 的复合边界；同一毫秒到达的多项不会因为分页而遗漏或重复。各数据来源先在数据库中应用过滤、顺序边界和 limit，再合并有限窗口，不先加载完整会话。

游标由服务端签名，绑定盒子身份、恢复代次、用户、thread、日期或搜索条件。客户端不得解释或改写，不能用于另一账号、thread 或查询。游标非法或因恢复而失效时返回 `409 PROJECT_SYNC_RESET`，重新读取同一 thread 首屏；不要新建会话或重放用户发送。

### 搜索与定位

```http
GET {base}/conversation/history/search?q=发布计划&limit=40
GET {base}/conversation/history/items/turn%3ATURN_ID?before=20&after=20
```

搜索 `q` 为 1–200 字符的字面子串，覆盖用户正文、回答正文和任务回报正文。`%`、`_` 按普通字符处理，不作为通配符。`limit` 默认 40，范围 1–100。响应结构与正文页相同，`next_cursor` 继续搜索更早匹配项；更换关键词时必须从无游标的新搜索开始。

定位参数 `before`、`after` 各默认 20，范围 0–50，返回 `anchor_item_id`、按时间正序排列的 `items`、`history_revision`、`has_before`、`has_after`。目标不在本人 thread 中时返回 404。该接口可直接围绕搜索命中的稳定项读取前后文，不需要从头翻页。

## 基本私人记忆

长期记忆复用既有 `octop-memory` Agent 存储和 `agent_<agent_id>` 命名空间，没有新增第二套记忆表。Task 的独立记忆保持自己的范围。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `{base}/memory?limit=50&include_deprecated=false` | 最近的记忆窗口，默认排除废弃和被更正的旧事实。 |
| POST | `{base}/memory` | 用户明确保存一条事实，成功返回 201。 |
| POST | `{base}/memory/{atom_id}:replace` | 更正有效事实，保留前任及审计关系。 |
| POST | `{base}/memory/{atom_id}:deprecate` | 逻辑废弃事实，重复操作安全。 |

保存请求：

```json
{"assertion":"回答时先给结论，再解释原因。","kind":"Preference"}
```

`assertion` 去除首尾空白后必须非空，长度最多 2048 字符。`kind` 默认为 `Fact`，也可为 `Preference / Decision / Task`。用户身份由认证确定，不能指定另一个 owner 或覆盖审计 actor。

更正请求为 `{"assertion":"新的准确事实"}`，废弃请求为 `{"reason":"用户不再认可此项"}`，其中 `reason` 可空，最多 1024 字符。

三个写操作返回同一 `ResidentMemoryItem`：`atom_id`、`assertion`、`kind`、`created_at`、`deprecated_at`、`superseded_by`、`source_event_ids`、`quote_event_id`。更正后的 `atom_id` 可能改变，客户端应使用返回的继任身份；旧项通过 `superseded_by` 指向新项。不存在或已经失效的更正目标返回 404；重复事实或并发更正冲突返回 `409 RESIDENT_MEMORY_CONFLICT`。

最近窗口返回 `items` 和 `has_more`，`limit` 默认 50，范围 1–100。既有 `/api/agents/{agent_id}/memory/atoms/list` 继续提供查询、offset 和更多管理筛选，使用同一数据；resident 实例的私人所有权校验同样适用。新的窗口不是完整记忆库的全量导出。

后续每轮 resident 对话仅临时提供最新 20 条有效事实，JSON 总长度最多 8192 字节；它们作为本轮背景数据使用，不永久追加到聊天检查点。更正和废弃影响之后发起的轮次，不撤回已经开始的模型请求，也不删除既有历史。超出窗口和预算的有效事实仍保留在记忆库中，可继续管理，但不保证每轮全部召回。

本期保留手工沉淀，自动捕获和自动推断关闭。普通聊天正文和任务回报不会自动变成私人事实；私人记忆不会自动复制到委派任务。废弃是逻辑移除并停止召回，审计记录和既有对话仍保留，不是物理擦除。

## 定向验证

```bash
uv run pytest tests/unit/db/test_resident_history.py tests/unit/agents/test_resident_memory.py -q
```

验证覆盖七个非空日、日期定位、服务端日期冻结、跨午夜、升级回填、签名游标隔离、同毫秒混合来源分页、字面搜索、有限窗口读取、复用现有 Agent 记忆存储、更正和废弃后的有效召回及预算上限。完整闭环另见常驻会话说明中的集成验证。
