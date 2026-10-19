# 给 AgentComputer 的 Task 交互接入说明

交付日期：2026 年 10 月 16 日。**本次只修改 Octop，AgentComputer 仓库没有改动。** 参考的是 `/Users/fangyuan/Desktop/code/AgentComputer/`，不是 AgentComputer2。服务端契约与现有网页实现已就绪，本文用于客户端后续接线。

2026 年 10 月 17 日新增的计划、公开进展和动作链路，按[APP 专项接入指南](agentcomputer-task-planning-and-progress.md)升级 DTO、合并、缓存和界面；这部分同样尚未修改 APP。

任务协议现为 **v3**，已提供 Task 三档模式、Octop 远端独立 Reviewer 和客户端命令审查接口。模式范围及实现规则见[审批模式与 Reviewer](project-task-approval-modes.md)。AgentComputer 的本机增强模式同意、Reviewer 适配与派发门禁仍需按本文接线，尚未完成真机端到端验证。

## 必须一起升级的部分

Octop `/api/project-tasks/ws` 发送 `ready.protocol_version=3`。旧客户端应提示升级，不能把未知等待状态当作完成；不提供隐式旧协议兼容，不恢复 AgentComputer 专用接口或 Bridge。

1. 扩展 TaskStatus：保留 queued/running/completed/failed/cancelled，增加 waiting_approval、waiting_answer、waiting_client_tool、paused。所有等待状态仍是活动任务。
2. TaskSummary / TaskDetail 增加 approval_policy；TaskTurn 增加 approval_policy_at_start、interactions、interaction_revision、pause_requested。详情、历史、订阅事件和写入回执共用结构；数据来自 Octop，不启动客户端本地 Runtime 模拟远端等待。
3. 在共享 Socket 中增加 task.approval.set、tool.review，以及 task.interaction.respond、task.pause、task.resume。只有唯一接收循环，命令回执与订阅事件可交错；tool.review 不阻塞接收循环等待模型返回。
4. 复用原审批 Dock、问题卡与任务列表。根据来源把服务端交互发给 Octop，把本机命令审批发给现有 OctopClientToolRunner。

完整结构见 [JSON Schema](project-task-websocket.schema.json)，规则见 [交互协议](project-task-interactions.md)。建议从实际 Schema 生成/核对 Swift DTO，而不是复制本文的省略字段。

## 建议接线位置

以下是根据只读参考代码给出的定位，属于客户端后续工作：

| 位置 | 接线内容 |
| --- | --- |
| OctopTaskDetail / OctopTaskProjection | 新状态、Task 模式、动作模式快照、reviewing 交互、审查回执与暂停意图；保留同一 turn 的多段内容。 |
| OctopTaskSocket | v3 检查、新命令、稳定请求体、回执匹配和 tool.call 更新。 |
| OctopTaskSessionService | 模式更新、远端回答/审批及暂停/继续；不直接改本地全局模式 Store。 |
| OctopWorkspaceDataSource | 服务端请求与本机执行 journal 各自投影，正文更新不能覆盖待办。 |
| ConversationRunController | 待答/待批/暂停状态与提交中状态；确认后收卡，失败可重试。 |
| OctopClientToolRunner / 现有加密 journal | 本机模式同意、原调用动作/上下文摘要、远端 Reviewer 适配、授权来源和派发前在线复验。 |
| 现有 Dock、问题卡、任务列表 | 复用现有入口，展示执行位置与精确动作。 |

`interaction_id` 是不透明字符串，不能强行转为 UUID。UI 需要 UUID 时建立稳定映射，键至少包含账号、实例、task、turn、来源和服务端请求身份。

## 当前 Task 的三档模式

`approval_policy` 为 `{mode,revision,updated_at}`，模式值沿用 `ask_for_approval`、`approve_for_me`、`full_access`。Octop 是远端 Task 的模式权威；保留本地 Runtime 的 `AgentPermissionModeStore` 原设备级语义，不用它直接修改所有远端任务。

新建请求顶层 `approval_mode` 默认请求批准，不能放入 options。当前任务切换示例：

```json
{
  "type":"task.approval.set",
  "id":"mode-command-001",
  "task_id":"task_example",
  "data":{"request_id":"mode-intent-001","expected_revision":1,"mode":"approve_for_me"}
}
```

`expected_revision` 是独立的 `approval_policy.revision`。成功 result.data 为当前 TaskDetail；失败、超时或丢回执不能乐观显示已切换。显式重试使用原 request_id 和完整载荷，或先读取当前任务。跨设备冲突刷新权威模式，同一个旧请求重发不能再次覆盖用户后来的选择。

任务资源 `approval` 返回 `allowed_modes`、`reviewer_ready`、`execution_profile:"host_process"`。`allowed_modes` 固定包含请求批准、帮我审批和完全访问，三种模式始终可选，无需管理员开放。`reviewer_ready` 仅供状态展示，未就绪不限制选择帮我审批；实际审查不可用时转人工。管理员在 `/api/admin/security/task-approvals` 仅设置强制人工工具，该设置仍优先，普通用户选择模式不能改变它。

模式只影响后续尚未固定的动作；原人工卡、正在审查和已经创建但未领取的本机调用仍保持原快照。网页已提供服务端完全访问范围确认：命令在 Octop 宿主进程执行，工作目录不是系统沙盒。AgentComputer 需分别说明服务端范围与电脑范围，不能把一次服务端确认当作本机同意，也不能仅显示“两端都已生效”。

## 服务端审批

从 `TaskTurn.interactions` 取 `kind=approval,status=pending` 的项。展示 `payload.execution_host`、tool_name、arguments、reason；按钮由 allowed_decisions 决定，只允许 allow_once/deny。payload 的 approval_mode / approval_revision 是该动作固定快照，不随当前任务改档更新。

`status=reviewing` 只显示“AI 正在审批”，不显示人工批准按钮或待批徽标。自动允许/拒绝保存 `resolution.source=auto_reviewer`，完全访问允许为 `full_access_mode`，相同原动作已拒绝为 `previous_denial`；使用紧凑折叠详情。需要用户、超时或异常时原 interaction 转 pending，按 `review.reason_code` 显示原因后复用原人工卡。来源由 Octop 写入，客户端不能通过回应命令伪造自动来源。

```json
{
  "type":"task.interaction.respond",
  "id":"approve-command-001",
  "task_id":"task_example",
  "turn_id":"turn_example",
  "interaction_id":"interaction_example",
  "data":{
    "request_id":"approve-intent-001",
    "expected_revision":1,
    "response":{"kind":"approval","decision":"allow_once","action_digest":"从 payload 原样回传"}
  }
}
```

拒绝只改 decision。关闭审批界面绝不能发送允许。十分钟未处理由 Octop 过期拒绝，客户端不自行裁决超时，也不通过切模式放行原人工卡。不接项目始终允许或 allow_project；远端三档模式使用 Octop 的任务接口。

收到 `tool.call` 的本机决定继续由本机执行器处理，不发送到服务端 interaction 入口。领取、模式或 Reviewer 允许均不等于本机授权，具体步骤如下。

## 本机命令与远端 Reviewer

首版只给 `name=exec_command` 提供自动审查；`arguments.command` 是原 argv，不能改写后送审。其他客户端工具沿用各自授权规则。Call 新字段：

| 字段 | 客户端用途 |
| --- | --- |
| `approval_context` | `{mode,revision,action_digest,requires_user}`，由服务端创建原 call 时固定。 |
| `dispatch_allowed` | 原调用有效且原轮次当前未暂停/取消；只是派发检查条件，不是授权。 |
| `manual_review_required` | 当前仍必须人工；即使旧 review 允许或模式为 full，也不能覆盖。 |
| `review` | 可为空；含 review_id、status、decision、reason_code、action_digest、context_digest、model_ref、profile_version=1 和原期限。 |

客户端先核对本机目录和命令开关、硬性禁止及 Schema，再 claim 并把领取凭证落入原 journal。增强模式的本机同意应绑定实例、账号、client_id、Task、模式版本和原目录/命令授权身份，只有执行电脑上的明确用户操作能建立。手机改档、快照恢复或远端 Bool 不能建立它；没有本机同意时保持逐次批准。此同意不打开已关闭的命令权限，也不授予目录访问。

帮我审批且满足本机同意后，用与 tool.claim 相同连接已 attach 的 client_id 提交：

```json
{
  "type":"tool.review",
  "id":"review-command-001",
  "task_id":"task_example",
  "turn_id":"turn_example",
  "call_id":"call_example",
  "data":{
    "request_id":"review-intent-001",
    "claim_token":"claim-token-from-tool-claim",
    "action_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "context":{
      "working_directory":"/Users/example/Project",
      "workspace_authorization_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "command_authorization_id":"local-command-authorization-001",
      "execution_profile":"workspace_write",
      "context_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }
}
```

示例摘要必须替换为实际值：action_digest 原样取原 call；context_digest 由客户端覆盖完整 argv、真实目录、授权身份和实际执行配置生成。workspace_authorization_digest 对应原目录授权身份，command_authorization_id 对应命令授权。execution_profile 只接受 workspace_write/full_access，必须匹配真实执行配置；不得把显示用目录末级名称当工作目录。远端 Reviewer 的 profile_version=1 是独立协议版本，不照搬本地 Runtime 的 Reviewer profile 常量。

tool.review 先核对 owner、client_id、原 task/turn/call、claim_token、固定摘要、模式和强制人工要求。它只接收本机上下文，原参数由服务端读取；不能提交任意参数或服务端目标。成功回执立即返回含持久 review 的 ClientToolCall，不等待模型；首次通常为 queued/reviewing，重读可直接得到已完成结果。完成后通过 tool.call 更新，重连或派发前用 tool.get 读取。相同 call 和固定提案返回原 review，换 request_id 不会重新审查；同一 call 换摘要或本机上下文返回 TASK_APPROVAL_REVIEW_CONFLICT。

同一轮同 client_id、工具名和完整 JSON 参数已有 Reviewer 拒绝时，换 call_id、改本机目录或切 full 都不能再创建动作或发起审查，服务端返回 TASK_CLIENT_TOOL_CLOSED 并保守终止本轮；不同 argv 的新提案可以重新审查。本机用户拒绝仍由本机 journal 保留并阻止原样再试，不能依赖服务端替代本机拒绝记录。

Reviewer 在 Octop 运行，使用本轮模型的独立无工具上下文，无需在 Mac 再配置模型密钥。期限 45 秒，异常、配额不足、无效输出和未知结果转 requires_user；原 call 十分钟期限不延长。本机元数据和 argv 会发送给 Octop 及审查模型，应明确告知用户；不补传文件内容、环境变量或凭据。服务端只按文本审查工作目录，不读取本机路径。

本机处理结果：

- `allow_once`：核对 review_id、原 call、action_digest、context_digest 和冻结模式完全匹配，本机授权仍有效时保存 auto_reviewer 单次授权。
- `deny`：保存拒绝并按原 call 回传错误结果，禁止执行；不能换 call ID 或模式在同轮原样重试。
- `requires_user`：转现有本机 Dock，保留原 call、截止时间和审查原因；不复制服务端人工卡。
- `full_access`：不请求 Reviewer；只有本机明确同意该 Task 对应模式和真实范围，且 manual_review_required=false 时才可保存 full_access_mode 来源。

派发前必须在线 tool.get，要求 dispatch_allowed=true、原 call 仍为本人领取且未过期，并再次核对 manual_review_required、本机 bookmark、目录/命令授权身份、硬性禁止、账号/实例和模式同意。暂停、取消、撤权、断网或状态未知均不能新派发。命令已开始后不回滚；授权事实、执行开始和实际结果沿用原加密 journal 落盘，再通过原 tool.result 幂等回传。远端无法替客户端完成这些本机校验。

## 提问

`kind=question` 的 payload 包含 question/header/options/multi_select=false。每轮累计最多三问，每题最多三个选项，选项选择只填入答案，自由输入始终可用。回答去除首尾空白后 1–4000 字符。

提交方式与审批命令相同，data.response 替换为：

```json
{"kind":"question","action":"answer","answer":"用户确认的答案"}
```

显式跳过用 `{"kind":"question","action":"skip"}`。关闭卡片/采用默认值用 `{"kind":"question","action":"use_default"}`，不能把关闭当成跳过。默认值由服务端取第一项，无选项则按未回答继续。离开页面、退后台或断网不等于关闭卡片。

十分钟截止由服务端持久 expires_at 决定，客户端只显示时间，不自行提交超时裁决。收到多问时逐项展示/提交，剩余问题也必须保留；全部处理后服务端才恢复原工具调用。响应记录区分 answered/skipped/defaulted/unanswered 及 user/user_default/timeout。任何问题答案都不授予工具权限。

## 暂停、继续、停止

```json
{"type":"task.pause","id":"pause-command-001","task_id":"task_example","data":{"turn_id":"turn_example","request_id":"pause-intent-001"}}
```

继续使用 task.resume，新的 request_id。pause_requested=true 只表示已请求暂停，status=paused 才表示安全边界已落盘。正在执行的动作不会回滚。等待审批/提问时也能暂停，仍可回应但不会执行；继续只解除手动暂停。

停止继续用 task.cancel。retry 创建新轮次，不能用于恢复等待。命令始终带原 turn_id，不要在迟到回执里换成“当前活动轮次”。

## 重连、幂等和合并

- 首次订阅或重连先追平在线快照。离线缓存仅供展示，在线确认前禁用审批/回答；账号或实例切换清理可操作缓存。
- snapshot/turn/result 按 interaction_revision 合并交互与暂停意图，content_revision 合并内容块，list_revision 合并任务摘要，approval_policy.revision 合并模式。旧正文快照不能把已处理卡片重新打开或覆盖新模式，终态不能回退。
- 提交中保留卡片。成功 result.data 是当前 TaskDetail，表示决定已保存；通过后续 turn 观察执行结果。
- 每次用户新意图生成 request_id。发送超时、断网或丢回执后，先读当前状态；需要显式重发时沿用原 request_id 和完整原请求体，只换命令 id。同一个 request_id 换决定会冲突。
- 已接受的旧暂停/恢复请求重发只返回当前状态，不再次改变后续状态。不能为了重试而换成新的 request_id。
- TASK_INTERACTION_CONFLICT、TASK_INTERACTION_CLOSED 返回 409：刷新并按权威结果更新，不覆盖另一个设备的决定。跨 owner、错误 turn/interaction 归属为 404。
- 只有明确用户操作才提交回答或批准；切页、恢复订阅、快照重建均不能触发执行。

REST 可用于相同操作，均 Bearer 鉴权并返回 TaskDetail：

| 方法 | 路径 | body |
| --- | --- | --- |
| POST | `/api/project-tasks/{task_id}/turns/{turn_id}/interactions/{interaction_id}/respond` | 回应命令的 data |
| POST | `/api/project-tasks/{task_id}/pause` | `{turn_id,request_id}` |
| POST | `/api/project-tasks/{task_id}/resume` | `{turn_id,request_id}` |
| PUT | `/api/project-tasks/{task_id}/approval-policy` | `{request_id,expected_revision,mode}` |

本机 review 使用已 attach 的共享 WebSocket `tool.review`，没有单独 REST 入口。tool.review 的幂等权威是原 call 与完整固定提案，不能把 task.approval.set 的任务级 request_id 规则误用为允许同 call 改提案。

## 客户端联调核对

在原页面完成：三档模式及管理员上限、两端各自审批路由、reviewing/允许/拒绝/转人工、未知结果不重审、自由回答/选项/跳过/关闭、三问、超时、手动暂停/继续、同一轮多次等待、两设备竞争、丢回执重发、切页重连、旧快照晚到、取消后迟到回应。重点验证手机改档不授予 Mac 权限、同轮拒绝不能换 ID/模式重试、Reviewer 允许后暂停/撤权/离线仍阻止派发。保留本地 Runtime、本机命令审批和 journal 回归，不新增验收页面。

服务端当前持久恢复范围是主 Task 图；子代理/团队内部的嵌套人工中断尚未支持。不要据此在客户端承诺嵌套恢复。

## 验证范围

Octop 定向检查覆盖任务模式、独立 Reviewer、客户端工具协议、真实断点、审批/提问/控制及现有网页；具体命令与结果以本轮最终报告为准。按约定不重复完整门禁，不提交、推送或重启运行中的服务。AgentComputer 未修改，也未做真机端到端联调；客户端仍需实现本文的本机工作，不能把服务端验证通过当作客户端已接入。
