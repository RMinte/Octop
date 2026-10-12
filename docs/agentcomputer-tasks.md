# AgentComputer Task 与线程映射：第四轮

本轮从 feature/agentcomputer-foundation @ ab454c54 开发，复用已交付的 schema 22。内部 Task 登记与只读查询已实现，真实项目 ready、Task 创建 HTTP、Turn 提交和引擎派发继续等待执行门禁。AgentComputer 客户端本轮仅同步规划，Swift/UI 未改。

## 1. 身份和内部命令

内部 register_task 接收已认证 owner 和严格 TaskCreateCommand：commandID、idempotencyKey、expectedRestoreEpoch、expectedProjectRevision、projectID、taskID、title。Task ID 使用调用方稳定 UUID；title 最多 256 字符，拒绝全空白和 Unicode 控制字符，合法文本保持原样。key 最多 256 字符，版本/代次/修订必须是严格整数。

当前不接收 clientSessionID、内部 thread ID、owner、agent ID、session key、宿主路径或其他额外字段。公开 sessionID 由服务器生成 UUID 字符串，不能拼成本地文件路径。旧本地/导入会话不会因此自动转换成盒子 Task。

内部 session key 固定为 agentID:agentcomputer:taskUUID:dm。它与 Dashboard/CLI 当前选中线程分开，一个项目可以拥有多个不同 Task，各 Task 的 session/thread 全局唯一。内部 thread ID、agent ID 和 session key不通过 Task 查询返回。

## 2. 同连接原子登记

thread、session、历史投影和控制 Task 都属于同一个数据库。本轮不调用模型、harness 或外部线程服务，因此可以直接原子提交：

1. 先检查当前 expectedRestoreEpoch，再锁 owner，核对全局 commandID、owner/key 和规范化 v1 请求摘要。
2. 同 key/同摘要重放原收据；更换 commandID 不产生查询别名。不同正文或命令身份重绑定返回冲突，旧代次在重放前拒绝。
3. 新命令验证本人项目 ready 和 expectedProjectRevision，以及固定准备意图、摘要、托管专家 owner/项目、配置、verified 步骤、无失败原因和 disabled/stopped 状态。当前只有隔离测试库显式准备这种 ready Fixture；真实实验实例仍没有此放行路径。
4. 预留 Task pending、公开 session UUID 和标题，thread 暂为 NULL。ON CONFLICT DO NOTHING 不覆盖任何现有 Task/session/thread；冲突回滚本次事务。
5. 通过 ThreadRegistry 专用方法严格 INSERT 内部 thread、空历史投影和 session。所有 repo 使用调用方 conn，不使用普通 upsert/get_or_create，也不在持 owner 锁时另借 PG 连接。thread.last_active=0 表示尚无回合。
6. 仅 owned/pending/NULL 的 Task 可以确认线程并变为 ready；revision 和 next_sequence 保持 1。没有中间 pending 状态对外提交。
7. 核对实际 project/owner/agent/thread/session 绑定后，保存 TaskCreateReceipt，再一次提交全部记录。

Task/命令时间使用毫秒，现有 thread/session/历史投影使用秒；登记明确转换，不混用单位。任意写入或确认失败均回滚 Task、thread、session、投影和收据。此处没有“外部资源已经成功但 SQL 未确认”的窗口，也不需要新增 023 或第二份准备日志。

将来若线程准备包含数据库外的副作用，需要先保存稳定意图、再核实和分步确认；不能沿用本轮纯 SQL 的原子性结论。孤立线程清理仍要检查归属、历史及其他绑定。

## 3. 原始收据与当前视图

TaskCreateReceipt 使用独立 operation=task.create，保存 box/command/project/task/session UUID、原 acceptedAt/restoreEpoch、初始 taskRevision=1、preparationStateAtAcceptance=ready 和 executionReady=false。ready 只说明数据库映射已经完整提交；它不声明模型已启动、Run 已登记或真实任务可执行。收据不包含虚构 Run 或 Task 事件序号。

原 Turn CommandReceipt 与 ProjectCreateReceipt 的持久 JSON 格式保持。命令查询依据 operation 分派三个解码器，要求全部持久字段显式存在；缺字段不能由 Pydantic 默认值补造。Task 收据核对当前固定资源身份，重放不改原字节；后续 Task revision/状态变化不改原接收事实，历史代次仍可查询。

新只读接口：

```http
GET /api/agentcomputer/v1/tasks/{taskID}
Authorization: Bearer <Octop 用户登录令牌>
```

TaskView 返回 taskID、projectID、sessionID、完整 title、taskRevision、preparationState、executionReady=false。当前状态与原收据分开；未知和跨 owner 统一 404，管理员普通查询也不绕过归属。无效 UUID 返回 scoped 422，损坏绑定返回安全 503/taskInvalid，不回显内部路径或凭据。

读取在同连接一致事务中核对 project/agent/thread 归属、专用 channel/key，以及 session 原始 subject/chat/metadata。原始字段为 NULL 或 JSON 损坏时拒绝，不通过旧 SessionRow 的兼容回填补造 Task 绑定。

本轮没有 Task 创建 POST、列表、改名/pin/归档/删除、Turn 写入、快照或模型执行接口。真实 8090 原型保留零 Task/thread/Run；200 Task 与收据使用隔离 HTTP Fixture 验证，不修改真实失败项目以伪装 ready。

## 4. 旧入口隔离

停止专家和托管标记仍按第三轮保护。第四轮还保护专用 session/thread：ThreadRegistry 的旧 routing、reset、rebind、标题/配置、删除及未读状态变更核对保留 key/channel 和 ac_tasks 实际关联。源 session 和目标 thread 均受保护，普通专家不能通过传任意 session key 或线程提示重绑 Task。

CLI 在准备线程前核对托管专家和路由；GlobalProcessor 在 slash、HITL、历史及其他会话副作用前核对专家和源/目标绑定。普通专家、Dashboard、CLI 和 IM 保留原行为。专用 Task 登记不通过 legacy 方法，也没有可从外部传入的 skip guard 开关。

恢复中的 owner remap、生产迁移、备份 fencing、客户端 Runtime 和托管引擎执行入口仍属于后续阶段，不能据这些局部门禁声明已完成 Task 迁移。

## 5. 能力和验证

adapterVersion=0.4.0、capabilityRevision=4。tasks 与 projects 为 supported=true/available=false、reasonCode=executionUnavailable；commandReceipts 查询可用。权限模式为空，deviceID=null；控制命令上限保持 65536 字节，其余执行容量未测。

确定性验证覆盖各写入失败点回滚、同键重放及异体/资源身份冲突、原收据缺字段和错配、原始 session 损坏、两连接竞争、重新建池读取、跨 owner、独立 Dashboard 绑定和普通入口跨绑定攻击。内部映射还可供先前 newMessage Fixture 登记 queued/notDispatched Run，证明衔接而不调用模型；该内核没有生产 ingress。

工程门禁、本地更新和 fork 交付状态见 [集成入口](agentcomputer-integration.md#第四轮交付状态)。完整 M1-04 的生产 ready/创建与列表流程、M1-05 入站和 M1-06 引擎派发仍待后续实施；PG 只有 SQL/约束静态核对，实机与模型门禁继续按 M0 安排。
