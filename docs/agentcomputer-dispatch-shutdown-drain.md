# AgentComputer 可信派发任务的服务关闭等待（第二十一轮）

第二十轮解决已领取但尚未开始派发的 Run 安全停止。本轮处理另一个边界：可信派发任务从数据库写入 `dispatching`、交给引擎，到异常后持久收紧为 `interrupted/reconciliationRequired`，服务关闭不能在这段工作仍使用数据库时先关闭连接池或释放 daemon 所有权锁。

## 生命周期和关闭顺序

- 开工基线为主工作目录 `feature/agentcomputer-foundation @ b8783f0f42ec5bd4c4bcfb55b42e0e0be85c750a`，工作树干净。生产 Run worker 和能力开关仍关闭。
- 本轮先由可信调用方显式传入服务器的 `ActiveRunRegistry`；后续已改为必传（见下文）。外层登记在第一次异步等待前开始，覆盖 `_begin_dispatch` 的持久事务、引擎调用、回执查询和异常后的持久 fence。引擎流保留精确 Run 登记；同一任务、同一 intent 和 claimant 可嵌套登记，不接受其他 Run 或其他任务冒用。
- `OctopServer.stop()` 先关闭登记入口，向已登记任务发本地取消信号，等待这些任务及其持久 fence 全部退出，然后才关闭运行时、数据库连接池并释放 daemon 锁。关闭等待可承受调用者重复取消；同一服务器实例再次启动时建立新的空登记表。
- 工作区校验、引擎入站收据、模型步授权和引擎回执查询中的数据库线程，在异步调用被取消后仍须等同步操作完成才传播取消。此前派发准备及异常 fence 的等待规则继续有效。关闭过程不会替用户创建取消命令，也不会把已交接 Run 写成 `stopped` 或释放 Task/Project 双活动占用。

## 验证与限制

确定性测试覆盖派发事务已提交但工作线程尚未返回、引擎入口工作区校验线程仍在运行、引擎回执写入时的迟到确认、异常 fence 尚未完成、关闭期间新登记被拒绝、重复取消关闭请求以及同实例重启。集成测试检查 fence 完成前数据库仍可访问、第二个 daemon 仍无法取得锁；随后 Run 保守进入 `interrupted/reconciliationRequired`，原收据与双活动占用保持不变。定向测试 45 passed；完整 `make all` 5313 passed / 23 skipped，Ruff 与严格 mypy 通过。

本轮的登记表仍是可信调用方可选的显式参数，当时未传入的内部 `dispatch_run` 不受关闭等待保护。单独调用 `invoke_engine_entry` 虽可等待其本地任务退出，却没有外层派发异常 fence，旧 Run 仍需下次启动的保守恢复。HTTP 命令的独立 executor 工作当时尚未纳入关闭等待，已由第二十二轮补齐现有操作。生产 worker 尚未装配，后续必须强制它走完整登记；本地 `task.cancel()` 和流关闭都不能证明远端模型或受管子进程已经停止。已开始派发 Run 的安全 `stopped` 终态仍需远端/子进程核验、晚到工具效果对账与独立的持久 CAS。

后续强化（2026-10-14）：`dispatch_run` 和 `invoke_engine_entry` 的内部调用现在都必须显式传入 `ActiveRunRegistry`，已删除未登记的执行分支。真实 Harness 阻塞流测试补充了服务关闭并发路径，验证本地流关闭、Run 保守收紧为 `interrupted/reconciliationRequired`，且不伪造用户取消或释放双活动占用。这只约束调用参数；未来生产 worker 仍必须使用当前服务器持有的登记表，并另行证明远端模型与工具子进程停止，才能开放执行能力。
