# AgentComputer 无工具 Run 的自然完成

本轮把可信 Harness 流的**自然完成**接到控制库，但只接受已有 v2 历史归档和已初始化的持久 SQLite 检查点共同证明的一次无工具、无暂停 Turn。普通 `agent.stream()` 返回、应用入站 ACK 或历史 Turn 单独标记 `complete` 都不能释放 Task/Project 占用。当前公开 Task/Run 创建和生产执行能力仍关闭；这里的调用入口由隔离的可信 Harness 测试覆盖，不会从 HTTP 请求直接创建 worker。

## 完成所需的三组事实

1. 固定 `DispatchIntent` 已通过应用入站 ACK，原命令、Turn 快照、claimant、恢复代次和执行代次仍与 Run 对应。
2. 历史库中的**同一业务 `turnID`** 是 v2 `complete`，归档身份、线程、专家、原始 Human 消息 ID 和正文完全相同；只接受一条非空、无工具调用/错误的最终 AI 回答。探测只读，缺失或损坏不会新建归档。
3. 已初始化的 `AsyncSqliteSaver` 保存同一引擎请求的根检查点，原输入和元数据绑定正确，且最新固定检查点没有 pending writes、图没有后续节点/任务，最终 AI ID 和内容摘要与历史回答相同。`HarnessAgent.cancel(thread_id)` 可能令流正常 EOF，但测试在 token、model update 和 after-model update 三处取消时均留下非终态检查点，因此 EOF 本身不进入收据。

可信入口先结束历史写入，再读取两组外部证据。任一证据不满足时，持久检查点路径会抛错，让原派发的异常 fence 把已交接 Run 保守收紧为 `interrupted/reconciliationRequired`，双活动占用保留。内存检查点只用于隔离夹具，不作为持久自然完成证据。

## 控制库提交

v26 的 `ac_run_terminals` 保存单 Run 唯一的规范完成收据及摘要，并将 Run、Turn、项目、任务、派发、引擎请求、claimant 和两个代次绑定到原图。控制库在同一事务内按 owner → Run 顺序加锁，再核对原接收/派发/工具效果/暂停/取消事实以及 Task/Project 双活动占用。只允许未对账的 `queued + engineAccepted`、当前 revision、**零工具效果、零暂停、零取消**；接着执行 Run 成功 CAS、写完成收据、写 `run.finished`，最后释放两条占用。`run.finished` 在 Task 释放前写入，因为事件序号仍由活动 Task 分配。

写入任一环节失败都会回滚整个控制库事务。取消先提交则完成拒绝；完成先提交则后来的取消无法把终态改写。`GET /runs/{runID}` 读取成功 Run 时会反向核验完成收据、唯一成功事件和两条已释放占用，缺失或篡改报存储数据错误。v25 已部署数据库通过成对 v26 SQLite/PostgreSQL 迁移升级；若记录水位为 26 却缺完成表，且已经有成功 Run，启动拒绝用空表掩盖丢失的收据。

历史库、检查点库和控制库仍是独立持久化单元。若历史已经写成 `complete` 而控制库成功事务尚未提交，进程退出后启动恢复仍将未终结 Run 收紧并保留占用，不能据历史自动补成功。完成收据记下当时的历史与检查点观察摘要；它不证明远端模型停止、工具副作用，也不允许从旧检查点重播执行。人工损坏控制库并伪造 Run/双占用释放的情况仍须在后续修复入口和新 Turn 接收路径进一步拒绝。

本轮隔离验收覆盖真实 Harness 自然完成、历史/检查点固定身份、三处取消后的正常 EOF、空回答、原收据与完成收据篡改、取消竞争、写入故障回滚、后续同线程 Turn，以及 v26 升级和已标水位缺表情形。完整工程门禁与实际提交记录见[集成入口](agentcomputer-integration.md)；目标 Ubuntu 盒子、生产受限 worker、真实工具授权与效果、已交接 Run 停止证明仍未验收。
