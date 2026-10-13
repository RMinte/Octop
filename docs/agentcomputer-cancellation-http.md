# AgentComputer 精确 Run 取消命令与状态查询（第十轮）

本轮把第九轮的持久取消内核接到认证 HTTP。请求只作用于路径中的一个 `runID`；不通过 Octop thread 的“当前回合”寻找目标，也不调用线程级取消。`adapterVersion=0.10.0`、`capabilityRevision=10`，数据库仍为 schema25。当前没有生产 Run 创建和 Run 专属引擎停止证明，所以 `cancellation` 声明为 `supported=true`、`available=false`、`reasonCode=executionUnavailable`；`runs`、`tasks`、审批和提问的客户端可用性仍关闭。

## 命令与收据

`POST /api/agentcomputer/v1/runs/{runID}/cancel` 要求 Octop 用户 Bearer token。body 是严格类型化的 `CancelRunCommand`：`commandID`、`idempotencyKey`、`expectedRestoreEpoch`、`expectedRevision`。owner 从认证会话取得，路径 `runID` 是唯一目标，不能在 body 中换目标。成功返回 HTTP 201 和原始 `CancelRunReceipt`；其中 `status=cancelRequested` 仅证明请求已经持久化，`status=stopped` 只出现在事务能证明从未派发的 Run。`acceptedAt`、原命令 ID、请求时和结果 Run revision、所属 Project/Task/Turn/Run 及两个执行代次均记录在收据中。

同一 owner、幂等键、Run 和请求语义的重试返回首个原收据，响应中的 `replayed=true`；历史 `GET /api/agentcomputer/v1/commands/{commandID}` 仍返回不可变原收据的 `replayed=false`。当前 `restoreEpoch` 在重放前核对；旧代次、改动后的 revision/目标/语义、跨 owner 或复用命令 ID 都不能变成新的取消。未知 Run 与其他用户的 Run 均为 404；修订、代次和幂等冲突为 409；非法 body/ID 为 422；超过 65,536 UTF-8 字节的 body 为 413；损坏的持久关联图安全返回 503。成功和错误响应均禁止缓存。

`GET /api/agentcomputer/v1/runs/{runID}` 在同一数据库事务核对取消命令、journal 与事件后返回 `cancelRequested`。它只表示该 Run 已接受持久取消请求：本轮已实现的未派发原子 `stopped` 和已交接待取消两类路径均返回 `true`；最终结果应读取 `state`，不能把该布尔值显示为“已经停止”。未来若让已交接 Run 走到 `stopped` 等终态，须同步扩展取消 journal 的终态校验并补回归测试。原排队命令收据与历史收据的格式不变。

## 执行边界

只有 `queued/notDispatched`、`executionEpoch=0` 且无领取/派发事实的 Run，才能在一笔事务中记录请求和 `run.finished/stopped`，并释放 Task/Project 双活动占用。`claimed`、`dispatching`、`engineAccepted` 以及运行或等待中的 Run 只记录 `cancelRequested`，保留双占用并拒绝该 Run 的新领取、模型步骤和工具交接；已经交接的工具仍可写入真实迟到结果。响应丢失时，客户端用原命令 ID 查询收据，再读当前 Run；不得根据 HTTP 201 推断引擎或子进程已停。

当前本机 8090 的持久项目仍是 `providerUnavailable`，没有可由客户端生产创建的 Task/Run；成功取消路径只能由隔离夹具验证。公开路由的存在不解除 `capabilities` 的执行门禁，不开启审批、提问提交或模型执行。完整 M1-08 仍需可信生产 Run 生命周期、HITL/问题触发、引擎安全续跑、Run 专属停止句柄与进程停止核验；M0 的真实模型和盒子部署条件另行验收。

## 本轮验证

`make all` 通过格式、Ruff、严格 mypy（598 个源文件）及非 live 测试：5243 passed、23 skipped。定向 91 项覆盖公开取消、状态投影、原能力和恢复查询；跳过项不计为通过。本地 8090 经完整备份后升级，schema25、控制身份、原 Project 收据字节和计数保持；新接口的未知 Run 404、非法输入 422、无认证/URL token 401、超限 413、typed OpenAPI 与 Scalar 均核对。真实库中 Task/Run、取消记录和模型调用仍为零；原 8088 健康检查为 200，同目录第二 daemon 被 `CONTROL_PLANE_BUSY` 拒绝。live PostgreSQL、真实 Provider、Ubuntu 盒子及客户端尚未验收。
