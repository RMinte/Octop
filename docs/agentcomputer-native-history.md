# AgentComputer 受托 Run 的原生历史 Turn 绑定

本轮把内部受托引擎入口的业务 `turnID` 用作 Octop `HistoryArchive` 中的原生 Turn ID。它解决的是**身份关联和本地流内容落盘**：同一 Task 的 Octop 线程历史中，用户消息与已捕获的模型输出归属于这个固定 ID。此入口仍只供可信内部派发使用；项目 `ready`、生产 worker、公开 Task/Run 执行和远端停止证明均未开放。

## 写入顺序与边界

1. Turn 接收事务先在控制库固定 Run、Turn、输入快照、用户消息 ID 和 `octopThreadID`；派发事务再固定 claimant、双 epoch 与 `engineRequestID`。这些值来自经校验的服务端意图，不由调用者在引擎入口重新选择。
2. `EngineEntryMiddleware` 校验请求和工作区绑定后，先提交持久的 `EngineEntryReceipt`。已有收据阻断同一派发再次进入模型循环。然后通过 `HistoryArchive.begin_bound`，以原业务 UUID 建立 v2 历史 Turn，在模型调用前持久保存固定的用户消息。
3. 引擎使用公开 `HarnessAgent.stream(..., protocol="langgraph")`。受托 hook 之前，Harness 可能先发原始输入快照和不含消息的 `before_agent` 设置事件；输入已由固定用户消息保存，只有严格匹配的这些前置事件会略过，其余前置输出会拒绝。受托 hook 建立历史后，每个流片段交给 `RecordingTracker`，写入结算后才继续消费；自然结束、异常或本地取消均结束该历史 Turn。异常保留失败状态，取消保留 `interrupted` 与已落盘片段。工具调用仍由受托 Middleware 拒绝，历史记录不能授权工具效果。

`begin_bound` 只接受规范 UUID。既有同 ID、错专家归属，或同线程存在未结束/暂停 Turn 时拒绝建立新 Turn；它不把重复 ID 解释为恢复许可，也不自动重放模型。普通 Octop 聊天继续使用 `HistoryArchive.begin` 自行生成 ID，这项绑定不改变普通 Gateway 的历史策略。

## 可用性与故障语义

`history_v2_enabled` 默认是 `false`，且没有现存归档文件时运行时可能没有 `HistoryArchive`。受托入口要求已启用的 v2 归档；缺失、关闭、ID 冲突或首次用户消息写入失败都会在模型前阻断。这个要求不是把生产执行能力自动打开：当前真实项目的 `executionReady=false`，客户端不能因为存在历史 API 或内部测试便显示“可执行”。版本化归档当前只支持 SQLite 控制库；PostgreSQL 生产路径仍待单独设计与验收。

控制库和 `history_v2.sqlite` 使用**彼此独立的 SQLite 事务**；归档 Turn 建立与初始消息写入也不是同一次提交。持久入站 ACK 已提交、原生历史尚未建立或尚未保存初始消息时，进程可能退出或归档可能失败；这时 Run 必须保留待核实/保守对账，不能伪造一个成功历史 Turn。反向的归档终态也不等于 AC Run 已终结：模型流结束后，Run 完成收据、公开事件/快照和工具结果仍需各自的可信持久证据。历史 ID 相同只证明关联，不能声称跨库原子提交、原生 exactly-once、远端模型停止或完整恢复。

## 验收范围

隔离测试核对：正常真实 Harness 流的历史 Turn ID 等于业务 `turnID`，用户消息 ID 与内容固定，回答可从原生历史读取；重复派发不创建第二 Turn 或第二次模型调用；缺失/关闭归档、ID 冲突、首次写入失败均在模型前拒绝；本地取消不把同 ID 的历史标为完成，并保留此前已持久的片段。受托引擎定向回归 65 passed，完整 `make all` 为 5333 passed / 23 skipped。目标 Ubuntu 盒子、真实 GLM 托管 worker、跨进程停止与恢复未在此切片验收。

相关边界见[内部派发与接收证据](agentcomputer-dispatch.md)、[保守启动恢复](agentcomputer-recovery.md)和[版本化历史归档](versioned-history.md)。
