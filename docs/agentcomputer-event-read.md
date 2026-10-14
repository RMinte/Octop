# AgentComputer Task 控制事件读取

`GET /api/agentcomputer/v1/tasks/{taskID}/events?limit=50&cursor=...` 以 Task 的连续 `sequence` 升序返回已提交的控制事件摘要。认证使用 Octop 用户 Bearer；不存在或属于其他用户的 Task 一律返回 404，管理员也不越权。`limit` 为 1–100。响应禁止缓存，形状为：

```json
{
  "items": [
    {
      "eventID": "00000000-0000-0000-0000-000000000001",
      "taskID": "00000000-0000-0000-0000-000000000002",
      "turnID": "00000000-0000-0000-0000-000000000003",
      "runID": "00000000-0000-0000-0000-000000000004",
      "sequence": 1,
      "occurredAt": "2026-10-14T08:00:00Z",
      "type": "run.accepted"
    }
  ],
  "coveredSequence": 1,
  "hasMore": false,
  "nextCursor": "不透明字符串"
}
```

首次请求不带 `cursor`。当 `hasMore=true` 时，原样传回 `nextCursor` 继续读取同一个 `coveredSequence` 窗口；为 `false` 时保存该 cursor，下次轮询仍原样传回。空 Task 也返回可继续轮询的 cursor。客户端只在条目已处理后推进 cursor；重复条目可按 `eventID` 去重，序号出现缺口时暂停投影并重新从头读取，不推断缺失事件。游标包含盒子、用户、Task、当前恢复代次及已读取序号，不能跨身份或 Task 使用；旧代次的已保存事件可从无 cursor 的首页重读，旧 cursor 返回 422。

当前白名单类型为 `run.accepted`、`run.claimed`、`run.dispatching`、`run.engineAccepted`、`run.cancelRequested`、`run.interrupted`、`run.finished`、`tool.prepared`、`tool.dispatching`、`tool.terminal`、`tool.outcomeUnknown`、`approval.requested`、`approval.decided`、`approval.expired`、`question.asked`、`question.answered`、`question.expired`。接口只输出类型、身份、顺序和时间；内部 `payload_json`、工具参数/结果、模型正文、路径及凭据不进入响应。读取时在同一控制库事务内核对 Task、连续序号、每条事件的 Run/Turn 归属与当前完整 Run 图；损坏的页整体返回 503。

这些类型表示**已落库的控制事实**。`run.engineAccepted` 只表示受托引擎入站接收，不证明模型已经开始生成；`tool.*` 表示内部工具效果 journal 阶段，不等于真实生产工具调用。当前本机纯文本 worker 配置 `tools=[]`，真实工具与审批续跑仍未接通。模型生成片段保存在独立的 v2 历史文档中，不在 `ac_events` 内；成功答案需由 `/runs/{runID}/result` 核对完成收据和历史。此只读分页也不是包含正文、快照重建及保留策略的完整 M1-09 事件协议，因此 `capabilities.events` 仍不可用。
