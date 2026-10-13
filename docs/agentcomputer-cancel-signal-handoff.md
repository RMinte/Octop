# AgentComputer 持久取消后的本地信号交接（第十九轮）

第十七轮把认证 Run 取消请求接到同服务器的活动 task；第十八轮用真实 harness 的阻塞 SSE 夹具验证这条本地链。但原路由在数据库操作返回到 HTTP 请求协程后才发信号：若请求协程恰在事务提交后被取消，持久 `cancelRequested` 已存在，本地活动 Run 却可能收不到通知。本轮只收紧这个交接窗口。

## 实施边界

- 开工基线是主工作目录 `feature/agentcomputer-foundation @ 383baaee3052f51231ec8b997ec57c531e08a75d`，工作树干净。不改 schema、adapterVersion、capabilityRevision、收据格式或生产能力开关。
- `POST /api/agentcomputer/v1/runs/{runID}/cancel` 仍在 executor 中执行原有 `cancel_run`。它成功返回时，事务已经结束；同一 worker 随即用捕获的事件循环 `call_soon_threadsafe` 排队调用 `ActiveRunRegistry.signal_cancel`，之后才向请求协程返回原收据。因此请求协程在等待 executor 期间被取消，不再跳过已经提交的请求对应的本地信号排队。
- 线程只负责向事件循环排队；完整 Run 身份核对与 `task.cancel()` 仍在登记所属的循环中进行。路由不调用普通聊天的 thread 级取消。若循环已经关闭，排队会被拒绝并记录不含 Run 身份的警告；数据库里已提交的原收据不会因此被改写。即使排队成功，循环在回调执行前停止时也不能保证本地信号送达。
- 已交接 Run 收到信号后的保守规则不变：持久取消事实仍为 `cancelRequested`；取消传播后可变成 `interrupted/reconciliationRequired`，Task/Project 双活动占用继续保留，不能据此写 `stopped` 或释放许可。

## 验证

`tests/integration/test_agentcomputer_cancel_api.py` 为同一精确 Run 场景增加确定性竞争夹具：执行线程在取消事务成功提交后、返回路由之前等待；测试取消 HTTP 请求协程，再放行线程。夹具通过原命令收据查询确认持久事实，核对活动 Run 仍收到取消、普通 thread 取消没有被调用，最终 Run 保守待对账且双活动占用未释放。正常收到 HTTP 201 的路径也继续覆盖。该文件定向结果为 11 passed。

这只证明所测进程内事件循环和已运行 executor 的交接，不证明远端模型、工具子进程或计费停止。生产 worker 仍未装配；未来启用前，还需在服务关闭时先取消并等待活动 Run，再关闭数据库和释放 daemon 锁，并建立执行停止、晚到效果对账与持久终态的独立证明。
