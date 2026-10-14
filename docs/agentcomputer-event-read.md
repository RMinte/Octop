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

`GET /api/agentcomputer/v1/tasks/{taskID}/snapshot` 返回 `{task,latestRun,coveredSequence,nextCursor}`，供客户端在无法连续续读时建立**控制面**基线。`latestRun` 在从未接受 Turn 的 Task 上为 null。Task、最新 Run 与事件水位由同一控制库快照读取；SQLite 持有事务快照，PostgreSQL 使用只读 repeatable-read。服务端核对序号区间连续、末尾事件锚点与最新 Run 的完整归属和持久图；不能证明的存储返回 503，不返回半个基线。`nextCursor` 即上述事件接口的 cursor，从 `coveredSequence+1` 继续；空 Task 的水位 0 也有可续读 cursor。未知或他人的 Task 同为 404，旧恢复代次 cursor 返回 422，此时重取快照。响应禁止缓存。

当前快照不含历史消息页、模型正文、审批内容或正式成果；它没有跨控制库与 v2 历史库的原子性。客户端从头补事件可保留旧阶段历史；若连续性丢失，切换到新快照后只能把已验证的 Task/最新 Run 当成基线，不能补造跳过的历史阶段或正文。客户端应先耐久保存新基线，再保存其 cursor，并从该 cursor 续读；这仍是完整 C18/C19 协议的一个有限切片。

这些类型表示**已落库的控制事实**。`run.engineAccepted` 只表示受托引擎入站接收，不证明模型已经开始生成；`tool.*` 表示内部工具效果 journal 阶段，不等于真实生产工具调用。当前本机纯文本 worker 配置 `tools=[]`，真实工具与审批续跑仍未接通。模型生成片段保存在独立的 v2 历史文档中，不在 `ac_events` 内；成功答案需由 `/runs/{runID}/result` 核对完成收据和历史。此只读分页也不是包含正文、快照重建及保留策略的完整 M1-09 事件协议，因此 `capabilities.events` 仍不可用。
