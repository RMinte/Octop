# AgentComputer Turn 登记与 Run 查询：第五轮

第五轮以 `feature/agentcomputer-foundation @ bdff37f` 为开工基线，复用 schema 22，无新增 023。新增受执行能力门禁保护的 Turn POST、本人只读 Run GET，并加强原 Turn 接收收据与完整持久图的核验。AgentComputer Swift/UI 本轮未改。

当前协议为 protocolVersion=1、adapterVersion=0.5.0、capabilityRevision=5。projects、tasks、runs 均为 supported=true/available=false，reasonCode=executionUnavailable；supportedPermissionModes=[]。commandReceipts 仅表示已有收据查询可用。模型派发、工具、审批、问答、取消、公开事件流与恢复 fencing 没有因此开放。

## 1. 入口和当前门禁

所有新接口使用 `/api/agentcomputer/v1`，要求 `Authorization: Bearer <Octop 用户登录令牌>`。用户登录身份仍不是可信设备配对，deviceID=null。URL access_token 不被接受，普通管理员查询也不能绕过 owner。

| 接口 | 本轮行为 |
|---|---|
| `POST /tasks/{taskID}/turns` | 只接受现有本人 Task 的 newMessage；新登记受真实执行能力门禁保护 |
| `GET /runs/{runID}` | 查询本人 Run 的当前已持久状态，不派发或修复 |
| `GET /commands/{commandID}` | 查询原 immutable 收据，兼容已有 Turn、Project、Task 收据格式 |
| `POST /projects/{projectID}/tasks` | 尚未实现；本轮不会自动创建 Task |

生产能力函数没有把 runs 标记 available。合法新请求指向有效本人 Task 时返回 503/runtimeUnavailable，reasonCode=executionUnavailable；整个登记事务回滚，不增加 Turn、Run、接收事件或命令，也不取得 Task/project Run 许可。未知和跨 owner Task 统一 404。

隔离测试可以对服务内可信能力函数打补丁，使用真正完成资源准备与映射的托管 Task，验证登记返回 201。该补丁不属于产品配置；请求体、环境变量和用户设置均不能启用它。此测试成功仍只有 queued/notDispatched，没有模型、harness、工具或 engine handoff。

真实 8090 原型保留原 providerUnavailable 项目和零 Task/Turn/Run。占位 Task UUID 不会自动变成有效 Task；不能通过直接修改真实项目 ready 状态来取得接口成功证据。

## 2. 严格 newMessage 请求

示例中的 Task 与用户消息身份只用于说明格式；有效 Task 必须已存在。

```http
POST /api/agentcomputer/v1/tasks/22222222-2222-4222-8222-222222222222/turns
Authorization: Bearer <Octop 用户登录令牌>
Content-Type: application/json
```

```json
{
  "commandID": "c1111111-1111-4111-8111-111111111111",
  "idempotencyKey": "turn-fixture-01",
  "expectedRestoreEpoch": 1,
  "contextMode": "newMessage",
  "userMessageID": "55555555-5555-4555-8555-555555555555",
  "expectedProjectRevision": 1,
  "prompt": "合成请求，仅用于格式说明",
  "inputArtifactIDs": [],
  "selectedSkillIDs": [],
  "permissionMode": "askForApproval"
}
```

commandID/userMessageID 为 UUID；key 最多 256 字符。epoch 和 project revision 必须是正的严格整数，不接受布尔、数字字符串或浮点替代。prompt 保存精确用户文字，不裁剪或变成线程标题。

inputArtifactIDs 与 selectedSkillIDs 省略时使用类型化空数组；非空集合尚不支持。内核首版只接受 askForApproval，不暗中把 approveForMe/fullAccess 改成另一策略。在可信能力测试中，能力声明以外的有效权限模式返回 422/capabilityUnsupported；资源集合仍由登记内核明确拒绝。真实能力关闭时，新有效命令首先返回 executionUnavailable。

只支持 newMessage；继续原消息属于后续契约。本轮不接收 owner、agentID、threadID、session key、客户端旧会话引用、host path、执行配置或任意额外字段。请求不能借用 Dashboard/CLI 当前线程，也不能指定执行环境或在本机与盒子之间切换。

`MAX_COMMAND_BYTES=65536` 是实际 enforced 控制正文上限，按已接收 UTF-8 字节计数，不能靠不准确的 Content-Length 绕过。超过上限返回 413/payloadLimitExceeded。这个限额不代表模型上下文、上传容量、队列或并发能力已经测量。

## 3. 同连接登记和原接收收据

传输服务与原内部 Fixture 共享一个登记内核。HTTP 路由只做认证、类型校验、序列化和安全错误映射；没有另一套模型执行循环。

1. 检查安装 box/restore epoch 与能力画像一致；旧 expectedRestoreEpoch 在重放之前拒绝。
2. 在一个数据库事务内锁定已认证 owner，使用同一 conn 核对 Task、固定项目/专家意图、thread 与 session 原始绑定。
3. 核对全局 commandID、owner/key 和规范化 v1 请求摘要；存在原命令时先验证并返回原事实，不重新派发。
4. 新命令必须通过当前 runs 可用性和权限声明；随后检查项目/Task ready、expectedProjectRevision、重复用户消息和活动 Run/需对账许可。
5. 在同一事务内保存固定请求/配置快照、Turn、queued Run、Task/project Run 许可、Task sequence、run.accepted 和原命令收据。
6. 全部写入和 deferred FK 提交后才返回 201；任意失败或竞争冲突均整事务回滚。

repo 不调用域服务或模型，所有事务内读取使用调用方 conn；PG 持 owner 锁时不再借池连接。数据库的项目 writer 许可只约束已登记 Run，不等于已经 claim worker。

CommandReceipt 沿用第二轮原始格式，包含稳定 box/command/Task/Turn/Run/用户及助手消息 UUID、原 acceptedAt、restoreEpoch、taskRevision、acceptedSequence、accepted=true、replayed 和原 runState=queued。本轮不会给旧持久 JSON 添加新的必填 operation 或改写它的字段；新的 RunView 是独立当前投影。

201/accepted=true 只证明登记耐久。初始 Run 为 queued/notDispatched、executionEpoch=0；它没有证明模型已开始、工具已执行、审批已发生或结果已交付。

## 4. 重放、快照与完整接收图

同 owner/key/同摘要重试，包括更换 commandID 的响应丢失重试，仍返回 201 的原收据，响应 replayed=true。落盘 receipt 仍为 replayed=false，原 commandID、acceptedAt、对象 ID 和字节保持；新 commandID 不生成查询别名。不同正文、前提或身份重绑定返回 409/idempotencyConflict。

能力关闭后，已经接受的命令可以重放其原事实；这不是新登记许可。重放仍先校验当前 epoch、本人的托管映射、commandID/key 冲突和原接收图。项目当前状态或 Run 已经变成终态，不会把原 queued 改写为另一接收结果。恢复后旧 epoch 变更请求拒绝，历史收据与 Run 的原代次仍可读取；初始化 epoch 不代表完整恢复 fencing 已验收。

规范化 v1 为对象键排序、无额外空格、UTF-8、不转义中文的 JSON，摘要带 sha256 前缀；保留字符串内容及数组顺序，拒绝 NaN/Infinity。请求摘要包含 box、Task 目标、operation、协议/摘要版本及全部业务字段和 revision/epoch 前提，排除 commandID/idempotencyKey。

原 Turn 快照必须完整保存：

- schemaVersion、projectID/projectRevision、workspaceID、agentID 和 octopThreadID。
- 完整原 ProjectConfiguration：schemaVersion、instructions、modelRef。
- 完整原请求：epoch/contextMode/userMessageID/project revision/prompt/附件/所选技能/permissionMode，空集合也显式落盘。

原配置/请求使用严格持久 DTO，缺字段不使用 Pydantic 默认值或当前项目配置补齐。重新计算原 snapshot digest、command payload digest 和配置 digest，全部与落盘事实对应；当前项目配置或 revision 变化不能替换原输入。

命令查询、重放及 Run 查询核对 owner/box、Run→Turn→Task→Project、用户/助手消息 ID、原时间和 epoch、receipt sequence、唯一 run.accepted 及其完整 payload。接收 payload 包含原 receipt 事实以及 configDigest/inputSnapshotDigest。一个 Run 的多个 run.accepted 必须被识别为损坏，不通过 LIMIT 1 隐藏。

当前 Task revision/next_sequence 可以已前进，历史终态 Run 也不必继续占当前 activeRun 指针；原收据不要求当前 state 仍为 queued。快照、事件、绑定或原收据缺失/损坏时明确失败，不重新生成、补造或自动重放。

## 5. Run 当前视图

```http
GET /api/agentcomputer/v1/runs/44444444-4444-4444-8444-444444444444
Authorization: Bearer <Octop 用户登录令牌>
```

RunView 返回以下当前已持久字段：

| 字段 | 事实来源 |
|---|---|
| runID、turnID、taskID、projectID | 已核对的固定 Run/Turn/Task/Project 身份 |
| state | ac_runs 当前状态 |
| dispatchPhase | 当前 notDispatched/claimed/dispatching/engineAccepted |
| executionEpoch | 当前执行领取代次，初始为 0 |
| restoreEpoch | 此 Run 接收时持久代次，历史读取不改成当前 epoch |
| revision | 此 Run 当前持久 revision |
| reconciliationRequired | 此 Run 当前需对账事实 |
| createdAt、updatedAt | 数据库存储的毫秒时间转换成 UTC，不使用查询时刻补造 |

state 使用已有 schema 的 queued、running、waitingApproval、waitingInput、succeeded、failed、stopped、interrupted；服务端 Run 没有 unknown 状态。查询只返回实际存储的枚举值，不从网络连接状态推断运行结果，也不因存在某个枚举而生成 Approval、Question 或取消事实。

queued/notDispatched 表示已接收且未交给引擎。执行代次和恢复代次独立；reconciliationRequired 为 true 时，新 Run 许可必须等待对账。这个 GET 不 claim worker、发起模型、释放许可、取消 Run、修复存储或提供尚未实现的事件 cursor。

Run 查询不返回原 prompt、模型配置、内部 thread/agent ID、宿主路径或凭据。原命令 GET 始终返回原 queued 收据；客户端要知道当前状态应查询 Run，而不是把收据当 live projection。

## 6. 状态码与作用域

新接口及安全错误响应使用 Cache-Control: no-store。

| HTTP | 情况和协议码 |
|---|---|
| 201 | 可信能力测试下成功登记，或原命令同键同体重放；原 CommandReceipt |
| 401/403 | authenticationRequired、credentialExpired 或 accessDenied；只接受 header Bearer |
| 404 | resourceNotFound；未知和跨 owner Task/Run/Command 相同，管理员也不绕过 |
| 409 | idempotencyConflict、restoreEpochConflict、revisionConflict、activeRunConflict 或 reconciliationRequired |
| 413 | payloadLimitExceeded；实际控制正文超限 |
| 422 | invalidRequest，或受支持类型中未支持的权限/资源 capabilityUnsupported |
| 503 | runtimeUnavailable/executionUnavailable，或损坏/不可读存储的 storageUnavailable |

损坏 Run 绑定为 storageUnavailable/runInvalid，坏提交图为 storageUnavailable/submissionInvalid，坏查询收据使用收据专用原因。错误不回显输入、prompt、异常详情、内部路径或其他用户对象。确定的损坏不自动重试；原请求是否接受不明时先查询原 commandID，不能换 key 盲投。

## 7. 验证和后续步骤

确定性验证范围包含关闭能力零新增、实际托管绑定、授权测试画像下 201、能力再次关闭后原收据重放、请求超限、权限/资源拒绝、完整快照/事件/摘要错配、重复 run.accepted、各写入点回滚、跨 owner、恢复代次、同 conn 和当前 Run 状态读取。历史字段和身份不由默认值补造，旧普通 Octop 入口继续受既有托管/Task 路由保护。

本轮数据层新增 owned Turn、Task sequence 事件和 owned Run 的完整接收列表读取，schema 22 不变。SQLite 使用临时数据库；PG-shaped 验证只检查代理占位符和行映射，没有真实 PostgreSQL 并发或事务实测结论。

工程门禁结果：最终 make all（Ruff、strict mypy、全套非 live 测试）通过，4757 passed / 23 skipped；skipped 不计通过。实际交付必须通过仓库提交 hook/testmon 与 Dashboard 构建，结果保存在本机提交日志。真实模型、live PG 和 Ubuntu/Linux/Windows 实机及完整 M0/M1 尚未验收。

真实本地更新与证据：自有 8090 经 PID/命令/工作目录核对、完整备份后升级 adapter0.5/revision5；schema22、原用户、stopped 托管专家、failed/providerUnavailable 项目、boxID 与原 Project 收据字节保持。真实库仍 0 Task/thread/session/Turn/Run/event；新增有效格式指向未知 Task 的 POST404、未知 Run404、非法正文/ID422、匿名/URL token401、实际超限413。有效本人 Task 的关闭门禁503及可信登记201由隔离 HTTP Fixture 验证，未在原型写入 ready 或 Task。typed OpenAPI 与 Scalar Turn/Run 卡片可读，8088 健康200，无模型请求。升级前完整备份位于 /Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-05-before-adapter-05-20261012T141944Z；运行数据与凭据均在忽略目录。

当前生产原型仍未创建 Task/Run，项目仍 providerUnavailable，没有模型请求。完整 M1-05 的配置/输入能力和 M1-06 的真实托管入站、worker claim、派发与恢复尚未放行；真实 ready、Task HTTP 创建、取消、审批/问答、SSE、正式成果与生产设备配对按后续步骤分别验证。

历史验收与本轮交付记录见 [集成入口](agentcomputer-integration.md#第五轮交付状态)；前置资源和线程规则分别见 [项目准备](agentcomputer-projects.md)、[第四轮 Task 映射](agentcomputer-tasks.md)。
