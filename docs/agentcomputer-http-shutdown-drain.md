# AgentComputer HTTP 操作的服务关闭等待（第二十二轮）

第二十一轮让显式登记的可信 Run 派发任务在服务关闭前完成持久对账。本轮补齐另一条生命周期：AgentComputer HTTP 请求断开后，其数据库线程或项目准备过程仍可能继续。服务不能先关闭数据库和 daemon 所有权锁。

## 接线与顺序

- 开工基线是主工作目录 `feature/agentcomputer-foundation @ f12a34fba72f2fdd3ac228b624fdd7fb425d17f3`，工作树干净。schema、adapterVersion、capabilityRevision 和业务能力开关不变。
- 服务器为 AgentComputer HTTP 操作持有独立登记表。路由在首次异步等待前登记内部任务，请求通过 shield 等待；HTTP 请求取消只结束这次响应等待，不取消已经登记的线程或项目准备。登记覆盖该路由的查询、Turn 命令、项目接纳与完整准备、Run 取消事务及提交后的精确本地信号。
- 关闭先封闭新的 AgentComputer HTTP 操作入口，等待已登记操作结算；再取消并等待已登记的可信 Run 派发及其持久 fence；最后关闭运行时、数据库连接池，释放 daemon 锁。取消请求的信号在已登记的内部协程中于事务提交后发出，完整落在关闭等待内。对请求已消失后的后台异常仅记录异常类别，不记录用户输入或命令内容。
- AgentComputer HTTP 中间件在关闭门生效后、认证或设置检查读库前返回 `503 runtimeUnavailable`；关闭中的响应不再读取数据库以滑动续期。同一服务器实例完整停止后重建空登记表，才能重新接收请求。

## 验证与限制

确定性集成测试让取消事务、项目接纳事务与一个只读查询分别阻塞在线程中，然后取消 HTTP 请求并启动服务关闭，核对数据库连接池与 daemon 锁在操作结算前仍可用。项目事务结束后，原收据和停止的专家/工作区准备状态均落盘；取消事务保持原收据；关闭时新命令及关库后段的新认证请求返回受控 503。同实例重启后仍能查询原项目和接收新的项目命令。精确 Run 信号的原有正常与请求取消竞争测试继续覆盖。

相关集成回归 73 passed；完整 `make all RUN='uv run --offline --no-sync' PYTEST_JOBS=4` 通过，5317 passed / 23 skipped，Ruff 和严格 mypy 通过。最初在受限沙盒内运行的 18 个回环端口用例因 `PermissionError: [Errno 1]` 失败；在允许本机回环绑定的环境中重跑完整门禁后通过。跳过项不计为通过。

保护范围是 AgentComputer HTTP 路由及其认证入口，不代表普通 Octop HTTP 请求、未登记的内部派发、远端模型或工具子进程获得关闭等待。已经交接的 Run 仍不能因本地任务退出就宣称 `stopped` 或释放双活动占用。生产 worker、项目 `ready`、Task/Run 公开创建、客户端接线和目标盒子实机门禁仍关闭。
