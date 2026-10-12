# 第三轮：项目准备和默认专家登记

本轮从 `feature/agentcomputer-foundation @ d77b37f` 开发，实现项目意图接收、停止的默认专家、受限本地工作区、准备日志和当前状态查询。创建项目可以准备资源，但当前不会产生 Task/Run 或启动模型；完整 M0/M1 门禁仍未通过。

关联：[总体计划](agentcomputer-integration.md)、[第二轮事务内核](agentcomputer-core.md)。AgentComputer 的 SPEC-029 / M1-03 保存详细阶段规划。

## 1. 请求和原始收据

```http
POST /api/agentcomputer/v1/projects
Authorization: Bearer <Octop 用户登录令牌>
Content-Type: application/json
```

```json
{
  "commandID": "11111111-1111-4111-8111-111111111111",
  "idempotencyKey": "example-project-create-1",
  "expectedRestoreEpoch": 1,
  "projectID": "22222222-2222-4222-8222-222222222222",
  "name": "本地项目",
  "configuration": {
    "schemaVersion": 1,
    "instructions": "使用中文回复",
    "modelRef": "local/qwen"
  }
}
```

以上是合成请求；modelRef 不是已配置 Provider 或真实 Qwen 服务的证明。客户端不能提交 owner、agentID、backend、绝对路径或任意资源配置。名称最多 128 字符，拒绝空白和 Unicode 控制字符；instructions 最多 8192 字符，modelRef 最多 256 字符，幂等键最多 256 字符。合法字符串原样进入规范摘要，不裁剪后重用键。

实际流入的变更命令最多 **65536 字节**，在 JSON 解析前累计字节，超过上限返回 413/payloadLimitExceeded；不只信任 Content-Length。该数值是已实施并验证的控制命令策略，不表示模型吞吐、并发或 GPU 容量。其他限额保持 unknown/null。

同一数据库事务保存项目、稳定 workspaceID、预留 agentID、准备意图及 ProjectCreateReceipt。201 的 accepted=true 只证明此事务耐久提交。原收据始终保留 `preparationStateAtAcceptance=pending`、原 commandID/时间/恢复代次/资源身份；不会随着准备成功或失败改写。

POST 在当前请求内等待有限本地准备，不创建未跟踪的后台 bootstrap。准备结果通过以下接口查询：

```http
GET /api/agentcomputer/v1/projects/{projectID}
GET /api/agentcomputer/v1/commands/{commandID}
```

前者返回当前状态、步骤、独立 preparationRevision 和稳定原因；后者返回原始收据。名称、资源 UUID、摘要和状态对外可见；宿主路径、内部配置和凭据不返回。本轮尚未开放项目列表、配置修改、单独重新准备命令或删除。

## 2. 同命令重试与准备顺序

1. 认证与严格 schema 验证后，检查当前 expectedRestoreEpoch；过期请求在幂等重放前拒绝。
2. 锁定 owner，核对全局 commandID 和 owner/key；同 key 同摘要返回原 ID，异体冲突。不同 key 不能重新创建同 projectID。
3. 先提交固定意图。intent 包含 box/project/workspace/owner、预留专家、服务器 root 和配置摘要；原始 intent 不随步骤变化，准备日志不占 Task sequence。
4. 取得 OS 项目锁。其他进程不能靠 TTL/心跳过期抢走活跃锁；进程退出后锁自然释放。已被其他请求准备的项目返回原收据，当前步骤通过 GET 观察；中断后显式重发同命令核实并续做。
5. 发布受保护的 workspace ownership claim，再准备服务器分配的目录。已有目录缺归属证明、symlink、路径变化或 claim 损坏时拒绝，不删除、不覆盖、不回退到宿主根。
6. 固定 ID 专家不存在时通过 `create_stopped` 登记。INSERT 同时写入 disabled/stopped 与内部 `managed_by / managed_project_id`，不经过普通 create 的启动、模板、后台 bootstrap 或 proactive 调度。
7. 专家已存在或创建确认丢失时，核对持久标记、owner/project、model/instructions、backend、root、workspace、技能/MCP 空集合和停止状态后复用；相同名字不能作为归属证据。
8. 通过 BackendWorkspace 写入和核对项目 marker，确认 agent/workspace 步骤。内容 I/O 均经此接口，控制 claim 和 OS 锁位于专家内容之外。
9. 只检查本地 Provider 配置是否可用，不访问模型网络。缺 Provider 时项目 failed/providerUnavailable、retryable=true；配置可用时仍 failed/executionValidationPending，不写 ready。执行隔离和托管引擎入站需后续验收。

资源作用在数据库事务之外，每步完成后另一次事务确认。确认丢失先核实固定资源，不盲建第二个专家。失败保留已归属资源和日志；补偿删除需后续流程明确授权与归属，不在本轮自动清理。

客户端重复取消当前 HTTP 等待不会把仍在完成的资源操作提前解锁。请求保持对准备协程的等待责任；最终可以结束响应等待，原收据及已确认事实仍可查询。断开客户端不构成模型 Run 取消命令。

## 3. 托管资源隔离

停止状态不足以隔离旧入口。本轮用内部数据库列识别托管专家，普通 API 不能通过改 config 移除标记；enabled 或 last_state 变化也不能绕过启动检查。

普通 AgentManager/API/CLI 的启动、执行、配置变更、删除和 workspace 入口拒绝托管专家。Cron/Channel 不接受托管绑定，proactive 启动跳过；离线 skills 操作不能改变其固定配置，Provider 清理保留历史 modelRef。准备流程使用专用、严格核对的 workspace 入口。

普通专家继续按原规则工作。用户专家配额包含 disabled 的托管 metadata；项目准备不能通过不启动专家绕过计数。此保护只建立托管入口边界，不代替真实 Shell 沙箱、TLS、设备配对或恢复 fencing。

## 4. 迁移和收据兼容

实验库已应用 021，因此本轮新增成对 **022** SQL/PG，增加项目名称、内部托管列和 ac_project_preparations。journal 有整数 PK、独立 preparationID、唯一 project/planned agent、复合 owner FK、固定意图/摘要、restoreEpoch、步骤及 revision。

SQLite 正常升级、部分列/缺表和高 watermark 修复在 022 事务中执行；失败回滚，旧用户、六表、配置和收据字节保留。PG 维护相同 DDL 和约束，当前只有静态核对，没有专用 PG 实测。

命令查询依据数据库 operation 选择原 Turn 或新 Project 解码器。没有向旧 CommandReceipt 添加必填 discriminator，也不重写旧 JSON。两类收据都要求完整字段及原身份绑定；Project 收据的 workspace/preparationID 必须与持久登记吻合。接收事务重放复用已有连接，避免 PG 持锁时再借连接导致池耗尽。

本地升级前停止并核对自有 8090 进程，完整备份控制身份、配置、数据库、claims 与工作区；旧 8088 不受影响。回滚需停止服务、恢复完整备份和对应代码，不能仅降低 watermark。

## 5. 能力与验证

adapterVersion=0.3.0、capabilityRevision=3。commandReceipts 查询可用；projects supported=true/available=false，表示部分准备接口存在，完整执行准备门禁未通过。Task/Run、工具、审批、成果、恢复及设备绑定仍不可用，权限模式为空。客户端不能据此开放任务执行。

确定性验证覆盖注册事务回滚、资源确认丢失、固定 ID 重试、双连接/双进程互斥、进程退出释放、重复取消持锁、损坏 JSON/UTF-8/关联收据、跨 owner、路径回退、配额、旧专家回归及 HTTP 实际流入限制。测试使用临时目录和合成 Provider 配置，无真实模型请求。

最终工程门禁与本地部署结果见 [集成交付状态](agentcomputer-integration.md#第三轮交付状态)。完整 M1-03 的 ready 放行、M1-04 Task/thread、M1-06 派发和真实 M0 仍待完成。
