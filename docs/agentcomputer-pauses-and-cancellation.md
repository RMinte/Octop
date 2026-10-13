# AgentComputer 持久暂停与精确 Run 取消（第九轮）

这一轮新增 `025_agentcomputer_pauses.sql` 与对应 PostgreSQL 迁移，保存审批、提问和取消请求。它们是受信内部运行边界；当前安装没有生产 Run 执行入口、Run 专属停止句柄或客户端写入端点。`adapterVersion=0.9.0`、`capabilityRevision=9` 继续如实声明 `approvals`、`questions`、`cancellation` 不可用，`supportedPermissionModes=[]`。现有 Octop 普通聊天与专家入口不由这些记录驱动。

## 固定暂停事实

`ac_run_pauses` 以 `pause_id` 保存一种 `approval` 或 `question`，并固定所属 box、owner、Project、Task、Turn、Run、dispatch/engine request、claimant、恢复代次和执行代次。意图中的 `RunPauseContent` 保存规范 JSON 对象、SHA-256 摘要、工作区摘要、策略版本和能力版本；记录有效期与独立修订。它属于可信适配器输入，不表示已经挂接真实工具目录、路径授权或用户界面。

可信引擎调用暂停内核前必须有原应用接收回执、真正处于 `running` 的 Run、原 Task/Project 双活动指针、当前 owner/代次/权限模式及无待核实工具效果。`running` 不能仅由应用接收回执推定；生产模型开始证据尚未接线，现阶段只有隔离夹具可建立这个状态。内核在同一数据库事务中写入固定暂停意图、把 Run 改为 `waitingApproval` 或 `waitingInput`、追加业务事件。一个 Run 同时只能有一条待收敛暂停。

审批决定只支持对这条固定动作一次性 `approveOnce` 或 `deny`；问题只保存明确答案。命令带稳定 `commandID`、`idempotencyKey`、当前 `expectedRestoreEpoch`、目标 pause/Run 和 `expectedRevision`。盒子核对原意图、当前固定内容、有效期、取消事实和可信实时授权后，以 CAS 在同一事务写裁决及事件。精确重复返回原裁决，不同参数、失效版本、旧代次或不同目标冲突。裁决状态是 `adjudicatedOnly`：它没有给引擎发许可、恢复等待中的运行，也没有执行工具。未来要继续执行，仍需原动作、权限、工作区与引擎检查点的实时复验。

`ac_run_pauses` 的请求、裁决、过期事件和原记录逐项绑定。缺失记录、摘要/版本/事件损坏或孤立事件使读取失败，不将旧暂停当成新请求重建。公开 Run 查询保持原 12 字段，并在同一连接内校验这些隐藏事实；未实现审批卡片与公开提交接口。

## 重启和过期

前台 daemon 持有安装目录生命周期锁后，按固定高水位、每页最多 100 条扫描本盒子仍处于 `pending`/`decided` 的暂停。恢复只把无法安全重建的暂停标记为 `expired`，把相应 Run 收紧为 `interrupted`/`reconciliationRequired`，保留 Task 与 Project 的双活动占用；已裁决内容和原收据保留以供核对。重复扫描不重复写入。它不根据旧审批调用引擎，也不释放待核实的工具效果。

## 精确 Run 取消

`ac_run_cancellations` 对一个 Run 最多保存一个取消请求，并通过 `ac_commands` 固定原命令、归属、幂等键、载荷摘要和不可变收据。`run.cancelRequested` 与记录、Run 修订同事务提交。只允许对明确的 `runID` 请求取消；不能把当前 thread 的最新运行当成目标。

若 Run 仍为 `queued/notDispatched`、执行代次为零且不存在派发，数据库事务可以证明它未交给引擎：它随后写 `run.finished/stopped`，再清除 Task 与 Project 指向该 Run 的活动占用，整体失败则回滚。对 `claimed`、`dispatching`、`engineAccepted` 或运行/等待中的 Run，只记录 `cancelRequested`，保留双活动占用。现阶段没有 Run 专属引擎/子进程停止证明，因此这类请求不能写 `stopped`，也不能调用按 thread 取消的旧接口去影响下一回合。

取消记录会阻止同一 Run 后续领取、模型步骤和新工具交接；已经交接的工具若晚到真实结果，仍可记入原效果 journal。恢复扫描只按既有未知执行规则收紧，不能把“进程重启”解释为“用户取消已完成”。原命令收据可经已有本人 `GET /commands/{commandID}` 查询；`GET /runs/{runID}` 在同一事务核验取消命令、journal 和事件，删除任一原事实不能静默重新取消。

## 本轮验收边界

隔离夹具覆盖同键重放、版本/归属/代次冲突、暂停失效和恢复、取消与派发竞争、未派发原子停止、已派发保守请求、事件失败回滚以及丢失 journal/事件拒绝。SQLite 是本地执行存储；PostgreSQL 维护成对 DDL 与约束，但尚无真实 PostgreSQL 服务验证。完整 M1-08 还需要生产 HITL/问题触发器、原生引擎继续入口、Run 专属停止句柄和子进程停止核验。M0 的模型与隔离部署、真实盒子环境、客户端远端运行仍待后续步骤。
