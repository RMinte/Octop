# AgentComputer 暂停裁决收据只读回查（第十一轮）

本轮扩展已有的 `GET /api/agentcomputer/v1/commands/{commandID}`：除 Project、Task、Turn 和取消命令的原收据外，它还能按原裁决 `commandID` 读回审批或提问的 `RunPauseDecisionReceipt`。这只恢复已持久化的历史事实，不创建暂停、不提交决定，也不驱动引擎继续运行。`adapterVersion=0.11.0`、`capabilityRevision=11`，复用 schema25；`commandReceipts` 仍可查询，`approvals`、`questions`、`runs` 等生产执行能力仍不可用。

## 查询契约

调用方必须使用 Octop 用户 Bearer token。路径参数是**裁决命令**的 UUID，不是 `pauseID`、`runID`、幂等键或当前聊天线程 ID。服务端从认证会话确定 owner；未知命令与其他 owner 的命令都返回 404。管理员也不能仅凭管理员身份读取另一位 owner 的收据。没有裁决的 `pending` 暂停无命令收据可查。

若找到本人原裁决，HTTP 200 返回保存时的类型化原收据。`kind` 为 `approval` 或 `question`，`state` 固定为 `adjudicatedOnly`；`receiptID`、`commandID`、`pauseID`、`runID`、`turnID`、恢复与执行代次、意图与请求摘要、`pauseRevision` 和 `decidedAt` 均是当时的事实。此收据没有 `replayed` 字段，也不包含审批选项或答案正文；客户端不能从它推断当前暂停状态或工具是否执行。响应和错误均使用 `Cache-Control: no-store`。

历史查询不要求原 `restoreEpoch` 等于当前代次。即使原暂停后来已标为 `expired`，已保存的裁决仍按原始持久事实回查，不改写为新代次，也不重新授权原动作。读取时仍须核对保存的命令、意图、收据、Run 归属及相关事件和 journal；关联事实缺失或损坏时安全失败，不拼凑一个看似成功的收据。需要当前 Run 状态时，另外查询 `GET /api/agentcomputer/v1/runs/{runID}`；原裁决收据不是状态视图。

```sh
curl -i \
  -H "Authorization: Bearer ${OCTOP_BEARER_TOKEN}" \
  "http://127.0.0.1:8090/api/agentcomputer/v1/commands/${DECISION_COMMAND_ID}"
```

上例只适用于当前开发机的隔离回环实例。实际本机原型尚无生产 Task/Run 或裁决命令；因此把任意新 UUID 填入上例应得到 404，不能用它验证成功裁决。成功路径须用隔离测试夹具建立可信内部裁决后核对。

| HTTP 状态 | 含义 |
|---|---|
| 200 | 返回本人已持久化的原裁决收据；只证明当时的输入裁决。 |
| 401/403 | 缺少或无效的会话认证、用户访问受限。URL 中的 token 不代替 Authorization header。 |
| 404 | 命令不存在、属于其他 owner，或暂停尚无裁决。 |
| 422 | 路径 `commandID` 不是有效 UUID。 |
| 503 | 身份/存储不可用，或持久关联图与原收据校验失败。不会自动修复或重新裁决。 |

## 仍需完成的生产闭环

当前没有公开的审批/提问写入口，也没有生产 HITL/问题触发、可信实时授权和引擎安全续跑证明。内部裁决只有在固定原动作、当前 Run/权限/代次、有效期及可信授权均通过时才会登记；读取历史收据不能替代这些检查。客户端应继续遵循 `capabilities` 的 `available=false`，不显示可操作的审批或答题控件。完整生产路径仍依赖项目 ready、真实模型/工具、Run 生命周期、检查点与授权重验、专属取消/停止核验以及盒子环境验收。
