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

## 启动时的只读历史对账

daemon 在运行时创建前先保守收紧已派发 Run。对于**确实收紧、且在 Run 锁内已取得引擎入站 ACK** 的记录，扫描随后按固定 `turnID` 只读探测 `history_v2.sqlite`：先核对归档记录的控制库绝对路径身份，再按 Turn 主键取行，核对 v2 segment 的 Octop thread 与托管专家。没有 ACK 的 claimed/dispatching Run 尚未进入 `begin_bound`，因此不探测，也不将缺失历史误报为故障。晚到 ACK 若在收紧前已提交，则按锁后实际收据进入探测。

内部 `RecoveryReport.history_observations` 只汇总这些观察到的状态：归档缺失/不可用、归档身份或 Turn 归属不匹配、Turn 缺失，以及 `active`、`paused`、`interrupted`、`failed`、`partial`、`complete`。`history_initial_messages` 单独计数固定初始消息文档是否已落盘；存在 Turn 而没有该文档是允许的崩溃窗口。daemon 将非空汇总写入启动警告日志，不包含 Run、用户或消息正文。计数只对本次成功收紧的 Run 增加，重复扫描不会把同一事实重复计入，也不积攒无限的逐 Run 报告。

探测不调用可能新建库和 `.required` 标记的 `HistoryStore` 构造器，而使用 SQLite `mode=ro` 打开已有文件；读 WAL 时 SQLite 仍可能创建共享内存辅助文件。缺失文件不会被重建，损坏或不匹配不会被当作“空历史”。这些诊断**不写入控制库 Run/事件作为完成证据**：无论读到 `complete`、`partial`、无 Turn 还是归档不可用，既有 `interrupted/reconciliationRequired`、原派发收据和 Task/Project 双活动占用都保持。原生历史没有 boxID、restoreEpoch、runID 或工具效果证明，因此不能据它恢复执行、释放占用或宣布远端模型已停止。PostgreSQL 控制库当前没有 v2 历史支持，探测报告归档不可用。

## 验收范围

隔离测试核对：正常真实 Harness 流的历史 Turn ID 等于业务 `turnID`，用户消息 ID 与内容固定，回答可从原生历史读取；重复派发不创建第二 Turn 或第二次模型调用；缺失/关闭归档、ID 冲突、首次写入失败均在模型前拒绝；本地取消不把同 ID 的历史标为完成，并保留此前已持久的片段。受托引擎定向回归 65 passed，完整 `make all` 为 5333 passed / 23 skipped。目标 Ubuntu 盒子、真实 GLM 托管 worker、跨进程停止与恢复未在此切片验收。

只读对账补充测试覆盖 ACK 后无归档、已有归档无 Turn、Turn 已建立但初始消息未写、六种历史状态、错线程/专家/格式/归档身份、晚到 ACK，以及重复扫描继续保持双活动占用。它仅验收诊断和保守收紧，不验收自动修复、Run 自然完成或生产 worker。

相关边界见[内部派发与接收证据](agentcomputer-dispatch.md)、[保守启动恢复](agentcomputer-recovery.md)和[版本化历史归档](versioned-history.md)。
