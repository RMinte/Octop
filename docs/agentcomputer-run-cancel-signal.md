# AgentComputer 精确 Run 的本地取消信号（第十七轮）

第十轮已经允许本人对明确的 Run 提交持久取消请求，但已交接引擎的 Run 只会留下 `cancelRequested`，此前没有把该请求送到仍在执行的本地调用。本轮增加一个按 Run 身份登记的进程内信号通道：请求先在数据库提交，再尝试取消同一 `OctopServer` 中匹配的 `asyncio.Task`。可信调用登记句柄时，这条通道可以缩短本地执行继续运行的窗口；HTTP 收据仍只证明持久请求已被接受。

## 范围与顺序

- 开工基线是主目录 `feature/agentcomputer-foundation @ 64472405bb8205ba37124b05d8be5f0890288282`，当时工作树干净。本轮不改数据库 schema、adapterVersion 或 capabilityRevision，也不启用生产 Run worker、项目 ready 或受控 Turn 写入。
- `OctopServer` 持有自己的 `ActiveRunRegistry`。可信引擎调用 `invoke_engine_entry(..., active_runs=registry)` 时，完成固定上下文和工作区校验后，才在消费 `agent.stream(...)` 期间登记当前 task；退出时撤销登记。本轮仍保留未提供 registry 的调用方式；后续已将该参数改为必传。
- 登记保存完整不可变的 `EngineInvocation`，以 owner 和 Run ID 查找活动 task。登记拒绝同一个 Run 或同一 task 的重复占用；有活动登记时也拒绝另一个事件循环访问，全部注销后可在新循环复用。它是进程内活动句柄，不是可恢复的持久停止证明。
- 认证 `POST /api/agentcomputer/v1/runs/{runID}/cancel` 先调用原 `cancel_run`，持久化取消 journal、命令收据及事件。事务成功返回后，才用认证 owner 和原收据尝试发送信号。仅 `status=cancelRequested` 且收据的 box、Run、Turn、Task、Project、恢复代次和执行代次均与活动 invocation 匹配时，才对对应 task 调用 `cancel()`；没有登记、已结束、已发信号或身份不符都不发信号。接口不调用普通聊天的 thread 级取消。
- 发送信号不等待 stream 或远端服务结束，也不改写原取消收据。若调用方把 `CancelledError` 传出当前 `dispatch_run`，已有保守 fence 会将不确定的已交接 Run 置为 `interrupted/reconciliationRequired`，保留 Task 和 Project 的双活动占用。它不会将该 Run 写为 `stopped` 或释放许可。

## 本地验证

`tests/integration/test_agentcomputer_cancel_api.py` 的隔离 HTTP 夹具建立真实控制面 Run 接受图和引擎入站收据，让同一服务器的活动 task 停在可信回调内。认证取消返回 `cancelRequested`；夹具在 task 收到取消时核对数据库中已存在取消记录，并验证普通 thread 取消入口从未调用。取消传播后，Run 当前视图是 `interrupted`、`reconciliationRequired=true`、`cancelRequested=true`，Task/Project 仍指向原 Run。定向取消 API 集成测试的中间结果为 `10 passed`；最终全套检查以本轮交付记录为准。

这一夹具只覆盖控制面提交顺序、本地 task 信号和保守 fence。第十六轮的阻塞 SSE 夹具另行证明 harness 在所测阻塞点可停止本地流消费；两项证据没有合成一次真实 GLM 的 Run 取消实验。没有信号或进程重启后丢失活动登记时，持久 `cancelRequested` 仍可查询，不能由此推断远端模型、工具子进程或账单已停止。

## 尚未开放的能力

本轮不装配生产 worker，不把已接收的 Run 自动派发到真实模型，也不实现已交接 Run 的 `stopped` 终态。`cancellation` 和 `runs` 的客户端可用能力仍关闭；现有 `POST /runs/{runID}/cancel` 可持久接收命令，但 `status=cancelRequested` 不能显示为“已停止”。完整 M1 仍需真实 Run → 引擎生命周期绑定、模型/工具及子进程停止核验、晚到效果对账、可恢复的终态转换和事件/快照；M0 的目标盒子 Qwen、隔离与资源实测继续阻塞生产放行。
