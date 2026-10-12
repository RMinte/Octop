# AgentComputer 数据层与命令收据：第二轮

本轮以 `feature/agentcomputer-foundation @ a6015dd` 为基线，建立 schema 21 的六张控制表、同事务接收内核和本人收据查询。当前只能通过确定性 Fixture 调用接收内核；HTTP、IM、Cron 和 CLI 没有连接到该内核。查询能力不表示模型、工具、项目准备或任务执行已可用。

总体计划与本地启动方式见 [集成入口](agentcomputer-integration.md)。这一轮对应 M1-01 的命令契约、M1-02 的数据层和 M1-05 的部分事务规则；项目准备、线程创建、引擎派发、审批、取消、事件订阅和恢复仍需后续步骤完成。

## 1. 持久事实与约束

公共资源 ID 是 UUID 字符串，表使用整数 surrogate PK；owner 引用 Octop 现有整数用户 ID。SQL repo 只负责 SQL 与行映射，服务装配由 `RepoBundle / SharedServices` 提供。

| 表 | 本轮事实 | 约束 |
|---|---|---|
| ac_projects | 盒子、工作区和默认专家绑定、准备状态、配置及摘要、配置 revision、writer Run | workspace/default agent 唯一；writer 属于同项目/owner |
| ac_tasks | 项目/owner、session、Octop thread、准备状态、revision、下一个事件序号、active Run | thread/session 唯一；project owner 匹配；active Run 属于本 Task |
| ac_turns | 新用户消息、首个助手消息、固定配置与请求快照 | 同 Task/userMessage 只登记一次；不可重复 Turn 身份 |
| ac_runs | Turn、Task、项目/owner、状态、派发阶段、恢复与执行代次、对账标记 | 同 Task、同项目最多一个活动或待对账 Run |
| ac_commands | 命令 ID、用户作用域幂等键、目标、摘要、原始接收结果 | commandID 全局唯一；`UNIQUE(owner_id, idempotency_key)` |
| ac_events | event ID、Task 连续序号、Run/Turn、事件类型及固定载荷 | eventID 唯一；`UNIQUE(task_id, sequence)`；Run/Turn/owner 同属该 Task |

所有父子归属使用公共 ID 和 owner 的复合外键。Task/project 的 activeRun 外键延迟到事务提交检查，允许同一事务登记资源并绑定许可。数据库部分唯一索引同时覆盖 `queued / running / waitingApproval / waitingInput` 和 `reconciliation_required=1`；`interrupted` 但仍需对账的 Run 保留许可。

一个项目对应唯一 workspace，因此项目上的单 writer 约束覆盖同项目不同 Task。未来若增加共享 workspace，必须重新评审许可作用域。

`restore_epoch` 是接收时盒子恢复代次；`execution_epoch=0` 表示尚未领取。两者用途不同，未来领取流程负责递增执行代次。当前派发阶段固定为 `notDispatched`，没有后台领取者。

## 2. 迁移与故障边界

实验库已经应用 schema 20，故新增 `021_agentcomputer_core.sql` 与同号 `.pg.sql`，不修改已应用的 020。SQLite 021 在显式事务中建表、建索引、更新 watermark；失败时回滚。PostgreSQL 使用现有迁移器的外层事务，迁移文件内不额外 BEGIN。

空库、v20 升级、重复执行和已有用户保留均有 Fixture。旧库报告高 watermark 或已报告 21 却缺少六张物理表时，修复路径执行同一份幂等 DDL，不只修改版本号。已完整应用的库不在每次启动重跑 021。

该修复只用于现有迁移器支持的 schema 协调，不具备业务备份恢复、执行进程隔离或备份后命令对账能力。schema 21 不应运行在旧代码上；实验回滚需停止服务、恢复升级前完整独立数据目录，并检出对应代码。不要用降低 watermark 代替回滚。

SQLite 行为已经通过确定性测试；PostgreSQL 的配对 DDL、约束与占位符经过静态核对，当前没有专用 PostgreSQL 测试库，尚未完成真实 PG 执行验收。

## 3. 请求摘要与幂等身份

本轮仅定义内部 `newMessage` 请求；继续、附件输入和技能选择尚未接入，不能把无效资源引用登记为已接受输入。

规范化 v1 使用 UTF-8、对象键排序、无额外空格、中文不转义，随后计算带 `sha256:` 前缀的 SHA-256。类型化请求默认的空附件/技能列表等价于显式空列表；null 不等价于空列表。字符串不裁剪、不归一化 Unicode、不改变换行；数组不排序；非有限浮点数拒绝。

摘要包括协议/规范化版本、boxID、操作、Task 目标、业务正文、消息身份、权限模式、项目版本前提和 expectedRestoreEpoch。commandID 与 idempotencyKey 是传输去重身份，排除在语义摘要之外。测试保存固定规范化字节及已计算的黄金摘要，预期值不由被测函数生成。

| 请求情况 | 结果 |
|---|---|
| 同 owner/key、同摘要 | 返回首个 commandID 和原收据，replayed=true，不新增事件 |
| 同 key 改正文、目标、权限或版本前提 | idempotencyConflict；保留原事实 |
| 同 commandID 换 key | idempotencyConflict；ID 不重绑定 |
| 换 commandID 但同 key/同正文 | 返回首个 ID；新 ID 不建立查询别名，客户端保存响应中的原 ID |
| 两 owner 复用同 key | 各自独立；不能读取他人收据 |
| 新 key 复用同 Task 的 userMessageID | 拒绝，避免第二个可见用户输入 |
| expectedRestoreEpoch 过期 | 在重放前拒绝；不自动改 epoch 或换 key |

当前仅校验独立身份文件中的 epoch，没有提供恢复命令、代次递增、存活 worker fencing 或恢复前后事实协调。`recovery` 保持不可用。

## 4. 同事务接收顺序

内部接收函数通过 `SharedServices` 访问 SQL repo，每个变更只持有一次 `db.transaction()`，repo 接收该连接，不另开写事务。

1. 读取控制身份并校验 expectedRestoreEpoch。
2. 计算规范语义摘要，进入数据库事务；PG 锁定认证 owner，SQLite 使用既有 BEGIN IMMEDIATE 写入预留。
3. 查 owner/key 和 commandID，执行重放/冲突判断。查询不到本人资源不扩大到他人身份。
4. 检查 ready Task/project、配置 revision、当前执行许可及本轮支持的输入形式。
5. 校验已保存配置的 schema 和摘要，固定项目配置、内部绑定和完整请求快照。
6. 登记 Turn 与 queued/notDispatched Run；取得同 Task 许可和项目 writer 许可。
7. 推进 Task revision/nextSequence，追加 `run.accepted` 事件及配置/输入快照摘要，保存原始 command receipt。
8. 提交所有记录及延迟外键后返回。任一失败使收据、Turn、Run、许可和序号同时回滚。

不同 owner 偶然使用同一全局 commandID 时，`ON CONFLICT DO NOTHING` 配合整事务回滚处理竞争；不在 PostgreSQL 已 aborted 的事务中捕获唯一错误后继续查询。

accepted 只证明耐久登记，不能显示“模型已运行”。没有引擎调用、工具效果、输出消息或交付文件。Fixture 的 ready 记录是测试输入，不是真实工作区、专家和执行隔离已通过准备流程的证据。将内核接入任何入口前，必须完成 M1-03/04 的资源核实以及 M1-06 的托管入口、固定身份和派发崩溃窗口。

## 5. 收据查询 API

```http
GET /api/agentcomputer/v1/commands/{commandID}
Authorization: Bearer <Octop 用户登录令牌>
```

200 返回类型化 `CommandReceipt`。acceptedAt、restoreEpoch、资源 ID、receiptRevision、taskRevision 和 acceptedSequence 保留原始接收值；runState 是接收时的 queued，不是当前 Run 状态。查询响应 replayed=false，不执行重放。此接口也不创建缺失记录。

401/403 沿用新协议的严格 header 认证；URL access_token 拒绝。不存在和其他 owner 的 ID 都返回同样的 404，管理员普通查询没有跨 owner 绕过。非法 UUID 返回不回显输入的 422。损坏身份/收据和暂时数据库读失败返回安全 503；不泄露正文、SQL、凭据或宿主路径。响应 `Cache-Control: no-store`。

能力响应为 adapter 0.2.0、capabilityRevision 2，`commandReceipts` 仅表示已有收据查询 supported/available。其余 17 项业务能力仍不可用，权限模式仍为空，未测限额仍为 null。客户端不能根据收据查询能力打开发送按钮。

## 6. 验证与下一步

验证覆盖黄金摘要、重试身份、跨用户查询、两个 SQLite 连接竞争、事务故障回滚、project writer、待对账 Run、连续事件、关闭重开数据库、实际 HTTP 服务重启、损坏收据、数据库故障及 OpenAPI。

工程门禁及实验库升级结果记录在 [集成交付状态](agentcomputer-integration.md#第二轮交付状态)。跳过的 Linux/Windows/PG 实测不计为通过。

下一步为 M1-03：保存项目准备计划，通过现有 AgentManager/BackendWorkspace 准备资源，逐步核实归属并可在重启后继续。项目和 Task 的 ready 转换不能由客户端自由填写，也不能先接通模型发送再补派发和工作区许可。
