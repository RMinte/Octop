# AgentComputer 内部派发与接收证据

第六轮细化 M1-06 的内部 claim、派发前提交、应用接收收据与未知交接处理。
当前 adapterVersion=0.6.0、capabilityRevision=6；完整运行能力仍为 unavailable，权限模式为空。
本轮没有生产 worker、启动调度、HTTP claim/dispatch/recover 路由或运行开关，不开放真实模型执行。
已有对外端点与原始收据格式保持；本人 `GET /api/agentcomputer/v1/runs/{runID}` 展示实际持久状态。

基线为 `feature/agentcomputer-foundation @ ce3f0b5830cf79c497fb98f31973c581034d8111`。
交付状态、最终工程门禁和本地证据见 [集成入口](agentcomputer-integration.md#第六轮交付状态)。

## 接收证据的范围

`EngineEntryReceipt` 是 Octop 应用控制层保存的入站证据，表示固定 Run/Turn 的交接已被应用接收。
它不等于 octop-harness 原生的幂等执行收据，不证明模型已开始，也不证明执行完成。
本轮没有为 `HistoryArchive.begin` 增加原生幂等能力，未创建或预留与引擎原生 History Turn 绑定的 ID。
业务 turnID 与未来原生历史回合仍需明确衔接，不能把应用收据的业务 turnID 当作原生历史身份。

公开 harness 的 beforeAgent、beforeModel 与工具拦截边界用于核对固定入站及拒绝工具执行。
相关离线测试使用纯 Fake Memory model 和合成输入；它们验证接线与门禁，不能替代真实 Provider 协议、模型续轮、工具隔离或原生引擎恢复验收。
本轮不配置模型服务，不请求真实模型，不通过普通专家 start 或聊天入口绕过托管门禁。

## 内部身份与持久 journal

配对 023 迁移增加每个 Run 唯一的派发 journal；原 021/022 数据及原始命令收据保持。
claim、固定派发意图、领取主体、版本与应用接收确认通过 caller 的同一连接写入；不在事务内二次借用 PostgreSQL 连接。
journal 是控制层事实，不能当作原生 harness 的执行检查点。

| 对象 | 事实范围 |
|---|---|
| `DispatchIntent` | 固定 owner/box/project/Task/Turn/Run、原消息与快照、派发及请求身份、restore/execution epoch 与规范摘要 |
| `RunClaim` | 固定意图、claimantID、当前 runRevision/dispatchRevision，以及已确认 EngineEntryReceipt 或明确 null |
| `EngineInvocation` | 已提交 dispatching 后的固定意图、claimantID 和 expectedRunRevision |
| `EngineEntryReceipt` | schemaVersion=1、receiptID、dispatchID、engineRequestID、Run/Turn、双 epoch、intentDigest 与 UTC acceptedAt |

所有内部身份来自已经核实的原接受图或服务器生成的稳定身份，不来自客户端正文。
旧历史、损坏 journal 或丢失字段不通过类型默认值补事实；读取与重放拒绝错 owner、错 Run/Turn、快照、代次或摘要。
应用接收以固定身份核对并保存原结果，重复入站返回原确认，不把重复调用当作新模型执行。

## 阶段含义与当前查询

| `dispatchPhase` | 可以证明 | 不能据此推断 |
|---|---|---|
| `notDispatched` | 当前耐久事实中未提交交接 | 原接受收据本身不证明运行已开始 |
| `claimed` | 固定 worker 已领取并保存意图 | 不证明已交给模型，也不能按 TTL 自动抢占 |
| `dispatching` | 交接意图已提交，随后可能已经进入 callback | 没有 ACK 不证明没有开始或没有效果 |
| `engineAccepted` | 应用控制层已保存固定入站收据 | 不证明原生 harness 恰好一次、模型 started 或 completed |

Run `state` 与 `dispatchPhase` 是不同维度。只有应用入站确认时，Run 仍可为 queued；不能仅凭 engineAccepted 写 running 或 `run.started`。
未来开始、工具效果与完成分别需要相应真实证据，不能由网络连接、回调返回或最终文本猜测。

GET Run 字段保持为 runID、turnID、taskID、projectID、state、dispatchPhase、executionEpoch、restoreEpoch、revision、reconciliationRequired、UTC createdAt/updatedAt。
不添加 executionReady、claimant、内部 engine receipt、宿主路径、原生 history ID、审批或未实现 cursor。
Project/Task 的 executionReady 继续 false；阶段变化不会开启能力。
查询只读本人事实，管理员无跨 owner 绕过；未知和其他用户对象同 404，坏事实安全 503/runInvalid。
原 CommandReceipt 的 runState=queued、acceptedAt、restoreEpoch、消息/Turn/Run、sequence 与原 JSON 保持，不用当前 Run 状态覆盖原 ACK。

## 领取与交接顺序

1. 内部 claim 先校验真实执行能力及固定映射，再以 expected revision 进行条件领取；生产能力关闭时任何 journal、Run revision 或事件写入前拒绝。
2. 同事务保存唯一 journal、固定意图、领取主体与 execution epoch，更新 Run 的 claimed 事实。
3. 交接前重新核对固定快照、两级执行许可、当前主体与版本；先耐久写入 dispatching，再在数据库事务之外调用内部 callback。
4. callback 携带不可变 `EngineInvocation`；应用入口核对固定 Run/Turn、双 epoch、主体、意图摘要与 expected Run revision 后保存应用接收收据。
5. 调用完成后依据已保存的收据核对确认，不以 callback 的返回值拼造 ACK。同意图确认重放读取原收据，不再调用新 callback。
6. 迟到或改变身份/快照的回调、旧 claim 与错代次确认拒绝；不覆盖当前状态、不释放许可、不重新启动模型。

这里没有轮询消费者或真实调度。Fixture 可以调用上述内部函数并注入合成 callback，生产启动代码未装配这种派发器。
没有正文、用户配置或环境变量允许客户端把 unavailable 改成 available。

## 未知交接与恢复边界

dispatching 提交后的异常、接收确认丢失或交接后重启，不能自动视为尚未派发。
当前派发器对交接异常/取消收紧为 `state=interrupted`、`reconciliationRequired=true`，保留原 dispatchPhase、Run/Turn 和执行代次；Task/project lane 继续由该 Run 持有。本轮没有启动扫描或自动恢复；重启后的核实需后续恢复流程装配，内部收紧函数可用于已有事实。
Run 没有 unknown 终态。后续工具 journal 的 outcomeUnknown 是效果事实，不能在此补造工具成功、无效果或未调用证明。

`interrupt_uncertain_run` 先按 owner→Run 加锁，再核对固定意图/claimant 和当前 revision，条件更新时吸收晚到应用确认；旧调用方 revision 不阻止收紧同一固定 Run。已保存的 succeeded/failed/stopped 终态不被覆盖，也不触碰后继 Run 的 lane。停用 owner 或旧 restoreEpoch 可以收紧旧交接，不获得任何执行权限。
取消数据库线程并不能中断事务；派发器等待后台提交结果后再收紧，不调用已取消的新交接。重复取消也不能跳过持久边界。不按时间或 lease 过期释放许可。
确认丢失时不创建第二个 dispatch/request 身份，不把未知交接改回 queued，不自动 reclaim，也不重放普通聊天消息。
本轮没有 claimed 的自动回收、重新排队或 TTL 方案；没有引擎进程/子进程停止证明和人工处置时，继续阻塞后续执行。
后续恢复观察、确认未派发后的重试、用户明确新回合及效果对账分别进入独立门禁。
控制面关闭和请求断线不等于模型、工具或沙箱已停止；本轮没有取消端点。

## 验证与剩余门禁

隔离 Fixture 以真实 stopped 项目资源和原子 Task/Turn 登记为起点，不直接 SQL 伪造派发阶段。
验证领取竞争、版本/主体 fencing、dispatching 先提交、重复入站原确认、交接确认丢失、阶段保留和两级 lane 阻塞。
HTTP Fixture 通过内部流程后核对既有 Run GET 的 claimed/dispatching/engineAccepted/interrupted 投影、本人/admin 隔离、字段不泄露内部身份，以及原 queued 收据字节保持。
生产能力关闭 Fixture 证明 claim 零写；公开 harness 接线仅调用纯 Fake Memory model，工具路径始终拒绝。

工程门禁：最终 make all 的 Ruff、strict mypy（587 源文件）及全部非 live 测试通过，4942 passed / 23 skipped；提交仍必须通过仓库 pre-commit/testmon 与 Dashboard 构建。跳过不计通过；真实模型、live PostgreSQL 和 Ubuntu/Linux/Windows 盒子实机未验收。
本地验证：自有 8090 完整备份后升级 adapter0.6/revision6、schema23；身份、原 Project 收据字节和旧数据计数保持，stopped 托管专家及 failed/providerUnavailable 项目保持。Task/thread/session/Turn/Run/event 与新增派发 journal 均为0，usage/model calls为0；typed OpenAPI 和 Scalar 五个互斥阶段选项已核实，engineAccepted 样例仍为 queued。未知 Task POST/Run GET404、非法正文/ID422、匿名/URLtoken401、超限413；没有公开 claim/dispatch/recover、运行开关或 Task 创建入口，原8088健康200。升级前备份为 /Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-06-before-adapter-06-20261012T152548Z。
本机实验库不写 ready Fixture、Task/Turn/Run 或派发 journal；原 failed/providerUnavailable 项目和 stopped 专家保持。
实际备份、控制身份、凭据和数据库在忽略目录保存，不进入交付仓库。

M1-06 本轮仅内部派发和应用收据部分验证，完整原生引擎幂等/历史绑定、生产项目 ready、真实模型与工具隔离、取消/审批、事件流/快照、恢复 fencing、设备配对及盒子实机尚未通过。
PostgreSQL、Linux/Windows 和盒子环境按最终交付证据分别记录；静态 SQL 对照或跳过用例不能算实机验收。
