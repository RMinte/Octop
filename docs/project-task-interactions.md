# Task 审批、提问和暂停恢复

状态：Octop 服务端支持审批、提问、暂停及原轮断点续接，任务协议保持 **v3**。本次新增 `interrupted` 状态和 `TaskTurn.recovery`，客户端须更新状态解码与恢复入口；本次未修改 AgentComputer Swift/UI，也未完成真机端到端接线。客户端工作见 [AgentComputer 交接文档](agentcomputer-task-interactions.md)。更新日期：2026 年 10 月 19 日。

本手册保留审批、提问和暂停恢复的操作规则。模式权威、命令适用范围及自动审查说明见[审批模式与 Reviewer](project-task-approval-modes.md)。

## 执行边界

| 类型 | 请求与决定由谁保存 | 执行位置 |
| --- | --- | --- |
| 服务端审批 | Octop `TaskTurn.interactions` | Octop 主机；普通 `execute` 根据固定模式走人工、独立 Reviewer 或完全访问，其他审批继续原人工规则。 |
| 服务端提问 | Octop `TaskTurn.interactions` | 回答作为原 `ask_user_question` 调用结果交回模型。 |
| 客户端工具 | Octop 保存调用、模式、审查建议及结果；客户端保存本机授权与执行 journal | 用户电脑；`tool.review` 已提供给 `exec_command` 接线，审查建议不授予本机权限。 |
| 手动暂停 | Octop 保存原轮次的 `pause_requested` | 在模型/工具安全边界保存断点，继续仍是同一个 turn。 |

两端审批各自约束所在执行端。成功领取客户端调用不等于用户批准本机操作；服务端不会为同一本机调用复制一张服务端审批卡。问题回答、默认值、跳过和超时均不构成操作授权。

只使用 `/api/projects`、`/api/project-tasks`。项目继续独立于专家，不恢复专用 AgentComputer/Bridge 协议，不迁移或删除 `ac_*` 数据。服务端仅保存 `client_directory`，不探测或读取电脑目录。

## 状态与同步

任务摘要和轮次共同使用以下状态：

| `status` | 含义 |
| --- | --- |
| `queued` | 首次执行或从断点恢复已排队。 |
| `running` | 正在执行，或服务端交互处于 `reviewing` 自动审查。 |
| `waiting_approval` | 存在 `pending` 的服务端人工审批。 |
| `waiting_answer` | 等待问题回应。 |
| `waiting_client_tool` | 等待客户端工具实际结果，不推断是否正在本机审批。 |
| `paused` | 已到达安全边界，用户暂停生效。 |
| `interrupted` | 已中断，原轮和计划仍保留；是否可继续及是否须核对操作结果见 `recovery`。 |
| `completed` / `failed` / `cancelled` | 本轮终态。 |

所有等待、暂停和中断仍保留 `active_turn_id`，追问返回 `TASK_BUSY`，归档与删除仍受活动任务约束。`interrupted` 不是终态，也不表示模型仍在执行。进入/离开等待或中断的摘要变化进入 `list_revision` 和原列表增量同步。同一断点含多种等待时摘要按用户暂停、审批、提问、客户端工具的优先级显示；具体待办以全部交互和本机 journal 为准。

`TaskTurn` 新增：

- `interactions`：本轮交互完整快照，包含历史已处理项。
- `interaction_revision`：单调递增，交互、用户控制意图或续接能力变化时递增，独立于 `content_revision`。
- `pause_requested`：用户希望暂停；执行中为 true 时应显示“正在等待安全暂停点”，不能提前宣布已暂停。
- `approval_policy_at_start`：本轮入队时的任务审批模式快照；每个动作另外固定当时的模式，之后切换不改写历史动作。
- `recovery`：无诊断时为 `null`；中断或失败时包含 `can_resume`、`needs_reconciliation`、`phase`、`reason_code`、`cause_type`、`diagnostic_id`，以及可空的资源类型、资源 ID 和底层状态码。失败不因此变成可恢复原轮；错误诊断不返回异常正文、凭据或私有请求参数。

这些字段通过 REST 详情/历史、WebSocket `snapshot` / `turn` / 命令回执以及 SSE 返回。交互变化进入 `event_cursor` 指纹。按交互版本合并卡片和暂停意图，按内容版本合并正文块；迟到正文快照不能重新打开已经处理的请求。任务摘要按 `list_revision` 合并，审批模式另按 `approval_policy.revision` 保留最新已确认值。

## Task 审批模式

TaskSummary / TaskDetail 的 `approval_policy` 为 `{mode, revision, updated_at}`。新建任务顶层 `approval_mode` 默认 `ask_for_approval`，不放入本轮 `options`。`ask_for_approval`、`approve_for_me`、`full_access` 三种模式始终可选，无需管理员开放。管理员通过 `/api/admin/security/task-approvals` 仅设置 `require_human_tools`。任务资源 `approval` 返回固定包含三种模式的 `allowed_modes`、`reviewer_ready` 和 `execution_profile:"host_process"`；Reviewer 未就绪不限制选择，执行审查不可用时转人工；服务端目录不是系统沙盒。

当前任务切换使用 `task.approval.set`，data 为 `{request_id,expected_revision,mode}`，或 `PUT /api/project-tasks/{task_id}/approval-policy`。`expected_revision` 对应独立模式版本，成功返回当前 TaskDetail。该幂等键保存在任务级别；相同原请求重试只返回当前状态，同键异体返回 `TASK_REQUEST_CONFLICT`，版本竞争为 `TASK_APPROVAL_POLICY_CONFLICT`。

切换只影响尚未固定的后续动作，不能自动处理已出现的人工审批。首版模式治理服务端 `execute` 和待客户端接入的本机 `exec_command`，不扩大 MCP、发送、发布等其他工具的既有授权。禁止规则和强制人工工具优先于三档模式；问题答案仍独立于授权。

## 交互记录

| 字段 | 约定 |
| --- | --- |
| `interaction_id` | 服务端生成的不透明字符串。 |
| `task_id` / `turn_id` / `tool_call_id` | 固定任务、原轮次及原工具调用。 |
| `kind` | `approval` 或 `question`。 |
| `revision` | 单项版本；回应带 `expected_revision`。 |
| `status` | `reviewing`、`pending`、`resolved`、`cancelled`、`expired`。 |
| `payload` | 固定审批提案或问题，不能由客户端替换。 |
| `resolution` | 处理结果、来源和答案；未处理为 null。 |
| `review` | 可选的只读审查回执；包含 review_id、状态、decision、reason_code、原 action_digest、模型与 profile_version。 |
| `created_at` / `expires_at` / `resolved_at` | UTC 时间；界面使用 Octop 服务端配置时区。 |
| `dispatch_started` | 服务端一次性派发标记，只读；不代表操作已成功完成。 |
| `supersedes_interaction_id` | 明确恢复后新审批所续接的原超时审批 ID；普通交互为 null，旧超时记录保留。 |

审批 payload 为 `tool_name`、`arguments`、`execution_host: "server"`、`reason`、`action_digest`、`allowed_decisions`，以及固定动作的 `approval_mode`、`approval_revision`、`action_signature`。`action_digest` 绑定原调用、配置与模式快照，提交必须原样回传；`action_signature` 用于同轮相同动作的拒绝去重，不接受客户端改写。

`reviewing` 表示 Reviewer 正在处理，不显示人工允许/拒绝按钮，也不产生“待审批”徽标。自动允许/拒绝直接转 `resolved`；不确定、超时、配额不足或无效输出转回同一交互的 `pending`，显示原因后交给用户。自动结果以紧凑工具详情展示，不永久占据人工审批大卡。当前权限及安全策略在恢复和派发时重新检查；自动或完全访问结果不能覆盖撤权、禁止规则或强制人工要求。已保存的拒绝在切换模式或取消原审批策略后仍保持。

提问 payload 为 `question`、`header`、`options: [{label, description}]`、`multi_select: false`。始终支持自由文本。每轮累计最多三问，每次工具最多三问，每题最多三个单选项；问题最多 1000 字符，选项 label/description 最多 200 字符，header 最多 24 字符。第一项是推荐默认值。

## 回应命令

WebSocket：

```json
{
  "type": "task.interaction.respond",
  "id": "command-approve-001",
  "task_id": "task_example",
  "turn_id": "turn_example",
  "interaction_id": "interaction_example",
  "data": {
    "request_id": "response-approve-001",
    "expected_revision": 1,
    "response": {
      "kind": "approval",
      "decision": "allow_once",
      "action_digest": "原样回传服务端 action_digest"
    }
  }
}
```

REST 等价入口：`POST /api/project-tasks/{task_id}/turns/{turn_id}/interactions/{interaction_id}/respond`，body 为上例 data。

| 交互动作 | `data.response` |
| --- | --- |
| 允许本次 | `{kind:"approval", decision:"allow_once", action_digest:"…"}` |
| 拒绝 | `{kind:"approval", decision:"deny", action_digest:"…"}` |
| 自由回答/选中选项后确认 | `{kind:"question", action:"answer", answer:"…"}` |
| 显式跳过 | `{kind:"question", action:"skip"}` |
| 关闭问题卡/采用默认值 | `{kind:"question", action:"use_default"}` |

答案去除首尾空白后非空，最多 4000 字符。`skip` / `use_default` 不接受客户端自报答案，结果由服务端依据固定问题生成。界面选择选项只填入答案，仍需确认提交。

成功回执 `result.data` / REST 200 返回当前 `TaskDetail`。它证明决定已保存，不证明工具已执行或本轮完成。卡片确认处理后才收起；请求超时不自动批准，不主动重交用户 prompt。

`data.request_id` 是原轮次内的持久幂等键，至少 8、至多 128 个字母、数字、下划线或连字符。网络重发使用新的命令 `id`，复用原 `request_id` 和完全相同的载荷。服务端返回当前任务状态，不重新应用旧意图。相同幂等键换内容为 `TASK_REQUEST_CONFLICT`。同一请求被别的设备先处理、版本/摘要不符、迟到或取消后提交均返回 409；跨用户及错误归属为 404。

## 默认值、期限与取消

| 处理 | `resolution.outcome` | `resolution.source` |
| --- | --- | --- |
| 用户明确回答 | `answered` | `user` |
| 用户明确跳过 | `skipped` | `user` |
| 用户采用默认值且有选项 | `defaulted`，答案为第一项 label | `user_default` |
| 用户采用默认值但无选项 | `unanswered` | `user_default` |
| 十分钟超时且有选项 | `defaulted`，答案为第一项 label | `timeout` |
| 十分钟超时且无选项 | `unanswered` | `timeout` |
| 用户允许/拒绝审批 | `allow_once` / `deny` | `user` |
| 独立 Reviewer 允许/拒绝 | `allow_once` / `deny` | `auto_reviewer` |
| 完全访问允许普通命令 | `allow_once` | `full_access_mode` |
| 同轮相同服务端动作已被拒绝 | `deny` | `previous_denial` |
| 审批十分钟超时 | `deny`，交互状态 `expired` | `timeout` |

期限在 Octop 创建交互时固定，断网、切页、重连及手动暂停不重置。问题结果带 `authorization:false` 交回模型；显式拒绝作为未获授权工具结果交回模型。尚未派发的审批超时保留拒绝记录并中断原轮，用户明确继续后重新审批，不把超时当作允许。达到三问上限后不再创建卡片，直接提示模型依据已有信息继续。

Reviewer 独立请求的期限为 45 秒，输入最多 32 KiB，输出最多 4 KiB；结果只接受封闭结构 `{"decision":"allow_once|deny|requires_user"}`。审查不带工具、主 Task 历史或工作区访问，使用本轮模型并产生独立用量。原十分钟交互期限不因自动审查或转人工延长。服务端相同原轮次、参数和执行配置的已拒绝动作再次提出时，直接保留拒绝，不重新收费审查；结果未知的已发出请求不自动重发。

客户端同一轮、同 client_id、工具名和完整 JSON 参数已有 Reviewer 拒绝时，换 call_id、目录或模式均不再创建新动作或审查，服务端以 `TASK_CLIENT_TOOL_CLOSED` 保守终止本轮；不同 argv 可以重新审查。客户端本机用户拒绝仍由本机 journal 负责保存和约束。

`task.cancel` 仍是停止原轮次，等待中的轮次立即关闭未决交互与客户端调用，迟到决定不能恢复它。客户端工具十分钟截止时间不延长；尚未领取的调用过期后不能派发。已经领取但未收到结果的调用保留原领取身份，禁止新派发，原执行者可用原 `claim_token` 补交实际结果；超时本轮进入中断状态，不能推断操作未发生。

## 主动暂停和恢复

```json
{"type":"task.pause","id":"pause-command-001","task_id":"task_example","data":{"request_id":"pause-intent-001","turn_id":"turn_example"}}
```

恢复把 type 换为 `task.resume` 并使用新的 request_id。REST 为 `POST /api/project-tasks/{task_id}/pause` 和 `/resume`，body 同 data。响应都是当前 `TaskDetail`。

暂停是合作式的：正在进行的模型请求或工具操作可先完成，在下一个模型/工具边界保存检查点；已经发生的操作不回滚。`task.resume` 可撤销尚未生效的暂停意图。审批/提问/客户端等待期间也可暂停，此时仍可保存回应，但不会继续执行。手动暂停的恢复仍须收齐原断点全部必需结果。

`task.resume` 也用于用户明确继续 `interrupted` 的原轮次。服务端重新校验断点、资源和权限；成功后仍使用原 `turn_id`、`plan_id` 和 `step_id`，保留已完成步骤及工具结果，不新增或重复显示原用户消息。恢复准备阶段中断时可以重新准备尚未进入执行的资源，已进入图执行则从原检查点续接。命令成功仅表示恢复意图已接受，最终以推送状态为准。

只在这个明确恢复入口为尚未派发的超时审批创建新交互：旧项仍为 `expired`、`deny/source=timeout`；新项有新 ID 和摘要，通过 `supersedes_interaction_id` 指向旧项，并按当前策略重新审批。用户或 Reviewer 的显式拒绝不因恢复失效，已开始派发的动作也不会重新获得授权。普通等待超时的处理保持拒绝，不自动反复弹出同动作审批。

Reviewer 可在手动暂停期间保存审查结果，但不能据此恢复执行。客户端接入自动审批后，派发前仍须在线 `tool.get` 核对 `dispatch_allowed`、`manual_review_required`、原模式和审查绑定，再复验本机权限；离线或状态未知不能新派发。本机已开始的操作不回滚，沿用原调用与 journal 保存真实结果。

控制命令绑定精确 turn。完成或取消后的新控制意图返回 `TASK_CONTROL_INVALID`；重复已接受旧 request_id 仅返回当前状态，不会暂停后续轮次。恢复继续原配置和同一 turn，不使用重试入口。

`recovery.can_resume=false` 时不能直接继续。`needs_reconciliation=true` 表示存在已派发但结果未知的操作，应先核对原执行者 journal、既有结果或外部实际状态，不能盲目重放。客户端已保存的同一次结果可补交；取消、失败或完成轮次不再接收未完成调用的新结果，已完成调用的完全相同结果重传仍可读取回执。仅在原轮确实无法续接时提供 `task.retry`“重新执行”，它会创建新轮，必须说明可能重复之前的操作。仍处于 `interrupted` 的原轮须先由用户明确 `task.cancel` 停止，收到取消终态后才能重试。当前没有单步重试命令，不能把 `task.retry` 标成“重试此步”。

## 持久恢复及范围

数据库保存私有检查点引用、中断到交互/客户端调用的映射、控制请求摘要、交互快照和中断诊断，并保存任务审批策略、客户端动作模式及独立 `project_task_reviews`。SQLite 与 PostgreSQL 使用配对结构，迁移可修复缺列且不重写原正文。原生检查点存于每轮 Harness 的系统目录；它与 `project_task_contexts` 的后续聊天上下文用途不同。

schema 46 增加 `recovery_json` 和服务端执行收据。每轮使用独立文件锁协调同一执行主机上的进程：原生断点写入与运行租约回收互斥，旧租约不能写回断点。模型和工具节点之间同步持久化后再推进；锁等待在后台线程执行，不跨异步写入持有主数据库锁。此机制不提供多台执行主机共享断点的能力。

等待先排空原生流使中断落盘，再保存业务记录并释放 Worker 租约，等待期间其他任务可执行。结果到齐后 Worker 重新排队，以原检查点恢复，不重新发送原 prompt。并行工具按原生中断 ID 分别回应；工具扇出的检查点索引用于核对原调用身份，同名同参也不能仅凭名称猜测。

独立 Reviewer Worker 使用单独执行槽和数据库租约。审查记录按执行端和原交互/调用唯一绑定；过期、取消和失去租约后迟到结果不能恢复授权。已发出但结果未知时转人工，未派发的排队审查可以继续调度；不重放未知模型请求或原工具。审查用量保存于 review 行并计入用户累计配额，不重复累计主轮次模型用量。

等待与运行租约分开处理，合法等待不按 90 秒运行心跳过期回收。回应、过期、取消和恢复准备使用 owner 事务锁；派发前再次检查原调用的批准、参数、租约和取消状态，并保存执行身份。已完成收据只作为原调用结果复用；新的未执行动作重新校验权限。崩溃发生在实际执行期间或派发后结果未知时保存中断诊断并禁止自动重放。缺失/不符的检查点不能用聊天文本替代，也不能把“已开始”标记当作成功收据。

网络、模型或资源暂时不可用会保留可恢复中断，诊断记录具体阶段和资源。技能在本轮准备完成后使用已校验的固定缓存，恢复不重新下载同一轮技能。确定的资源损坏、配置错误或没有有效恢复点时不宣称可以继续。

缓存固定 Skill 文件内容，包含插件附带技能；插件工具和中间件仍按当前安装代码校验加载，MCP 凭据不进入缓存。部分 MCP 加载器会先记录并吞掉底层连接异常，此时公开诊断只报告准确的服务器标识和工具发现不可用，不伪造原始异常。

运行服务所在电脑休眠时不能保证继续执行。服务部署在这台 Mac 上，本机模式保证的是唤醒后核对断点并续接；客户端连接断开不等于执行服务休眠。需要合盖期间持续执行时，应将 Octop 执行服务部署到独立常开主机。

当前持久交互支持 **主 Task 图**。子代理/同步团队内部的客户端工具仍走既有 broker；嵌套图中的人工中断尚未提供可提交的持久卡片，无法核对主图工具调用时按中断失败，不能声明支持嵌套恢复。需要用户回应的工作应由主 Task 发起。这项限制不改变既有子代理/团队功能范围。

定向验证覆盖真实 Harness 磁盘断点、等待释放执行槽、Worker 替换、重复与竞争提交、审批摘要/版本、拒绝/超时、权限变化、主动暂停安全边界及取消后的迟到结果。协议请求和响应的完整结构以 [JSON Schema](project-task-websocket.schema.json) 和 `/api/docs` 为准。
