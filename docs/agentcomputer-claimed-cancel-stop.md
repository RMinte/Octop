# AgentComputer 已领取但未开始派发的 Run 取消（第二十轮）

第十九轮保证持久取消后及时排队精确 Run 本地信号。本轮只扩展一个可证明尚未进入引擎的终态：Run 已被可信 worker 领取，但派发事务还没有写入 `dispatch_started_at`。这与已开始派发或已获引擎回执的 Run 不同；后两者仍只有 `cancelRequested` 事实，不能据本地信号释放活动占用。

## 判定与原子提交

- 开工基线是主工作目录 `feature/agentcomputer-foundation @ 2e98bfbf77340c7c509052c3f6b73e9bad3fa30a`，工作树干净。取消与 `_begin_dispatch` 均先锁 owner；取得锁的事务先完成，另一方只能观察其结果，不得用旧 revision 跨过新事实。
- 安全停止仅允许 `state=queued`、`dispatch_phase=claimed`、`execution_epoch>0`，且派发 journal 与 Run 身份、restore/execution epoch、固定 intent 一致，journal revision 为 1、`dispatch_started_at` 和所有引擎回执字段为空，Run 无待对账标记。原有 `notDispatched/execution_epoch=0` 路径继续有效。
- 取消事务先提升 Run revision 并写入 `run.cancelRequested`、原命令收据及取消 journal；在同一事务内以严格 SQL 条件把未开始派发的 Run 写成 `stopped`，追加 `run.finished`，再释放 Task 和 Project 两条活动占用。任一步失败整笔回滚，不留下只有一条 lane 已释放的状态。
- 已领取的派发 journal 保留为历史，`dispatchPhase=claimed` 与停止时的 `executionEpoch` 不抹除。原接收收据和取消收据保持不可变，取消回放仅返回原收据并标记 `replayed=true`。旧 claim 在下一次派发入口被持久取消事实拒绝，启动恢复不重新派发已停止的 Run。
- 若 `_begin_dispatch` 先提交 `dispatching` 和 `dispatch_started_at`，取消只能写 `cancelRequested` 并保留双活动占用；即使当时还没有引擎回执，也不能把已开始交接当作未派发。

## 验证与剩余门禁

单元与 HTTP 集成测试覆盖停止收据、持久事件、两条 lane 同时释放、原收据回放、旧 claim 拒绝、新 Turn 可重新获得占用，以及派发与取消两种事务赢家。故障注入核对 `run.finished` 写入失败或单条 lane 释放失败时整事务回滚。定向组合 38 passed；完整 `make all` 5306 passed / 23 skipped，Ruff 与严格 mypy 通过。

本轮不改 schema、adapterVersion、capabilityRevision 或生产能力开关。生产 worker 仍未装配；已开始派发的 Run 仍需可信执行句柄、服务关闭时完整任务排空、远端模型及受管子进程停止证据、晚到工具效果对账和独立的持久终态 CAS，才能考虑写 `stopped` 并释放双活动占用。
