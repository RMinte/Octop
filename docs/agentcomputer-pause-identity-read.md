# AgentComputer 审批与提问身份只读查询（第十四轮）

本轮增加 `GET /api/agentcomputer/v1/approvals/{approvalID}` 与 `GET /api/agentcomputer/v1/questions/{questionID}`。未来客户端持有明确 UUID 时，可在深链进入或断线重连后查询**本人已保存的暂停状态**，再核对所属 Run；当前尚未接入 AgentComputer 的 Swift 深链或重连界面。接口不列出所有审批或问题，不返回待审批动作/原始问题，也不创建暂停、提交裁决或恢复引擎。`adapterVersion=0.13.0`、`capabilityRevision=13`，复用 schema25；`approvals` 与 `questions` 因内部持久内核和本人状态/收据查询标为 `supported=true`，但完整裁决及续跑能力仍为 `available=false`、`reasonCode=executionUnavailable`。`runs` 同样不可用于生产执行，`supportedPermissionModes=[]`。

## 查询契约

请求必须在 `Authorization` 头携带 Octop 用户 Bearer token。服务端从当前会话确定 owner，不接受客户端指定 owner。审批 ID 只能用于审批路由，提问 ID 只能用于提问路由。未知 ID、类型不匹配和其他用户的 ID 对普通用户与管理员都返回相同 404；合法且属于本人的暂停返回 200 和类型化 `RunPauseStatus`，不会返回 JSON `null`。与 `GET /runs/{runID}/pause` 不同，这两个路由按暂停本身的 UUID 查询，适合已知 ID 的状态回查，不能据此发现未知暂停。

响应仅有 `pauseID`、`kind`、`runID`、`turnID`、`phase`、`revision`、`restoreEpoch`、`executionEpoch`、`expiresAt`、`createdAt`、`updatedAt` 和可空的 `decisionCommandID`。`phase=pending` 表示仍保存待裁决状态；`phase=decided` 仅表示裁决事实已存，`decisionCommandID` 可用于按原命令 ID 查询裁决收据；`phase=expired` 表示旧暂停已被保守收紧。客户端还应读取 Run 当前状态，不应从任一阶段推断工具获准、效果完成或引擎续跑。

响应不包含可信动作/问题正文、答案、owner、工作区、内部派发或引擎身份。仅凭这些字段无法安全展示审批卡片或让用户核对将要授权的固定动作；客户端不得据此启用审批、提问或“继续”按钮。断线后先使用已有暂停 ID 回查，再根据 `runID` 读取 Run；若 `decisionCommandID` 非空，可查原裁决收据。查询本身不重新执行命令，也不修复未知结果。

服务端在同一数据库事务中核对暂停所属的原 Run 接受图、派发、工具效果、暂停事件和取消事实。缺失、错绑、摘要或事件损坏时返回 503，不补写、迁移或重建事实。成功和错误响应均使用 `Cache-Control: no-store`。

```sh
curl -i \
  -H "Authorization: Bearer ${OCTOP_BEARER_TOKEN}" \
  "http://127.0.0.1:8090/api/agentcomputer/v1/approvals/${APPROVAL_ID}"

curl -i \
  -H "Authorization: Bearer ${OCTOP_BEARER_TOKEN}" \
  "http://127.0.0.1:8090/api/agentcomputer/v1/questions/${QUESTION_ID}"
```

本地 8090 原型尚无生产 Run 或暂停。查询真实 UUID 的 200 响应需以隔离测试夹具验证；对本地原型的未知 ID，404 只能证明查询边界，不能证明生产审批已启用。

| HTTP 状态 | 含义 |
|---|---|
| 200 | 本人且类型匹配的审批或提问持久状态。 |
| 401/403 | 无有效会话或用户访问受限；URL token 不代替请求头。 |
| 404 | 暂停不存在、类型不匹配或属于其他用户。 |
| 422 | 路径 ID 不是 UUID。 |
| 503 | 身份/存储不可用，或原事实关联校验失败。 |

## 后续门禁与验证

生产 HITL/提问触发、安全展示固定内容与实时授权、公开裁决写入、原生引擎安全续跑和 Run 专属停止仍需单独实现与验证。目标 Qwen 服务、Ubuntu 盒子隔离和 live PostgreSQL 也未因本轮只读接口得到验证；`approvals`、`questions`、`runs` 的能力状态继续关闭。

验证：审批/提问 ID 的认证 HTTP 集成测试 **47 passed**，覆盖两类暂停的 pending、decided、expired、错误类型与跨 owner 404、坏图 503、重启和零写入；能力定向测试 **23 passed**，恢复路径复验 **5 passed**。`make all` 的 Ruff、格式和严格 mypy（598 个源文件）均通过，非 live 测试 **5288 passed、23 skipped**。本地 8090 停机后冷备份至 `.runtime/backups/iteration-14-before-pause-id-query-20261013T074232Z`，重启后报告 adapter 0.13.0/revision 13；未知 ID 404、非法 ID 422、匿名及 URL token 401，OpenAPI/Scalar 200，旧 8088 健康。box 身份、原命令收据摘要、schema25 与资源计数在重启前后相同；本地仍有 1 个项目意图、0 Provider/Task/Run/暂停/取消/事件。成功查询由隔离 Fixture 验证，不代表生产审批、真实 Qwen 或 Ubuntu 盒子已通过验收。
