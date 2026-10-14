# AgentComputer 保守启动恢复与检查点探测

第七轮以 `feature/agentcomputer-foundation @ ca904ae0f629fbbf49c1747b365a1277eb5a18bf` 为基线，补充 daemon 启动前的保守扫描、生命周期互斥与原生检查点元数据边界。
当前 adapterVersion=0.7.0、capabilityRevision=7。完整 recovery 仍为 unsupported/notImplemented；projects/tasks/runs 仍 unavailable，权限模式为空。
本轮复用 schema23，不开放生产 worker、真实模型/工具、HTTP recover/cancel/claim、用户运行按钮或恢复运行开关。
API、原收据和公开 RunView 字段保持；最终门禁及本地结果见 [第七轮交付状态](agentcomputer-integration.md#第七轮交付状态)。
后续已将受托业务 Turn ID 固定到 v2 原生历史，并在启动收紧后增加[只读历史对账](agentcomputer-native-history.md#启动时的只读历史对账)；本文以下仍记录第七轮当时的恢复边界。

## 当前启动边界

daemon 在初始化控制面后、创建运行时之前执行恢复扫描。显式选择的 daemon 生命周期 OS mutex 先保护当前安装目录，持有到服务退出；冲突使用本地化 `CONTROL_PLANE_BUSY`，状态为 409。
CLI 在保存显式 host/port 或生成 TLS 文件之前取得这把锁，并将同一个 guard 交给 foreground/server；被拒绝的第二个进程不改运行配置。直接 foreground 同样创建 daemon guard，默认 embedded Server 不接收 guard。
这个 mutex 只协调参与第七轮约定、使用同一当前 OCTOP_HOME 的 daemon，不是数据库集群锁，也不涵盖另一目录、所有嵌入式调用或未采用此协议的旧版本进程。
它不能证明旧 0.6 进程不存在，不能证明模型、工具、子进程或沙箱已经停止，也不能授予重派权限。

embedded 启动默认跳过这项 daemon 互斥与扫描，不暗中把本地 CLI/测试调用变为全局恢复动作。
锁获取、扫描、组件清理与数据库关闭均等待后台操作结束后才传播取消；首次启动在恢复前或组件创建途中失败、监听规划失败和正常退出都清理已创建资源后放锁。部分组件 boot 的失败清理不依赖 `_started` 已置为 true。
确定性 Fixture 可以显式调用内部恢复函数；这与配置一个可运行模型的产品开关不同。
普通 Octop 专家的入口和原行为不因该元数据扫描变成 AgentComputer worker。

## 扫描范围与顺序

`recover_dispatches(services)` 是可信内部控制面动作，返回 scanned、tightened、unchanged 计数，不提供 HTTP 调用端点。
它先读取已有候选的内部整数高水位，再将分页范围限定为当前 box，用每页 100 条的 keyset 方式遍历，不把内部主键当作对外 cursor。
高水位为 0 时不读写身份文件、不创建业务记录或 journal、不调度执行；有候选时只读取已有身份，缺失不重新生成。

1. 读取候选高水位，有候选才读取已有稳定身份；只处理当前 box 的既有派发事实。
2. 按内部主键分页读取 claimed、dispatching、engineAccepted 等候选，严格解码数值与版本，不按更新时间或 TTL 判断进程是否停止。
3. 每个 Run 复用 owner→Run 锁顺序读取当前原接受图、固定意图、领取主体、双 epoch、journal 与已存在的应用收据；claimed/dispatching 可合法无 ACK，engineAccepted 必须有完整 ACK。
4. 对尚未收敛的 claimed/dispatching/engineAccepted 保守写 interrupted/reconciliationRequired，保留 phase、执行代次、原意图和 Task/project 双 active lane。
5. 晚到应用确认在锁内重读并吸收；已经保存的 succeeded/failed/stopped 不覆盖，已经收紧的原事实不重复生成事件或增加 revision。
6. 原 queued CommandReceipt、消息/Turn/Run 身份、acceptedAt、restoreEpoch、acceptedSequence 与 JSON 字节保持。没有新 Run、重新领取或排队，也不释放原 lane。

扫描以每 Run 事务提交；不能把一次批量遍历宣称为整个数据库的原子恢复。
损坏的原图、journal、版本、数字或关联拒绝，不补默认 receipt、checkpoint 或 notDispatched 事实；扫描异常不能被包装成已经恢复完成。
停用 owner 或旧 restoreEpoch 的既有 Run 可以收紧，不能因此获取新的执行、工具或文件权限。

## 恢复收紧与用户取消

这里的 interrupted 表示控制面不能证明旧执行结果。reconciliationRequired 和 lane 保留使后续命令继续受阻，避免把不明结果当作可重试动作。
claimed 被收紧也不代表模型已开始或已停止；这只是保守封锁未收敛交接。
Python 请求/服务取消和进程重启是控制面上下文变化，不能冒充用户显式取消 Run 的授权。
本轮不写 cancelRequested，不生成用户取消决定，不调用模型 cancel/stop，不标 stopped、不杀进程、不清理用户文件。
只有未来具有真实停止范围、用户命令身份、版本及效果处理的取消协议，才可以确认 stopped 和解除相应许可。

daemon 扫描也不重建历史、恢复模型循环或释放后台工具。
租约、心跳、PID 缺失、无 token、空 checkpoint 或应用 ACK 都不能单独证明没有执行或没有效果。
未知效果继续属于 M1-07 的工具 journal 与对账范围；不能通过恢复扫描补成功、失败或无效果结论。

## 原生检查点元数据

本轮只通过已经存在的 agent 的公开 saver 保存/探测固定关联元数据；不冷打开检查点文件，不自行连接原生数据库，也不创建新的 agent/runtime 来查历史。
探测使用固定 Run/Turn、派发/请求身份、双 epoch 与快照关联核对已经保存的元数据；不把整个 checkpoint、模型正文、内部宿主路径或凭据暴露给公开查询。
不存在、不可用或不匹配不能解释为模型未开始、进程已停止或工具未发生。
即使元数据匹配，也只证明该 saver 保存过这组关联，不能据此自动 resume 或重派。

`EngineEntryReceipt` 是应用接收证据；应用 ACK 和原生 checkpoint 没有跨存储原子提交。
ACK 已保存而 checkpoint 尚未保存、checkpoint 存在但模型结果未知，仍保留不明窗口。
原生 HistoryArchive.begin 的历史 UUID 没有与业务 turnID 建立绑定，本轮不预留假的 historyTurnID，不宣称原生 exactly-once、模型 started/completed 或完整历史恢复。

## 公开协议保持

本人 GET /api/agentcomputer/v1/runs/{runID} 仍只返回 12 字段：runID、turnID、taskID、projectID、state、dispatchPhase、executionEpoch、restoreEpoch、revision、reconciliationRequired、UTC createdAt/updatedAt。
查询展示已经保存的收紧结果；它本身不执行恢复、取消、修复或新派发。
未知和跨 owner 同 404，管理员没有绕过；坏事实安全 503/runInvalid，不回显内部值。原 receipt 查询继续表达原接收事实。
Project/Task executionReady 仍 false。Scalar 保持五个互斥阶段示例，不能根据 supported、phase 或匹配的 checkpoint 启用执行。
capabilities 的 commandReceipts 已有查询可用，projects/tasks/runs supported/unavailable，recovery unsupported/notImplemented；permissionModes=[]、deviceID=null、maxCommandBytes=65536，其余限额 null。

## 验证证据与后续范围

确定性 Fixture 从真实 stopped 资源、原子 Task/Turn 登记及合法派发过程生成 claimed/dispatching/engineAccepted，再扫描并核对公开投影、双 lane、原收据及重复扫描。
验证多页高水位、空扫描、当前 box 隔离、晚到确认、终态胜出、停用 owner/旧 epoch、坏数字/未知 schema 和扫描不调用模型/工具。
启动 Fixture 分别验证 daemon 生命周期互斥、冲突本地化、扫描先于 runtime 与 embedded 默认跳过；原生 saver 仅通过纯内存 Fake model/既有公开接口核对，保持非 live。

工程验证：最终 make all 通过：Ruff、strict mypy（589 个源文件），5038 passed / 23 skipped；跳过不计通过。提交还须通过仓库实际 pre-commit/testmon 与 Dashboard 构建。真实模型、live PostgreSQL、Ubuntu/Linux/Windows 实机及完整 M0/M1 尚未验证。
本地验证：完整备份后升级自有 8090 至 adapter0.7/revision7，schema23不变；原用户、stopped 托管专家、failed/providerUnavailable 项目、box身份和原 Project 收据字节保持。真实库0 Task/thread/session/Turn/Run/event，dispatch journal0条、usage/model calls0。第二个同home CLI显式指定host/port/SSL时返回 CONTROL_PLANE_BUSY，配置字节与锁inode不变，首个8090进程保持；原8088健康200。未知Task/Run404、非法正文/ID422、匿名/URLtoken401、body超限413，typed OpenAPI与Scalar五阶段示例可读，无HTTP claim/dispatch/recover/cancel或Task创建/执行入口。备份在 /Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-07-before-adapter-07-20261012T163009Z。
本机原型不注入 ready、Task/Turn/Run/journal，不配置真实模型；原 failed/providerUnavailable 项目、stopped 专家和原收据保持。控制身份、备份、数据库与凭据仅存忽略目录，不推送。

本轮不证明真实原生 checkpoint 恢复、模型/子进程停止、claimed 安全重领、效果对账、用户取消/审批、公开事件/一致快照、备份 restoreEpoch 防重放、设备配对或 live PostgreSQL/Ubuntu/Linux/Windows 断电恢复。
完整 M0/M1 仍需分别验收；后续在具备原生身份、停止与副作用证据后再设计恢复运行与解除许可。
