# AgentComputer Run 暂停状态只读查询（第十三轮）

本轮为 M1-08 增加 `GET /api/agentcomputer/v1/runs/{runID}/pause`，供客户端按明确 Run ID 回查**已保存的暂停状态**。它不创建审批或问题，不提交裁决，也不恢复引擎。`adapterVersion=0.12.0`、`capabilityRevision=12`，复用 schema25；`runs`、`approvals`、`questions` 仍不可用于生产执行。

## 查询契约

请求必须携带 Octop 用户 Bearer token，服务端从会话确定 owner。本人 Run 没有暂停时返回 HTTP 200 和 JSON `null`；未知 Run、其他用户的 Run 对普通用户和管理员都返回同一个 404。存在暂停时返回类型化对象：`pauseID`、`kind`、`runID`、`turnID`、`phase`、`revision`、`restoreEpoch`、`executionEpoch`、`expiresAt`、`createdAt`、`updatedAt` 和可空的 `decisionCommandID`。

`phase=decided` 仅说明裁决事实已经保存；`decisionCommandID` 可以用于查询原裁决收据。它们都不表示动作获得许可、工具已执行或引擎已经续跑。`phase=expired` 表示保存的旧暂停已过期并被保守收紧；客户端还应读取 Run 当前状态。响应不包含原始问题/工具参数、答案、owner、工作区、内部派发或引擎身份。审批卡片所需的安全展示内容契约尚未建立，因此不能据此开启审批按钮。

查询在同一数据库事务里核对原 Run 接受、派发、工具效果、暂停事件和取消事实。记录缺失、错绑、摘要或事件损坏时返回 503，不补写或重建事实。成功和错误响应都带 `Cache-Control: no-store`。

```sh
curl -i \
  -H "Authorization: Bearer ${OCTOP_BEARER_TOKEN}" \
  "http://127.0.0.1:8090/api/agentcomputer/v1/runs/${RUN_ID}/pause"
```

本地 8090 原型没有生产 Run；成功响应通过临时数据库隔离夹具验证。对原型中的未知 Run，上述请求应返回 404，不能用任意 UUID 证明生产暂停已可用。

| HTTP 状态 | 含义 |
|---|---|
| 200 | 本人 Run 的暂停状态；没有暂停则为 `null`。 |
| 401/403 | 无有效会话或用户访问受限；URL token 不代替请求头。 |
| 404 | Run 不存在或属于其他用户。 |
| 422 | 路径中的 Run ID 不是 UUID。 |
| 503 | 身份/存储不可用，或原事实关联校验失败。 |

## 仍需完成

公开审批/提问提交需要可信的固定动作内容和实时授权；原生引擎安全续跑还需要完整 Run/代次/检查点上下文。当前普通 harness 的 HITL 恢复入口没有这些托管 Run 绑定，不能直接接到公开 POST。生产触发、客户端安全展示、Run 专属停止证明、真实模型和盒子隔离仍按 M0/M1 门禁推进。

## 验证记录

隔离 HTTP 测试覆盖本人无暂停、审批与问题各自的 pending/decided/expired、跨 owner 和管理员、认证、非法 ID、关联损坏、重启后原状态读取及读取不写入；定向集 **29 passed**。完整 `make all` 的 Ruff、格式、严格 mypy（598 个源文件）与非 live 测试通过，**5268 passed、23 skipped**；跳过项不计通过。

本机 8090 原型在停止后备份至 `.runtime/backups/iteration-13-before-pause-query-20261013T065930Z` 并重启。新版本报告 adapter 0.12.0、revision 12；`/api/docs` 和 OpenAPI 可读，新查询的未知 Run 404、非法 ID 422、匿名及 URL token 401。重启前后的 box 身份、原命令收据摘要、schema25 与数据计数相同；本地仍有 1 个项目意图、0 Provider、0 Task/Run/暂停/取消/事件，原 8088 健康。成功暂停状态来自临时隔离夹具，不是本地原型的生产执行。PostgreSQL live、真实 Qwen、Ubuntu 盒子和客户端操作仍未验。
