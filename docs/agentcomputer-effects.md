# AgentComputer 工具效果 Journal

第八轮以 `feature/agentcomputer-foundation @ ab0f0391eaa5f88d236b646862832efa63f71b23` 为基线，交付内部工具效果 Journal 与隔离 Fixture，M1-07 部分通过。
当前 adapterVersion=0.8.0、capabilityRevision=8。024 SQLite/PostgreSQL 配对迁移新增一张 `ac_tool_effects` 表；既有 Run、原接受收据与公开协议保持。
默认 `EngineEntryMiddleware.awrap_tool_call` 继续拒绝全部工具执行，没有新 HTTP 工具/恢复/审批入口、生产 worker 或启用开关。
最终工程和本地验证见 [第八轮交付状态](agentcomputer-integration.md#第八轮交付状态)。

## 意图与调用身份

固定意图属于已登记的 Run/Turn、Task/project/workspace、owner、派发/请求与领取主体，绑定恢复代次、执行代次、助手消息和精确 toolCallID。
意图还保存能力/策略版本、工作区授权摘要、固定参数表示及摘要；不从当前会话、模型回答或客户端新参数补造旧调用。
effectID 是该 Journal 的稳定身份，executionAttempt 只能为 1。同 Run/toolCallID/attempt 不另建第二次执行，响应丢失不能换 effectID 或增加 attempt 来重做。
持久 schema/attempt/epoch/revision 数字严格解码，能力/策略版本是可信适配层提供的非空字符串。缺字段、坏摘要、未知 schema、bool/float/string 冒充数字或归属错配都拒绝；不静默回填。

参数摘要只证明本适配层使用的固定参数表示。Swift 现有参数 JSON 字符串摘要与 Python canonical JSON 对象摘要尚未完成互通验收。
argumentsJSON/resultJSON 必须是 canonical JSON 对象的精确序列化，各自最多 65536 UTF-8 字节，并核对对应内容摘要；不截断正文，也不以宽松重新编码接受另一表示。
operationDigest 绑定 dispatchIntentDigest、claimantID 与完整 action；effectID 和参数不能在重放时替换。结果中的 fileChangeIDs/artifactIDs 各最多64个 UUID，仍是 adapter reported 的关联引用。
意图的业务 assistantMessageID 也不代表原生 AIMessage 或 HistoryArchive Turn 已建立身份映射。
敏感参数与内部身份只供控制面绑定；错误和普通日志不回显参数、宿主路径、正文或凭据。

## 持久阶段与结果

| phase | 已保存的事实 | 后续边界 |
|---|---|---|
| prepared | 固定意图及 prepared 事件已同事务提交 | 尚不能宣称工具成功或没有效果；派发前重新核实授权 |
| dispatching | 交接标记先于 handler 调用提交 | 标记证明控制面进入交接窗口，不证明外部工具已开始或已停止 |
| terminal | 完整、绑定正确的 terminal receipt 与对应事件已同事务提交 | 读取原结果；succeeded、failed、notDispatched 分别表达本次调用结果 |
| outcomeUnknown | 没有可接受的耐久结果，需核实 | 不调用原工具、不生成假 terminal receipt，不解除 Run 对账与双 lane |

`dispatchStartedAt` 的有无保留之前是否已进入交接窗口。prepared 或 dispatching 收紧为 outcomeUnknown 后，phase 可以变化，这个交接事实不能被清除。
同一个 Run 最多一项 prepared/dispatching/outcomeUnknown 效果，由数据库部分唯一索引阻止并发未收敛调用。
terminal failed 虽然不再占用该未收敛效果索引，Run 仍 reconciliationRequired 且保留 Task/project 双 active lane，不能凭索引空闲继续副作用。
succeeded 不表示整个 Run 已完成、后台进程已结束或项目执行许可已经释放。

failed 表示收到了失败结果，可能已经产生部分文件、进程或外部效果；它不能表示回滚或没有效果。
输出的 `effects=unknown` 即使伴随 succeeded，也保留原 terminal receipt 并要求 Run 对账，不能把报告成功当作效果已核实。
outcomeUnknown 表示没有可信耐久结果；空结果、工具抛异常、取消或无 receipt 都不能补成“未执行”。
notDispatched 只用于确认仍在 prepared 且 handler 未进入的拒绝，不用于交接后失败。

## 执行与授权边界

1. 在完整原 Run/dispatch 绑定和现有许可下固定意图；prepared 与业务事件一次事务提交。提交失败不调用工具。
2. 通过可信 caller 的 bool callback 再检查当前授权；该 callback 是本轮内部适配契约，不能等同生产 capability catalog、策略引擎或文件授权已经验收。
3. 授权撤销且确定未交接时保存 notDispatched。身份、epoch、版本或 lane 不匹配不能改写原意图继续调用。
4. dispatching 先提交，再调用一次受控 handler。外部效果与控制库事务不存在原子提交，未知窗口不能靠 SQL 幂等消除。
5. 校验返回身份、结果摘要与有界记录，将 terminal receipt 和事件同事务提交，之后才可返回该耐久结果。
6. failed 或确认丢失将 Run 收紧为 interrupted/reconciliationRequired，保留双 lane。若结果/事件存储也失败，停止推进，不能报告成功；已有 pending 意图和许可继续约束后续调用。

当前没有生产受控 runtime、工具 catalog 装配或实际 Provider。隔离 callback 的授权回答和报告结果是明确的可信测试输入，不是来自模型的自由声明。
工具调用错误可以伴随部分效果，Fixture 使用受控标记模拟这个窗口；本轮不据此宣称 Shell/网络/文件工具执行隔离通过。
实际工作区内容仍须经 `BackendWorkspace`；文件 before/after、可信恢复引用和正式成果发布事务继续按独立契约验收。
结果中的 fileChange/artifact UUID 引用仅为 adapter reported 的关联值，不证明内容已保存、文件变更已核实或成果已正式发布。

## 取消、重放与恢复

生命周期取消先等待正在进行的控制数据库操作结算，才能传播取消与释放资源；不能在后台事务尚未完成时关 pool 或宣称失败已回滚。
prepared 后取消、交接后取消与结果提交失败分别保留真实阶段和交接事实，不能一律伪造用户 Run 取消或 stopped。
prepare 已提交且取消到达时保存 callerCancelled/notDispatched；begin 已提交才收到取消则保守 unknown，不能重新调用 handler。terminal 已提交而取消或返回确认丢失时保留原 receipt，之后精确重放只读取该结果。
Python 请求取消和 App 断线也不是用户授权取消原工具、子进程或沙箱。

重复调用必须核对完整固定身份、参数/操作摘要、能力/策略版本和原记录。同一已完成调用返回原 receipt，不能再次进入 handler；改参数或归属拒绝。
原结果读取保留历史 epoch，停用 owner 或关闭能力不能把原事实改成不存在；这些读取不授予调用权限。新的 prepared/begin 必须通过实时身份、epoch、有效 owner、能力与 lane 校验，旧 epoch 不能开始工具交接。
prepared/dispatching 没有 terminal receipt 以及 outcomeUnknown 记录只允许保守核实，不作为自动重放入口。attempt 不递增，不新建效果身份绕过阻塞。
恢复保留原意图、dispatchStartedAt、代次与已经提交的 receipt。重复收紧不改原事件身份，不重写旧结果或释放双 lane；已经保存的 Run 终态保留。
daemon 在既有 Run 扫描之后扫描工具效果，两类分别固定整数高水位并每页100条 keyset。工具扫描覆盖已经 interrupted、停用 owner 或旧 epoch 的未收敛效果，只做保守收紧。
内部 RecoveryReport 新增 effects_scanned/effects_tightened/effects_unchanged，默认为0；没有候选时不读取或创建控制身份。计数不进入公开 RunView，也不构成完整恢复能力。
Run GET 与内部 effect 操作一并核对原接受图和关联效果；缺绑定、坏版本或结果损坏明确拒绝，公开读取使用安全503，不从旧图补造结果。原命令收据查询继续表达原 accepted 事实，不据后续工具结果改写原 ACK。
只读观察与未来人工处置不能补写“没有效果”；解除许可仍需可信停止范围与独立处置证据，本轮不提供这一入口。

## 对外能力与验证范围

公开 HTTP 路由和原收据格式保持，Run GET 仍 12 字段；本轮不把工具意图、参数、内部 receipt 或额外执行字段暴露给客户端。
projects/tasks/runs 仍 supported/unavailable，commandReceipts 仅已有查询可用；完整 recovery、文件、成果、审批和取消继续 unsupported/notImplemented。
permissionModes=[]、deviceID=null、maxCommandBytes=65536，其余执行限额 null。能力版本提升不授予生产工具权限。

确定性 Fixture 核对 prepared/dispatching/terminal 事务与事件、同 Run 竞争、精确重复调用、撤销前后边界、部分效果错误、确认丢失、结果持久失败、坏数字/版本/绑定，以及未知不重放和双 lane 保持。
工程验证：最终 make all 通过：Ruff、strict mypy（592 个源文件），5190 passed / 23 skipped；跳过不计通过。提交还须通过仓库实际 pre-commit/testmon 与 Dashboard 构建。真实模型、live PostgreSQL、Ubuntu/Linux/Windows 实机及完整 M0/M1 尚未验证。
本地验证：完整备份后升级自有8090至adapter0.8/revision8及schema24；原用户、stopped托管专家、failed/providerUnavailable项目、box身份和原Project收据字节保持。真实库0 Task/thread/session/Turn/Run/event、原派发journal0条、新tool effects0条、usage/model calls0。未知Task/Run404、非法正文/ID422、匿名/URLtoken401、body超限413；原typed OpenAPI和Scalar五阶段卡片可读，无HTTP工具/claim/dispatch/recover/cancel或Task创建/执行入口。同home第二CLI显式host/port/SSL返回CONTROL_PLANE_BUSY、config字节与锁inode保持，首8090进程及原8088健康200。备份：/Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-08-before-adapter-08-20261013T021821Z。
本机原型不注入 ready/Task/Turn/Run/effect Fixture，不配置真实模型或工具；备份、控制身份、数据库、凭据和运行文件仅在忽略目录。

生产 catalog/授权、实际文件 before/after、正式成果发布、真实工具隔离、原生历史/恢复、模型/子进程停止证明、取消/审批、事件/快照、备份恢复 fencing、设备配对、live PostgreSQL 和盒子实机仍待验收。
完整 M0/M1 未通过；下一步继续 M1-08 的持久取消/审批与 M1-09 的事件/快照，按真实依赖逐项放行。
