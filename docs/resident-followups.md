# 私人智能体主动跟进接口

此能力由统一生活工作管家承接。每个关注事项固定关联当前登录用户、管家 `agent_id` 和原会话 `thread_id`，生活提醒、工作进度和委派成果都进入同一会话的持久历史。服务端没有自动创建的关注事项；用户显式创建授权后才开始跟进。

`note` 按用户指定的时间发送原样保存的提醒正文；`task_watch` 观察用户明确选定的本人 Task，无变化时保持安静，状态变化时通知，任务完成、失败、取消或删除后发送最终状态并停止。已经完成的任务可显式创建关注，现有终态结果会通知一次。这两种授权只产生会话通知。`task` 则需要用户额外明确授予创建 Task 的能力，按指定时间受理完整任务，再沿用普通 Task 的权限、审批和执行流程。

所有路径的前缀为：

```text
/api/agents/{agent_id}/threads/{thread_id}/conversation/followups
```

接口均需普通 Bearer 用户认证，返回 `Cache-Control: no-store`。跨用户、Agent/thread 不匹配或跨会话访问返回 `404`。跟进授权与具体事项绑定，不因专业工作委派而扩大。

| 方法与后缀 | 用途 | 成功响应 |
| --- | --- | --- |
| `POST /` | 显式创建并启用提醒、关注或定时任务授权 | `201 ResidentFollowup` |
| `GET /?limit=50&page_cursor=...` | 分页读取全部状态的事项 | `200 ResidentFollowupPage` |
| `GET /{followup_id}` | 读取当前授权与调度状态 | `200 ResidentFollowup` |
| `PUT /{followup_id}` | 全量修改、重新授权，或暂停 | `200 ResidentFollowup` |
| `POST /{followup_id}/cancel` | 取消并使旧待发通知失效 | `200 ResidentFollowup` |

## 显式创建提醒

```json
{
  "request_id": "user-reminder-20261020-001",
  "purpose": "每日整理工作记录",
  "kind": "note",
  "schedule": "cron:0 18 * * 1-5",
  "subject_task_id": null,
  "payload": {"text": "请整理今天的工作记录。"},
  "capabilities": ["conversation_notify"],
  "notification_target": "conversation"
}
```

`request_id` 在本人授权范围内唯一。断线后重试使用同一个标识与同样内容；原事项已修改、取消或完成时，重试仍返回原事项的当前状态。相同标识配不同内容或不同会话返回 `409 RESIDENT_REQUEST_CONFLICT`，不会授权第二个事项。

提醒和定时任务的 `schedule` 复用服务端既有 Cron 解析器：

- `date:2026-10-21T09:00:00+08:00`：一次提醒，创建时必须是将来的时间。
- `cron:0 18 * * 1-5`：标准五段 Cron，数字星期采用 Unix 规则，`0` 和 `7` 都是星期日。
- `interval:3600`：固定秒数的周期，最短 `60` 秒。

不含时区偏移的 `date:` 和 Cron 使用创建授权时的服务端时区，并在事项的 `timezone` 中返回该快照。服务端以后更改时区不会悄悄改变已授权的时间。间隔周期的锚点持久保存，服务重启不会重置计时。

纯通知的权限与目标可省略；它们的默认值分别为 `["conversation_notify"]` 和 `"conversation"`。定时任务必须显式额外授予 `task_create`。外部收件人或未定义的能力会被拒绝。

## 显式创建任务关注

```json
{
  "request_id": "watch-report-task-001",
  "purpose": "等待季度报告任务的结果",
  "kind": "task_watch",
  "subject_task_id": "task_01...",
  "payload": {},
  "capabilities": ["conversation_notify"],
  "notification_target": "conversation"
}
```

任务关注不填写 `schedule` 或提醒正文。服务端保存创建时的任务状态作为基线，后续只对状态、具体任务轮次或最终正文的变化产生通知；运行中的正文片段不逐字发送。终态通知包含该轮正文的前 `8000` 个字符和原 Task、轮次引用，完整结果从原任务接口读取。任务审批、追问、客户端工具等待、暂停和中断属于可观察状态，原审批与执行流程继续由任务接口管理。

## 显式授权定时工作

```json
{
  "request_id": "schedule-report-001",
  "purpose": "每个工作日整理项目日报",
  "kind": "task",
  "schedule": "cron:0 18 * * 1-5",
  "subject_task_id": null,
  "payload": {
    "source_turn_id": "turn_01...",
    "project_id": "project_01...",
    "message": {
      "request_id": "authorized-message-001",
      "prompt": "按当前项目配置整理今天的日报。",
      "attachment_ids": [],
      "options": {"skills": [], "mcp_servers": [], "memory_enabled": false}
    }
  },
  "capabilities": ["task_create", "conversation_notify"],
  "notification_target": "conversation"
}
```

`source_turn_id` 必须是这个私人会话中的本人轮次。`project_id` 为用户明确选定的本人项目，或者显式传 `null` 创建无项目 Task，不推断项目。`message` 使用完整 `TaskMessage`，包括用户选择的模型、能力、设备与客户端工具；省略的选项沿用普通 Task 的当前项目配置规则。管家本身不会获得这些行动工具。

每个到期时刻使用由 `followup_id`、`authorization_version` 和该次计划时间生成的稳定派发标识，替换消息模板中的 `request_id`。因此同一到期事件在断线、重启或重复轮询时只受理一个 Task；不同周期明确创建独立 Task。任务、首轮、原会话委派记录、受理通知与调度游标一并提交。其完成、失败或取消结果复用既有成果回流机制，返回原会话。

受理时重新校验本人项目、权限、模型、配额、能力与附件，使用届时的项目配置和账户审批策略；定时授权不会跳过任务原有的审批。周期任务不能绑定一次性 `attachment_ids`，因为上传附件首次发送后归属一个具体 Task；一次 `date:` 任务可以使用附件，届时仍需满足普通附件的归属与有效期规则。

受理失败会回滚这次部分创建的任务，持久保存失败通知，并将事项置为 `completed` 停止后续触发。事项返回 `last_outcome: "admission_failed"` 和 `last_error_code`；用户解决问题后，可用最新 `expected_revision` 和完整授权内容 `PUT` 重新启用，此时授权版本递增。成功受理则返回 `last_outcome: "admitted"` 和 `last_task_id`。成功的一次任务授权不会自动恢复。

暂停或取消仅停止后续定时受理；已经受理的 Task 独立存在，用户通过普通 Task 控制接口暂停、取消、处理审批或继续追问。

## 修改、暂停与取消

事项返回字段包括 `followup_id`、`request_id`、`agent_id`、`thread_id`、授权内容、`timezone`、`status`、`revision`、`authorization_version`、`next_fire_at`、`created_at` 和 `updated_at`。

`status` 为 `enabled`、`paused`、`cancelled` 或 `completed`。`next_fire_at` 对提醒或定时任务表示下次到期时间，对任务关注表示下一次观察时间。UTC ISO 时间戳供传输，客户端可结合返回的服务端时区展示。

修改使用全量授权内容，额外带上刚读到的修订和目标状态：

```json
{
  "expected_revision": 1,
  "status": "paused",
  "purpose": "每日整理工作记录",
  "kind": "note",
  "schedule": "cron:0 18 * * 1-5",
  "subject_task_id": null,
  "payload": {"text": "请整理今天的工作记录。"},
  "capabilities": ["conversation_notify"],
  "notification_target": "conversation"
}
```

修订不匹配返回 `409 RESIDENT_FOLLOWUP_CONFLICT`。每次修改、暂停或取消都提升 `authorization_version`，旧授权版本尚未投递的通知失效。暂停期间不产出新通知；重新启用属于新的明确授权。已取消或成功完成的事项不能复活，需要新的 `request_id` 创建新事项；上文所述受理失败的定时任务可通过完整 `PUT` 明确重新授权。

取消请求：

```json
{"expected_revision": 2}
```

同一次取消断线重试是安全的；旧修订的取消不能覆盖更新后的授权。修改与取消使用与通知投递共享的授权锁：取消成功返回后，旧版本通知不会再晚到。已完成的历史通知保留。

## 重启、离线与投递

到期观察、下次调度游标、任务受理与通知出站记录在同一个数据库事务内写入。重复轮询或进程重启不会为同一到期事件创建重复通知或 Task。周期提醒、定时任务在长时间离线后合并逾期周期为一次受理，然后跳到下一个将来时刻，避免积压集中补发；一次授权会补投一次。

前台关闭或网络断开不终止已明确授权的跟进。通知通过统一持久投递进入历史与后续对话上下文，并形成会话未读事件；前台重连读取历史和未读即可。写入历史或上下文中途失败时，服务端保留通知状态并幂等补投。客户端 APNs、系统推送、邮件和 IM 投递不属于这些授权。

通知收据也可独立读取：`GET /conversation/notifications?limit=50&page_cursor=...` 和 `GET /conversation/notifications/{notification_id}`，前面仍带相同管家、会话前缀。列表包含已投递和当前授权版本的待投记录；已取消或旧授权的待投记录立即隐藏。单条收据保留 `discarded` 审计状态，客户端不应把它显示为新通知。通知返回稳定 `message_id`、`followup_id`、`authorization_version`、`kind`、正文、结构化 `payload`、时间与投递状态。

SQLite 与 PostgreSQL 共用迁移和 API 语义。服务端按现有 resident 单进程工作者边界执行；此接口没有引入额外队列服务。
