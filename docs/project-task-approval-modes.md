# Task 审批模式与远端独立 Reviewer

更新日期：2026 年 10 月 16 日。Octop 服务端和现有网页已实现下述 **v3** 契约。一个 Task 共用三档审批模式，由 Octop 保存；独立 Reviewer 在 Octop 远端运行，执行权限留在各执行端。AgentComputer 仅作只读参考，其本机模式同意、远端 Reviewer 适配和派发检查仍需客户端接线，尚未完成真机端到端验证。

暂停、提问和人工回应的完整手册见[交互协议](project-task-interactions.md)，客户端操作字段和接线顺序见[AgentComputer 交接说明](agentcomputer-task-interactions.md)。

## 模式与执行范围

| 模式 | 普通命令需要审查时的处理 |
| --- | --- |
| `ask_for_approval` 请求批准 | 等待用户允许一次或拒绝。 |
| `approve_for_me` 帮我审批 | 独立 Reviewer 返回 `allow_once`、`deny` 或 `requires_user`；后者转人工。 |
| `full_access` 完全访问 | 在执行端明确授权的范围内省略普通命令确认，仍遵守禁止规则与强制人工要求。 |

首版治理服务端 `execute`，以及供客户端接入的本机 `exec_command`。服务端 `execute` 即使全局 HITL 关闭也进入 Task 审批路径，不靠命令关键词猜测是否安全。`execute` 的完整参数按其原契约送审；Shell 字符串不伪装成无 Shell 的 argv。本机 `exec_command` 的 `arguments.command` 必须是非空字符串数组，客户端继续执行自己的命令校验和禁止规则。

其他工具沿用既有安全配置，不因切模式获得额外自动放行。MCP、发送、发布、支付等不在这次普通命令模式的自动审查适配范围；需要人工审批的动作仍按原规则处理。文件授权也不由任务模式重新定义。提问保持独立，完全访问不会自动回答问题。当前持久恢复仍限主 Task 图。子代理和团队成员的 execute 明确返回未获批准的错误，保留其读取工具；嵌套图不能借用根任务的审批或完全访问决定执行命令。

## 权威与设置

| 层级 | 保存的事实 | 不能替代的权限 |
| --- | --- | --- |
| Octop 管理员设置 | 可用模式、强制人工工具和既有安全限制。 | 用户模式不能覆盖管理员限制。 |
| Octop Task | `approval_policy:{mode,revision,updated_at}`。 | 不代表执行电脑已同意采用增强模式。 |
| 单个动作 | 原参数、执行端、模式版本、动作摘要和决定来源。 | 之后切模式不能改写该动作或替代另一次批准。 |
| 执行电脑 | 目录 bookmark、目录/命令授权身份、本机模式同意及执行 journal。 | 远端 mode、Reviewer 允许或领取成功都不授予本机权限。 |

新 Task 默认 `ask_for_approval`，创建请求顶层可指定 `approval_mode`，不放进 `options`；首条消息的幂等核对包含该模式。后续消息和重试沿用当前任务模式，每轮在 `approval_policy_at_start` 留下入队快照。动作实际使用创建该动作时的 Task 模式，不能只依赖轮次开始值。

网页输入框只有一个“当前任务审批模式”选择器。它显示远端已确认状态，模式请求未确认前保留原值；丢回执后显式重试原请求，或读取当前模式。首次选择服务端完全访问会说明真实范围：`execution_profile=host_process`，命令在 Octop 宿主进程内，以其操作系统身份和实际权限执行，工作目录不是 OS 沙盒。该确认不授权任何电脑。

管理员在现有安全设置的 Task 审批区域配置：

```json
{
  "allowed_modes": ["ask_for_approval", "approve_for_me", "full_access"],
  "require_human_tools": ["custom_sensitive_tool", "client__exec_command"]
}
```

读取和保存分别为 `GET` / `PUT /api/admin/security/task-approvals`，需要 `security` 权限。默认仅开放 `ask_for_approval`，且该模式必须始终保留；`require_human_tools` 默认空，最多 256 个不重复的准确工具名。客户端命令可按原名 `exec_command` 或模型侧名 `client__exec_command` 指定。保存替换该设置，不修改专家的全局策略。

`GET /api/project-tasks/resources` 的 `approval` 为 `{allowed_modes,reviewer_ready,execution_profile:"host_process"}`。该资源接口的 `reviewer_ready` 检查当前默认模型是否可用；实际审查仍使用原轮次模型，并在审查前复验。不就绪不能选择帮我审批。模式被管理员撤销后，尚未派发的旧自动授权不再可用；客户端调用读取时会关闭为不可执行状态，强制人工变化通过 `manual_review_required` 重新报告。

## 切换任务模式

```json
{
  "type": "task.approval.set",
  "id": "approval-mode-command-001",
  "task_id": "task_example",
  "data": {
    "request_id": "approval-mode-intent-001",
    "expected_revision": 3,
    "mode": "approve_for_me"
  }
}
```

REST 等价入口为 `PUT /api/project-tasks/{task_id}/approval-policy`，body 同 data，成功返回当前 `TaskDetail`。`expected_revision` 校验 `approval_policy.revision`，与正文、标题和运行状态的版本分开。模式写入、任务级幂等记录及列表增量在同一 owner 事务锁中保存。空闲或轮次结束后也可以切模式。

同一原请求重发只返回当前状态，不重复应用旧意图；同键异体为 `TASK_REQUEST_CONFLICT`，版本竞争为 `TASK_APPROVAL_POLICY_CONFLICT`，管理员未开放或 Reviewer 未就绪为 `TASK_APPROVAL_MODE_UNAVAILABLE`。跨用户隐藏目标。客户端收到成功回执或更新快照后才更新实际模式，按模式独立版本避免旧正文覆盖新设置。

切换只影响尚未固定的后续动作和未来轮次。已存在的人工卡、正在审查的动作和已创建但尚未领取的客户端调用保持原快照。切换不是撤销已经形成的单次批准；要立即阻止新派发应暂停、停止或撤销资源权限。已启动的操作不回滚、不自动重跑。

## 服务端裁决流程

普通 `execute` 及已有安全规则产生审批后，Octop 固定原调用与动作模式，在原生检查点落盘后释放主任务执行槽。顺序为：

1. 同一轮相同服务端参数与配置已被拒绝时，直接保存 `previous_denial`，不重新发起 Reviewer。
2. 强制人工工具、模式已被管理员撤销、原安全策略不允许批准，以及非命令适配范围的审批保持人工。
3. 请求批准保持 `pending`；帮我审批进入 `reviewing`；完全访问保存单次 `full_access_mode` 允许。
4. Reviewer 明确允许或拒绝后保存 `auto_reviewer`；需要用户或异常时，同一个 interaction 转为 `pending`，不另建卡片。
5. 恢复原断点后，派发前复验原调用、参数、Worker 租约、暂停/取消、当前安全限制与派发标记；每个批准只能用于原动作一次。

服务端自动审查期间公开 Task 可以保持 `running`，界面显示“AI 正在审批”；只有人工 `pending` 形成 `waiting_approval`。自动裁决只留紧凑详情，不永久占据人工卡片。审批 payload 带 `approval_mode`、`approval_revision`、`action_digest` 和 `action_signature`；后者用于同轮动作拒绝去重。普通回应入口只接受用户允许/拒绝，不能自报可信自动来源。

## 远端独立 Reviewer

Reviewer 使用本轮已选模型，以独立请求处理固定提案，不带主 Task 聊天历史、工具、MCP、专家或工作区读取能力。系统指令固定，动作提案作为数据传入，不能把工具文本中的“请批准”当成审查指令。服务端提案含执行端、原工具参数、真实工作目录、执行配置、动作/模式摘要和必要用户目标。

首版输入最多 32 KiB，调用期限 45 秒，接受的文本输出最多 4 KiB；模型输出只能是 `{"decision":"allow_once"}`、`deny` 或 `requires_user` 对应的同结构 JSON。附带工具调用、额外字段、重复键、无效 JSON 或超限输出均转人工。没有为远端 Task 增设第二套模型凭据或 Reviewer 参数面板。

| `reason_code` | 处理含义 |
| --- | --- |
| `review_allow` / `review_deny` | 原动作明确允许一次或拒绝。 |
| `review_requires_user` | 需要用户决定。 |
| `review_timeout` | 45 秒审查超时，转人工。 |
| `review_unavailable` | 模型、用户或审查服务当前不可用，转人工。 |
| `review_context_too_large` / `review_invalid_response` | 提案超限或返回结构无效，转人工。 |
| `review_quota_exceeded` | 用户模型配额不足，转人工。 |
| `review_unknown` | 已派发审查失去租约且结果未知，转人工，不重发模型请求。 |
| `review_expired` | 达到原动作期限，不能继续自动授权。 |

独立 Reviewer Worker 使用持久 SQL 队列和单独异步执行槽，不占主 Task 的执行槽，也不增加 Redis 或外部服务。按原执行端及 interaction/call 唯一保存 review、租约和结果；原动作只进行一次自动审查。重启后可调度尚未发出的队列项，已发出但结果未知的审查不会再发，恢复为人工。取消、过期和租约变化阻止迟到允许；模型用量独立记录在 review 行并计入用户累计配额，不重复计入主轮次。已取消请求的迟到用量可结算，但不能复活授权。

十分钟动作期限从原创建时计算，不因审查、转人工、重连或手动暂停重置。Reviewer 可在暂停期间保存结果；主任务保持暂停，原工具不因此启动。

## 执行电脑接入

Octop 已提供 `tool.review`，结果是审查建议，不是本机执行授权。客户端先完成 `client.attach` 和原调用 `tool.claim`，保留 claim_token，再使用原 call 的 `approval_context.action_digest` 与固定的本机执行上下文请求审查。接口只支持可自动审查的 `exec_command`，不能审查任意服务端目标或覆盖强制人工工具。

本机上下文包含真实 `working_directory`、`workspace_authorization_digest`、`command_authorization_id`、`execution_profile` 和完整 `context_digest`。模式、参数和服务端动作摘要从服务端原调用取值；请求不能替换原参数。首个提案在原 call 上持久固定，后续相同提案返回相同 review，不重复花费模型用量；更换上下文或摘要返回冲突，不能靠新 request_id 重审。

同一轮、同一 client_id、工具名和完整 JSON 参数已有 Reviewer 拒绝时，即使换 call_id、改目录或切到 full，也不会创建新动作或审查，服务端以 `TASK_CLIENT_TOOL_CLOSED` 保守终止本轮；不同 argv 的新提案可以重新审查。本机用户拒绝事实仍由客户端 journal 保存和约束，不能把远端已知的 Reviewer 拒绝范围误当成本机全部拒绝记录。

`tool.review` 快速返回含 `review` 的 `ClientToolCall`，结果通过 `tool.call` 更新及 `tool.get` 恢复，共享 Socket 接收循环不等待 45 秒模型结果。客户端必须校对 review_id、原 call、action_digest、context_digest、模式版本和本机授权身份；不匹配时不执行。

派发前在线 `tool.get` 重新检查：

- `dispatch_allowed=true` 仅说明原调用与原轮次当前允许进入派发检查，不等于授权。
- `manual_review_required=true` 必须由本机用户明确批准，旧 Reviewer 允许或 full 模式都不能覆盖。
- 同时检查 call 仍为本人领取且未过期、Task/turn 未暂停或取消，以及所有本机硬性规则、目录和命令授权。

客户端还需实现本机增强模式同意。它应绑定实例、账号、client_id、Task、模式版本和目录/命令授权身份，存入现有加密 journal；其他设备改为自动或完全访问不能创建该同意。原本地 Runtime 的 `AgentPermissionModeStore` 继续保持设备级全局偏好，不直接成为远端 Task 权威。

只有本机同意与资源权限仍有效，才可把匹配的自动建议记为本机 `auto_reviewer` 单次授权；完全访问同样需要本机明确范围确认。`requires_user` 走现有本机 Dock，不新建服务端人工卡。决定来源不能伪装为用户批准，执行开始与实际结果先落盘再按原调用回传。断网、状态未知、暂停或撤权时禁止新派发；恢复沿用原调用，不重新创建执行。

审查会把原 argv 和必要目录、授权身份摘要等执行元数据发送给 Octop 及审查模型。服务端不读取或上传本机文件内容；客户端不可补传环境变量、凭据或文件来“帮助审查”。无法安全提供足够信息时保留人工路径。客户端本机同意、journal 更新、执行隔离和真机联调均为后续 AgentComputer 工作，不属于 Octop 服务端已实现的电脑能力。

## 验证与客户端验收

Octop 定向验证覆盖模式幂等与竞争、服务端人工/自动/完全访问、强制人工、审查隔离与无效输出、45 秒超时、配额、租约未知结果、取消与暂停、原检查点恢复和客户端 claim/摘要绑定。网页覆盖非乐观切换、稳定请求重试、范围确认、reviewing 展示和转人工原因。

2026-10-16 最终验证：后端相关 **456 项通过**，网页相关 **59 项通过**；37 个服务端源文件严格类型检查、定向 Ruff/Prettier 和 `make build-frontend` 均通过。协议 Schema 已重新导出为 v3，文档 51 个 JSON 示例及本地链接检查通过。后端定向命令为：

```bash
UV_CACHE_DIR=/private/tmp/octop-task-uv-cache uv run --no-sync pytest tests/integration/test_project*.py tests/integration/test_security_api.py tests/integration/test_usage_api.py tests/unit/projects tests/unit/db/test_db_pool.py tests/unit/db/test_usage_window.py tests/unit/test_usage_thread_totals.py tests/unit/users/test_resource_policy.py tests/unit/i18n -x -q --disable-warnings --basetemp=/private/tmp/octop-approval-final-2
```

网页定向命令在 `dashboard/` 运行：

```bash
npx vitest run src/api/modules/projectTaskSocket.test.ts src/pages/BoxProjects/TaskConversations.test.tsx src/pages/BoxProjects/index.test.tsx src/pages/Settings/Security/TaskApprovalsPanel.test.tsx
```

开发阶段按约定未重复完整 `make all`。用户后续授权重启和提交后，现有 8088 服务已完成数据库备份及重启，schema 从 40 升至 41；原项目、任务、轮次、附件、标题及旧 `ac_*` 数据的原列摘要逐表一致，数据库完整性和新增接口检查通过。提交检查还同步了旧迁移与备份测试中的最新版本断言，相关 63 项定向回归通过。提交使用仓库现有钩子，执行格式、类型、变更关联测试及网页构建；不推送代码。以上自动化使用 SQLite 与模拟模型；真实供应商、PostgreSQL 和 AgentComputer 真机链路尚未验收。

AgentComputer 接线后仍须验证：远端三模式与原本地 Runtime 分离；手机改档不建立 Mac 授权；目录/命令撤权立即阻止派发；Reviewer 允许前后暂停与断网；同一轮同客户端同参数的拒绝不能换调用 ID 或模式再次尝试；丢回执重发和重启不重复审查或执行。未完成这些客户端端到端验证前，不宣称两端已经自动审批。

## 源码定位

- Octop 策略和模型：`src/octop/infra/projects/approval_policy.py`、`approval_models.py`。
- 服务端动作：`approval_runtime.py`、`control.py`；独立请求与 Worker：`approval_reviewer.py`。
- 客户端契约与审查：`client_tool_models.py`、`client_reviews.py`、`client_tools.py`。
- AgentComputer 只读参考：`AgentToolModels.swift` 的三模式和动作快照、`ApprovalReviewer.swift` 的独立审查、`OctopClientToolRunner.swift` 的本机审批与 journal。
