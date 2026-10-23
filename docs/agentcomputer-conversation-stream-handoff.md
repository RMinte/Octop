# App 智能体与任务共用正文 WebSocket：客户端接入交接

更新日期：2026-10-23。范围：服务端已实现的会话订阅契约；客户端接线待客户端任务完成。本轮不修改 AgentComputer Swift/UI，不代表当前 App 已改为 WebSocket。

服务端工作区基线：`feature/agentcomputer-foundation @ 45ee2e793c5574f85ff2e13a762afb984cd707d4`，包含尚未提交的相关实现。客户端只读核对基线：`feature/0.0.2/octp @ 5d3526f195d114063c4b639f3d5409304449872e`。

## 1. 目标与共用边界

智能体（Resident）和任务都按正文的已保存增量主动推送，App 使用现有的同一条认证 WebSocket 接收。模型正文直接进入回复；不因工具意图缓存正文或将正文改为思考，也不等待轮次结束才显示。

共用的是连接认证、token 续期、心跳、单个接收循环、订阅生命周期、提交后的更新通知及推送泵。服务端公共层为 `infra/conversations/updates.py`，只保存待更新资源标识，不保存另一份正文、不负责执行或鉴权。连接仍使用 `/api/project-tasks/ws`，协议版本仍为 3；路径名称不表示 Resident 变成 Task。

任务继续使用 `task.subscribe`、`snapshot`、`turn` 和既有 cursor。Resident 使用本说明的新订阅及 DTO，保留私人身份、记忆、历史、授权和 REST 幂等操作，不套用 Task 执行器、审批或客户端工具协议。此次不实施任务／专家业务模型统一规划。

Task 正文目前按约 100ms 的窗口合并保存；Resident 在每次正文 chunk 保存成功后通知。通知立即唤醒本进程的订阅读取；约 500ms 的数据库检查只作其他进程写入或未通知变化的兜底，不是正文必须再等待的窗口。连续通知可以合并成一次最新状态读取，不承诺每个原始模型 chunk 单独发帧；WebSocket 携带权威累计状态，不是逐 token 日志。

## 2. 能力探测与连接

读取 `GET /api/agents/resident-capabilities`，本次新增：

```json
{
  "conversation_transport": "rest_polling",
  "conversation_stream": {
    "transport": "websocket",
    "path": "/api/project-tasks/ws",
    "protocol_version": 3
  }
}
```

这里只展示相关字段。`conversation_transport` 保留既有值，因为已部署 App 以它作为入口门禁，现有 REST 接口也继续可用。新客户端使用 `conversation_stream` 探测并启用正文订阅；不能继续只根据旧字段认定服务端没有 WebSocket。缺少或不识别新声明时，不猜测新端点或声称已接入流式能力。

复用现有工作区共享 socket；不要为 Resident 再建一套认证、接收或重连循环。沿用任务协议的鉴权规则，收到以下 ready 后才订阅：

```json
{"type":"ready","protocol_version":3}
```

`session`、`heartbeat`、鉴权失效、关闭代码和命令 ID 规则均沿用[任务 WebSocket 指南](project-task-websocket.md)。Resident 即使是唯一订阅者，也必须阻止共享连接被误判空闲并关闭。订阅总数上限为 Task 与 Resident 合计 32 个。

## 3. 订阅与取消订阅

先读取现有 `GET /api/agents/resident-conversation` 获得 ready 绑定。使用返回的准确 Agent/thread ID：

```json
{
  "type": "resident.subscribe",
  "id": "resident-watch-0001",
  "agent_id": "resident_agent_example",
  "thread_id": "resident_thread_example"
}
```

`id` 沿用公共命令格式：8–128 字符，`A–Z a–z 0–9 _ -`。资源标识为非空字符串，最长 256 字符。此命令没有 `cursor`、`revision` 或 `data` 参数，未知字段会拒绝。它只观察已受理的会话，不发起模型执行。

服务端先发送 `resident.snapshot`，然后发送订阅回执：

```json
{"type":"result","id":"resident-watch-0001","data":null}
```

不能等待 result 才开始处理首个快照。重复订阅同一 scope 会重建最近状态基线，但不会重复创建 watch 或执行消息。

离开页面或不再需要观察时：

```json
{
  "type": "resident.unsubscribe",
  "id": "resident-unwatch-0001",
  "agent_id": "resident_agent_example",
  "thread_id": "resident_thread_example"
}
```

取消订阅也返回 `result`、`data:null`。取消订阅、切页、App 后台及断线均不取消服务端执行；用户明确停止仍调用原 `POST .../cancel`。

## 4. 正文快照与版本合并

初次订阅、每次重连订阅，以及会话版本变化时发送同一种事件：

```json
{
  "type": "resident.snapshot",
  "data": {
    "agent_id": "resident_agent_example",
    "thread_id": "resident_thread_example",
    "revision": 12,
    "active_turn_id": "resident_turn_example",
    "has_earlier": false,
    "turns": [
      {
        "turn_id": "resident_turn_example",
        "revision": 8,
        "request_id": "resident-message-0001",
        "user_message_id": "resident:resident_turn_example:user",
        "assistant_message_id": "resident:resident_turn_example:assistant",
        "prompt": "请解释这个设计。",
        "reply": "先看正文如何到达客户端，",
        "status": "running",
        "created_at": "2026-10-23T01:00:00Z",
        "updated_at": "2026-10-23T01:00:01Z",
        "error_code": null,
        "error_message": null,
        "authorization": {}
      }
    ]
  }
}
```

`data` 为 `ResidentSnapshot`，固定包含最近最多 50 轮；`has_earlier` 表示更早记录通过既有历史接口获取。它不包含独立的任务成果回报、主动通知或 inbox，相关完整历史和已读仍使用已有接口。机器定义见[JSON Schema](project-task-websocket.schema.json)中的 `ResidentSnapshot`、`ResidentTurn`、`ResidentSnapshotEvent`。

两个版本不能混用：

- `data.revision` 是当前 thread 的会话版本，用于接受或忽略整份最近快照；它也可能因历史回报等会话变化而增长，不代表正文一定变长。
- `turns[].revision` 是单个持久轮次的版本。本次同时给 REST 发送回执、请求查询、取消、快照和历史中的 `ResidentTurn` 增加此字段，取已有数据库版本，无数据库迁移。

先校验连接代次、账号和准确的 `(agent_id, thread_id)`，再按会话版本合并。已有较新版本时忽略旧快照；相同版本应幂等。每轮按 `turn_id` 及其 revision 接受最新状态，使用稳定消息 ID 替换累计 reply，不能把 `A → AB → ABC` 追加成 `AABABC`。重复版本不重复生成消息，终态即使没有新增正文也必须应用。

REST 回执可能晚于 WebSocket 新正文到达，时间戳也可能在同一毫秒相同。回执仍用于确认 request/turn 身份、原输入和发送持久记录，但不能用旧 turn revision 覆盖新正文或终态。不能只用 `updated_at >=` 或字符串更长来决定整份状态的权威。

最近 50 轮快照只更新实时窗口；不能据此删除独立加载的旧历史、任务回报、通知或搜索结果。REST 历史中的 turn 使用同一个 per-turn revision 规则合并。

状态仍为 `queued / running / completed / failed / cancelled / interrupted`。只有真实终态停止流式展示；订阅成功、空 reply、暂时没有新帧或网络断开都不是终态。该 DTO 没有新增思考字段，不把正文归入思考，也不从任务 DTO 推导 Resident 状态。

## 5. 重连、发送与错误

网络重连后重新认证，并为所有仍被观察的 Resident scope 重新发送 `resident.subscribe`。每次都获取最新最近快照，不传 Task cursor、不把 thread revision 当事件日志游标。连接丢失期间执行继续；恢复观察不重发用户消息、不重新执行工具。

消息发送、原请求恢复和停止继续使用现有 REST 接口及原 request_id 幂等契约。丢失发送响应时查询原请求，不因 socket 重连生成新 request_id 或自动重新 POST。详见[Resident 接入说明](resident-agent-conversation.md)。

服务端每次订阅读取都会重验 owner、Agent/thread 绑定和可用状态；其他用户资源返回 404，管理员也不能越过私人会话隔离。

订阅失效只移除受影响的 scope，错误形状为：

```json
{
  "type": "error",
  "agent_id": "resident_agent_example",
  "thread_id": "resident_thread_example",
  "status": 404,
  "error": {"code":"NOT_FOUND","message":"资源不存在","details":{}}
}
```

命令错误带 `id`，通过 pending command 定位原 Resident scope；不要假设它一定另带 agent/thread。异步订阅错误使用 `agent_id + thread_id` 定位，不能因缺少 task_id 就关闭整个 socket、清掉其他 Task、审批观察或客户端工具。连接级认证失效继续按公共连接规则处理。

## 6. 客户端建议改动位置

以下来自只读核对，客户端团队实施时需重新核对自己的基线：

| 位置 | 接入工作 |
| --- | --- |
| `Services/Box/OctopTaskSocket.swift` | 现有共享 actor 增加 Resident 观察者、订阅／退订／重连恢复及独立状态投影；纳入连接存活和 reset 清理。保留唯一 receiver 和现有 generation/connection fencing。可按最终实际职责调整名字，不必为改名扩大差异。 |
| `Services/Box/OctopTaskSocketFrame.swift` | 增加两类命令、`resident.snapshot` 解码和 agent/thread 订阅错误身份；普通 Task 解码与工具回执不变。 |
| `Services/Box/OctopResidentModels.swift` | 解码新 capability 对象和 `Turn.revision`，保留 Resident 独立 DTO。 |
| `Services/Box/OctopResidentService.swift` | 提供 scope 观察接口，取得工作区已有共享 socket；发送／取消／原请求查找继续原服务。 |
| `Services/Box/OctopWorkspaceDataSource.swift` 的 `residentService()` | 向 ResidentService 注入此 DataSource 已有的 `taskSocket` 同实例，不能在 ResidentService 内再创建一个 socket actor。 |
| `Features/Workspace/OctopResidentConversationViewModel.swift` | 正文由 socket 观察驱动，移除活动正文 1 秒轮询；立即应用、渲染新快照，不串行等 history/days/inbox。保留持久 pending、原请求核对、旧回执去重和页面代次隔离。 |

历史、inbox 与共享已读可在进入前台、重连、终态或独立低频刷新时同步，不应在每个正文事件上依次请求四个 REST 接口。既有 `applyResidentPresentation`、消息稳定 ID 与约 50ms Markdown 渲染合并可继续复用，无需为此次服务端契约新增页面或逐字计时器。

## 7. 定向验收

服务端测试使用真实 REST 受理 → Resident worker → 持久提交 → 公共通知 → WebSocket 推送路径，仅把模型输出替换为可控序列；关闭数据库兜底也必须收到运行中正文和终态。另覆盖 Task 与 Resident 同连接、重复订阅／退订清理、跨用户拒绝、读中新增提交、通知缺失兜底及断线继续／重连快照。

客户端至少覆盖以下反例：

1. 仅 Resident 有观察者时连接保持可用，断线后自动恢复订阅；Task 与 Resident 同时观察仍只有一个接收循环。
2. 运行中接收 `A → AB → ABC` 时逐次更新同一正文，重复和低版本事件不回退、不重复追加；空白完成回复仍应用终态。
3. 新正文先到、旧 POST 回执后到，包括相同毫秒时间戳；按 per-turn revision 保留新正文和终态，同时完成 pending 身份核对。
4. 重连不重发 messages/cancel，不重执行；最新 50 轮不清空已加载旧历史和独立回报。
5. Resident 订阅级 404 只关闭该 scope，其他 Task／审批／工具保持可用；401/USER_DISABLED 正确使连接和会话失效。
6. 切页、后台与取消观察不会停止服务端工作；显式停止仍指向原 turn。
7. 在实际 App 的智能体入口和任务入口分别进行长、短回答测试，记录运行中正文多次增长，并分开测接收和实际显示。仅 Task 的日志、服务端通过或 50ms 配置均不能替代智能体 App 验收。

## 8. 本轮服务端验证

2026-10-23 定向测试共 **129 项通过**：公共通知与原任务正文推送 11 项，Resident 接口及历史 17 项，Resident 完整服务／闭环／恢复／授权工具 54 项，任务 WebSocket／诊断／关闭 36 项，真实 WebSocket 传输 4 项，新增 Resident 推送 7 项。后者走真实受理和 worker 保存路径，模型输出由可控队列替代，不产生真实模型用量。

相关源文件 Ruff、格式检查和严格 Mypy 通过；JSON Schema 与导出器一致，本文 7 个 JSON 示例完成解析，其中新增命令、正文快照和能力对象按实际模型校验。未运行完整门禁，未提交或推送。

本机 8088 已在连续空闲窗口后完成备份和平滑重启，新 PID 为 `68292`，健康检查与公开 API 文档检查通过。数据库版本保持 55，重启前后项目与 Resident 会话表的数据摘要一致。运行记录位于 `.runtime/8088/shared-conversation-latest.json`，备份位置也记录其中；只有实际接口声明出现 `conversation_stream` 后才启用新订阅。

文档已交给客户端任务《排查任务正文 Markdown 流式效果》继续实现。当前 App 是否已接线及实际屏幕验收必须由客户端任务另行确认，服务端通过不代替 App 验收。
