# AgentComputer APP 任务计划与动作链路接入指南

Octop 的计划面板和动作链路是两种互斥的过程展示。有可见计划时，面板展示步骤、结果、当前短活动、已知时间与产物入口；不把思考、公开进展和工具详情列表嵌入步骤。无计划或关闭步骤面板时，才由动作链路开关决定过程区样式。原始执行记录始终保存，切换展示不删除数据。

最新核对实际 APP `/Users/fangyuan/Desktop/code/AgentComputer/` 的基线为 `feature/0.0.2/octp @ 4be12673af766308c6516b69ea2c6cd0be94174d`；参考版为 AgentComputer2 `da4a8afdf19d3aa654d8040629758b4b80b4d03a`，服务端为 Octop `04c17f55` 加未提交改动，保留用户已提交的审批模式修改。APP 已完成安全设置、互斥展示、原计划面板，以及原阶段式 Process、工具行和命令卡复用，显式开场与权威整轮计时也已贯通。旧版“步骤内嵌完整过程”和“远端仅线性投影”均不再描述当前 APP。本文于 2026 年 10 月 19 日按源码、[原组件记录](/Users/fangyuan/Desktop/code/AgentComputer/SPEC/029-octop-box-integration.md:64) 和 [最新 H/I 真实复验](/Users/fangyuan/Desktop/code/AgentComputer/SPEC/029-octop-box-integration.md:76) 更新；本次文档工作只读核对 Swift 与记录，不修改客户端、用户配置或发起模型请求。H/I 的 brief 实测与后续宿主错误归因修正分开，不能将已完成 I 当作新错误分支的验收。

## 配置入口与优先级

管理员在“应用设置 → 环境变量”维护两个全局默认值，缺省均为 `true`。显示时读取当前安全接口，不能拿某次历史核对或轮次生成快照代替当前配置；本轮不修改其实际值。

| 环境变量 | 作用 |
| --- | --- |
| `OCTOP_TASK_ACTION_TIMELINE_ENABLED` | 新轮次是否启用公开进展生成及适用工作工具必填宿主 `brief`；同时为客户端过程区提供当前显示偏好 |
| `OCTOP_TASK_STEP_PANEL_ENABLED` | 客户端是否显示已有计划的步骤面板；不启停规划，不改变执行顺序 |

支持大小写不敏感、允许两端空白的 `true/false`、`1/0`、`yes/no`、`on/off`。保存前校验这两项，非法值返回 `400 TASK_SETTINGS_INVALID`，不保存任何本次修改；错误只包含设置名，不回显其值。不要把这两个显示/生成开关当作权限开关。

APP 使用已有登录凭据读取安全接口，无需管理员环境变量权限：

```http
GET /api/project-tasks/settings
Authorization: Bearer <access token>
```

```json
{"action_timeline_enabled":true,"step_panel_enabled":true}
```

响应使用 `Cache-Control: no-store`，只返回两个布尔值。不要让 APP 读取 `/api/envs` 或整个环境文件。该 HTTP 接口不属于 WebSocket 命令，未新增协议帧。

服务端每次读取持久化的 `~/.octop/env`（自定义安装使用对应 `OCTOP_HOME/env`）。文件存在时，文件是这两项的权威来源：缺键回到 `true`，不回退其他 worker 可能残留的旧 `os.environ`；仅整个文件不存在时才读取启动进程环境，再缺省为 `true`。管理员保存后，新请求即可读取，多 worker 不依赖环境变量广播，不需为这两个键重启服务。

生成配置的优先级为：**本轮 `options.action_timeline_enabled` 显式布尔值 → 入队时全局默认值**。省略或 `null` 继承，不能用字符串或数字代替布尔值：

```json
{"type":"task.message","id":"command-actions-001","task_id":"task_example","data":{"request_id":"message-actions-001","prompt":"继续处理","options":{"action_timeline_enabled":false}}}
```

创建、追问和手动重试都支持该选项。`options` 不接受 `step_panel_enabled`，它只属于当前显示设置。生效值在入队时写入本轮配置，`turn.options.action_timeline_enabled` 回显实际值。暂停、审批、客户端工具等待后恢复沿用原快照；同一 `request_id` 重传仍返回原轮，不重新解析全局默认。原请求选项不变，不能把服务器解析出的默认值填回待重传请求。新轮次才重新解析默认。旧轮缺该字段按 `true` 读取，不补写历史。

显示配置与生成快照分开：当前安全接口的 `step_panel_enabled` 决定已有计划是否可见，`action_timeline_enabled` 决定无可见计划时的过程样式；显示切换可立即作用于已有任务，但不能改变该轮已经冻结的生成策略或补生进展。APP 已接安全设置读取与恢复刷新；本地开发 Runtime 的偏好继续独立。客户端面板关闭实测见交接记录第 57～61 行，其中当前布尔值依据行为与代码推断，不能描述成独立认证 HTTP 取值验证。

## 统一展示规则

| 条件 | 过程展示 |
| --- | --- |
| `step_panel_enabled && turn.plan != null` | 仅步骤面板；步骤结果、已知起止时间/耗时、当前步骤一条短活动和产物入口；无步骤内动作列表或卡外 Process |
| 无可见计划，`action_timeline_enabled=true` | APP 已复用原结构化 Process：开场、阶段标题/状态、阶段内动作及可展开阶段结果；无正式计划时只有轮次级进展和工具。工具优先 `brief`，默认不显示原始思考 |
| 无可见计划，`action_timeline_enabled=false` | 经典过程区，显示已保存的思考/供应商摘要与工具事实；旧公开进展仍保留，不因切换被删除 |

正文和答案复制始终只取 `text`。计划面板的当前短活动可以取当前步骤最新公开进展或工具 `brief/tool_label`，不是嵌套完整时间线。已知步骤耗时可由已记录起止时间计算；工具耗时只用 `duration_ms`，未知值不补造。完成后可收起面板；暂停、失败、取消停止执行动画，保留实际步骤结果。

这属于展示互斥，不是记录互斥。计划模式仍可产生 reasoning、progress 和工具事实，正常保存和同步；步骤面板不铺开这些列表。关闭面板后可通过 Process 查看。不要为了实现互斥删掉步骤关联、工具输出或历史思考。

原版截图中“已处理 → 开场说明 → 阶段对勾 → 缩进工具 → 阶段结果”是动作 Process，而非步骤面板。参考条件为 `(!stepPanelEnabled || plan == nil) && showsProcessShell`：计划数据可以参与关闭面板后的动作分组。无计划任务没有可据以生成的正式阶段，不能按段落或文字相似度猜阶段。

## 当前 APP 的具体差异

| 当前文件 | 已接线行为与边界 |
| --- | --- |
| [TaskConversationProjection.swift](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/TaskConversationProjection.swift:138) | 无可见计划时调用远端只读 Process 投影，仍使用原 ProcessHeader；有可见计划时只走原计划面板 |
| [TaskConversationRemoteProcessProjection.swift](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/TaskConversationRemoteProcessProjection.swift:52) | 按真实计划顺序和 `(plan_id, step_id)` 生成原阶段、子动作和阶段结果；结果独有步骤、旧计划和退役步骤可展开，未知归属保留轮次级记录，只有显式 introduction=true 置于阶段前 |
| [TaskConversationView.swift](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/TaskConversationView.swift:1454) | 远端投影进入共用 StageHeader、StageGuide、StageResult、ToolRow 与原命令卡；原独立 RemoteContentRow/RemoteToolDetails 已移除，没有另起远端 UI |
| [TaskConversationToolCell.swift](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/TaskConversationToolCell.swift:35) | 原工具行和命令卡接入远端参数、任意 JSON 输出及真实时间，不套用本机终端输出解析，也不由远端记录授予本地进程控制或文件打开能力 |
| [WorkspaceComponents.swift](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/WorkspaceComponents.swift:575) | 已适配 execute、ls、glob、grep、write_file、edit_file 等服务端工具语义图标；brief 缺失的历史记录仍按真实工具标签回退 |
| [TaskConversationView.swift](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/TaskConversationView.swift:109) | Process、独立 phase 与 live answer-footer 共用归属判断；等待/暂停保留真实步骤状态并静态显示，终态未完成步骤冻结，不沿用旧首帧双占位的结论 |

远端 DTO、合并、缓存、Snapshot 和 Controller 已保留六类块、计划身份、结果、显式 introduction 及整轮计时。展示快照复用同一套原组件，保持服务端 String 身份，不构造执行用 AgentPlan/UUID，也不进入本机执行状态机。关闭面板且正文块为空、只有计划结果时仍走远端 Process，结果不会因落入本机分支而消失。

原版依据：[阶段构树、规划行过滤与阶段结果](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Core/TaskConversationPresentationModels.swift:611)、[阶段组件与竖线](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Features/Workspace/TaskConversationProcessSectionContainer.swift:53)、[工具语义及文件入口](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Features/Workspace/TaskConversationToolCell.swift:103)。远端资源仍通过已有附件/鉴权下载能力处理，不能把服务端路径直接交给本机工作区文件打开器，也不能从 brief 猜附件。

此外，状态卡仍使用固定的本地执行/token 节省比例，见 [状态卡入口](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Features/Workspace/TaskConversationView.swift:3264) 与 [原型文案](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Core/Strings.swift:1364)。这是既有原型信息，不能当作 Octop 实测统计；远端计划面板已省略该底栏，状态卡尚未一致处理。答案中的合法附件链接继续使用原渲染器；工具行复用不等于远端获得任意本机文件访问能力。

原版完成 Process 可显示整轮处理耗时；远端现已使用 TaskTurn 的 started_at、finished_at、processing_duration_ms，见 [数据源映射](/Users/fangyuan/Desktop/code/AgentComputer/AgentComputer/Services/Box/OctopWorkspaceDataSource.swift:978)。计时组按 updated_at 选择权威快照，updated_at 自身不是结束时间；未知时间不从消息创建时间、更新时间、工具耗时之和或本机时钟补造。工具毫秒独立保留，0ms 也是有效事实。运行中“思考中”与终态“已处理”本身属于正常状态切换。

## 服务端生成规则与参考差异

下表保留此前对比依据并注明当前状态。服务端已补齐规划独占提示、开场通知门控、带工具正文分类和适用工作工具必填 brief；APP 原组件接线也已完成。brief 的适用范围和长度合同仍与参考版不同，不能声称完全一致。

| 位置 | 原版 | 当前 Octop 与影响 |
| --- | --- | --- |
| [PlanningFlow.swift](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Services/AI/PlanningFlow.swift:132) / [中文提示](../src/octop/i18n/zh.json:950) | create 与 mark_step 的提示都明确要求独占工具响应，宿主也拒绝混批 | 已补齐独占提示与混批拒绝，不再仅依赖失败反馈 |
| [AgentRuntime.swift](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Services/AI/AgentRuntime.swift:1130) / [execution.py](../src/octop/infra/projects/execution.py:234) | 公开动作模式下，宿主要求有效开场通知先保存，规划 create/get 和提问有明确豁免 | 已要求本轮开场先持久化，并在下一模型响应才放行业务；同批通知、旧轮通知或失败通知均不授权工作 |
| [AgentProgress.swift](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Domain/AgentRuntime/AgentProgress.swift:84) / [execution.py](../src/octop/infra/projects/execution.py:61) | 仅对指定的 19 个自有内置工作工具收紧已有 brief | 动作生成开启时，有无计划均要求适用工作工具的宿主 brief；还向 MCP 等无同名业务字段的工作工具投影元数据。保留本项目 240 字符合同，不引入参考版单行或 2048 字节规则 |
| [原版正文缓冲](/Users/fangyuan/Desktop/code/AgentComputer2/AgentComputer/Services/AI/AgentRuntime.swift:875) / [response_text.py](../src/octop/infra/projects/response_text.py) | 建计划前的正文先缓冲，确定同一响应带工具后归过程；计划执行中普通正文也归过程 | 已按本次响应工具意图分类，带工具说明及计划执行正文归过程，异常前未定正文也保留为过程；APP 不再自行猜来源 |

此前截图核对曾找到候选轮 `task_01M59C7F0AFV101TYS38KVZW85 / turn_01M59C7F0AFV101TYS38KVZW86`。检查点首批为 `planning(create) + ls`，两条失败属于 mixed_batch，均无执行起止时间；随后单独建计划成功，并已有实际完成的工作工具。采样时该轮没有 progress/notify 记录；两段建计划前的带工具正文被存为 text，后续计划内正文被存为 reasoning。截图未给轮次 ID，因此不将候选身份描述为已经由截图独立确认，也不把后续等待工具当作已执行失败。当时混批调用带 brief，其红色失败不由 brief 缺失导致；这些历史记录不被新合同重写。

服务端显式 introduction 与整轮计时已由最新 APP 接入，阶段展示、原工具行和运行占位互斥也已完成。G 实测中的 execute 没有 brief，是当时可选合同下的真实记录，客户端正确回退“执行指令”；后续 H/I 已单独验证有效 brief 和缺失后补正路径，G 仍为 null，不补写历史摘要。I 暴露的宿主错误误带用户拒绝前缀，属于下述服务端归因修正，不能靠客户端改写历史输出来掩盖。

## 本次服务端修复后的接入变化

- 动作生成开启时，工作和 planning.mark_step 之前必须已有本轮成功保存的开场。通知需独占响应，等待成功后再工作；planning.create/get 和提问豁免。通知与工作同批即使通知先完成也不放行工作，恢复检查点后仍维持此规则。关闭动作生成时不要求开场。
- 动作生成开启时，适用工作工具新注入的宿主 brief 必填、非空且最多 240 字符，有无计划均适用；planning、通知和提问缺失 brief 豁免，显式非法参数仍校验。业务同名字段保留原合同，关闭动作生成时不注入、不要求宿主 brief。每次工作调用都校验，不按曾开始的 call_id 豁免；已派发客户端结果仍按原 call_id 消费，原生 respond 恢复不重新调用 handler。
- HITL 中的宿主准入错误使用内部 octop_host_error 分支，保留原调用身份、工具名、本地化原因及 ToolMessage status=error；不添加用户拒绝前缀或套旧 JSON 错误包。真实用户 deny 仍走 native reject，客户端实际 result_json 仍走原 respond；这些都是服务端内部恢复语义，不新增 APP 命令或公开审批选项。
- progress.introduction 是可空严格布尔值，来自宿主：本轮首条成功保存通知为 true，后续通知为 false，旧数据为 null。通知先持久化再返回工具成功；同调用重放不重复追加，旧轮开场不会授权新轮，历史缺标记不按文字补造。
- 模型普通正文等待本次响应工具意图确定；带工具正文、计划执行中的正文归入过程，确认无工具且不在执行计划时才进入 text/reply。异常和取消前已收到的未定正文、摘要保留为过程。APP 不应再次按内容文字修正来源。
- TaskTurn.started_at 为首次实际开始时间，finished_at 为完成/失败/取消时间；processing_duration_ms 为两者间墙钟毫秒数，包含审批、暂停和恢复等待、排除首次排队。运行中或任一边界未知时为 null。恢复不重置开始时间，旧轮不回填，不能将该字段冒充单个工具执行耗时。
- 数据库升级至 44，仅为 project_task_turns 增加两列可空时间；历史原列不重写。WebSocket 仍为 v3，新增字段可空，完整 Schema 和示例已同步。APP 已接新标记与整轮耗时，现有工具 duration_ms 及步骤时间继续独立。

## 规划执行合同

主模型通过 `planning` 工具决定复杂任务是否需要计划，APP 不调用规划工具。新轮次采用固定有序计划：标题非空、最多 200 字符，1～12 步；步骤文字非空、最多 500 字符。同轮只能创建一次，标题、步骤文字、数量和顺序创建后冻结。

| 模型工具命令 | 行为 |
| --- | --- |
| `create(title, steps)` | 宿主生成 `plan_id` 和稳定 `step_id`，修订为 1，启动第一步并记录时间 |
| `get` | 读取已有完整计划；没有计划时返回工具错误 |
| `mark_step(expected_revision, step_id, status, result)` | 只能结束当前执行步骤；状态为 completed/blocked/skipped，结果或原因非空、最多 2000 字符 |

完成或跳过后由宿主启动下一步；受阻时停止推进。任意时刻只有当前步骤可执行，不能通过元数据越步。规划调用必须独占一个工具批次，混批整体拒绝，正式计划只由主任务管理。完成或受阻后撤下执行工具，只能总结。

尚有未结束步骤却生成终稿时，轮次为 `failed/TASK_PLAN_UNFINISHED`；正式计划受阻时为 `failed/TASK_PLAN_BLOCKED`。它们是执行后的轮次状态与 `error_code`，不表示首次发送请求未被接收。计划结果是模型基于实际事实报告的说明，不自动等于产品验收通过。

当前不支持 `update`、`start_step`、`select_step`、清空、改排、并行步骤或同轮重建计划。数据模型仍保留旧版本最多 100 项的读取边界、`retired_steps`、旧身份与结果，不删除或迁移历史；这些历史可读字段不代表新的运行命令仍可使用。

## 数据与版本合并

WebSocket 仍为 v3，连接 `/api/project-tasks/ws`，沿用 `task.create/message/retry`、`snapshot/turn/result` 和原游标。没有新增顶层 plan/progress 事件。REST 详情、历史页和订阅共用数据模型；附 [完整 Schema](project-task-websocket.schema.json) 与 [虚构 turn 帧示例](examples/project-task-planning-turn.json)。

| 数据 | 合并规则 |
| --- | --- |
| `plan / plan_revision` | 最新完整计划及独立版本；无计划为 null/0，有计划时对象 revision 一致 |
| `content_blocks / content_revision` | 完整有序数组，按独立内容版本替换，以稳定 block_id 保持身份，不能重复追加 |
| `list_revision` | 只控制摘要；不作为忽略新结果、工具详情或思考的条件 |
| `plan_id / step_id` | 执行时固定关联，恢复和迟到结果不重新绑定；无 ID 的旧记录不按文字或位置补配 |
| `interactions / interaction_revision` | 沿用原审批、提问和暂停规则，状态保护不能吞掉较新内容或计划 |

先选择各自最新快照，再做终态保护；合并成功后将完整正文与 cursor 一起保存。终态新结果仍需刷新。旧计划快照、退役步骤和未关联记录继续保存；切换面板不修改这些数据。普通追问与手动重试创建新轮，计划不继承；暂停恢复同轮保留计划、身份和工具回执。

内容块仍为 `text / reasoning / reasoning_summary / tool / plan / progress`。计划执行阶段的普通正文归入公开 reasoning 过程，只有计划结束后的最终回答进入 text/reply；提前终稿而未 mark 当前步骤仍报 TASK_PLAN_UNFINISHED。成功规划与通知不重复显示为普通工具；失败保留失败工具块。`progress` 不进入累计回复或答案复制。

| 工具字段 | 客户端处理 |
| --- | --- |
| `brief` | DTO 允许缺失/null，历史未知不补造；新动作模式下适用工作工具的生成约束为非空且最多 240 字符，不是实际参数或成功凭据 |
| `tool_label / tool_name` | 本地化友好名称和真实工具名；brief 缺省时回退 |
| `arguments` | 实际业务参数对象，未知为空 |
| `output` | 实际 ToolMessage.content 的 JSON 值；保留对象、数组、字符串、数字、布尔和 null |
| `started_at / finished_at` | 实际调度及服务端观察结束的 UTC 时间；意图/审批等待不等于已开始 |
| `duration_ms` | 已记录的非负工具耗时；未知为空，不从轮次时间推算 |

`task_step_id` 是模型侧可选宿主元数据。动作生成开启时，宿主为适用工作工具注入 required brief：字符串去除两端空白后非空，原值最多 240 字符；planning、message_notify_user 和 ask_user_question 不因缺失 brief 被拒绝，显式非法参数仍按适用规则校验。宿主只扩展发给模型的 schema，不改真实业务定义；工具原定义已有同名 brief 时保留其类型、required 与业务意义，不把业务值当宿主标注。

每次工作调用都校验 brief，不按曾开始的 call_id 豁免。未执行的新旧调用缺失/无效 brief 时，在审批授权消费与实际派发前返回工具错误供模型纠正，不能修改已批准参数来补全。已派发客户端结果仍按原 call_id 消费，原生 respond 恢复不重新调用 handler，不因补 brief 重新派发；历史完成记录保持原值。通过校验及现有审批后，只剥离宿主元数据再派发。APP 不额外把这些元数据加入工具定义或调度参数，也不从命令或 reasoning 猜摘要。公开工具输出不包含私有 artifact、additional_kwargs 或加密 reasoning。底层工具已经截断的内容不能在客户端补回。

若缺 brief、开场/步骤/批次门控或 question_limit 等宿主错误发生于 HITL 检查点，服务端通过内部 `octop_host_error` 决定恢复，核对原 call_id 和工具名，再返回原本地化原因的 `ToolMessage(status="error")`。客户端收到 failed 工具块和原始错误字符串 output，不再收到新套的 JSON 错误包或错误的 `User rejected…` 前缀；真正用户 deny 仍保留原生 reject 语义。已派发客户端的实际 result_json 仍走原 respond，不能拿成功 respond 伪装宿主失败。

`output` 的公开 DTO 仍为任意 JSON，不改成仅字符串，也不要求 UI 根据错误文字判断来源。该内部决定不暴露为 WebSocket 命令或 allowed_decisions。任务 HITL 中间件按原名称替换既有槽位，保持检查点拓扑；主任务审批映射不继承给子代理/peer 的独立配置。已完成 I 的真实旧输出、G 的 null brief 和其他历史记录不修改；本轮只读审计没有待恢复的旧 inline 错误项，不新增历史迁移。

关闭生成动作链路时，不注册 `message_notify_user`，不注入宿主 brief 生成字段/提示；普通工具执行、结果和供应商公开思考仍保存。既有业务工具自己定义的同名字段不受开关影响。

工具内容块仅供展示，不触发本机执行。真正本机工具仍使用独立 `tool.call`、领取和结果回传。附件以实际 `artifacts` 与下载接口为准，不能从计划文字中的路径猜产物。

## APP brief 真实复验 H/I

[客户端 SPEC029 第 76～85 行](/Users/fangyuan/Desktop/code/AgentComputer/SPEC/029-octop-box-integration.md:76) 记录了两条独立真实模型任务，使用原组件界面，没有为此修改 Swift/UI、工具定义或模型提示，也没有重新运行此前构建和定向单测。

- H 有两步计划，实际 brief 为“打印验收标记，核对真实命令链路”，原工具行显示这条具体说明；参数只有 command，实际执行退出码 0、工具 12ms。待审批时步骤保留 in_progress、中性静态暂停图标，工具为“等待执行”，输出及执行时间全为空。冷启动期间用户处理了审批，因此只核对原 turn/start/plan/call/brief 身份持续一致，不能宣称恢复前后完整快照 hash 相同。
- I 无计划，首次 execute 缺 brief，在审批前保存 failed 工具块，没有任何关联审批或执行开始/结束/耗时。模型换用新 call 并补说明后，只有新 call 获批并实际执行一次，退出码 0、19ms；最终仍保留首次失败和随后成功两条事实。该首次失败当时被错误包装成 `User rejected the tool call…`，并没有真实用户拒绝；原记录保持，后续宿主错误分支修正不能算作已完成 I 的实测结果。
- G 的完整轮次 hash 保持，旧 brief 仍为 null。H/I 的审批回执均来自用户 allow_once，测试工具未发送批准或模式修改。空白、恰 240/241 字符、业务同名字段、关闭动作和提问豁免没有新增真实模型验收，不能以服务端定向用例替代。

## 接入验收

- 有计划且步骤面板开启：只出现计划面板，展开步骤没有完整思考/进展/工具列表；答案独立。
- 关闭步骤面板：恢复 Process；已有计划按稳定步骤身份显示阶段标题、子动作和阶段结果，无计划保持轮次级列表；动作开关切换结构化与经典样式，原始数据及游标不变。
- 生成首帧、无正文、已有正文、待审批、恢复及终态：同一运行状态只显示一个主要占位；阶段和工具收展保留独立稳定身份。
- 工具语义：文件读取、搜索、编辑和命令显示对应图标/摘要，合法资源入口可打开，参数、输出和未知时间仍保留真实边界。
- brief 生成与读取：动作生成开启时，有/无计划的适用工作调用均要求有效宿主 brief，不凭曾开始的 call_id 豁免；未执行的新旧缺失调用不消费审批或派发，模型纠正不得改写已批准参数。控制工具及业务同名字段保持原合同，历史 DTO 的 null 不补造，已派发客户端结果沿原调用消费。
- 错误归因：宿主准入错误保留原 call_id/工具名及本地化原因，工具 status 为 failed，output 为实际错误字符串；不冒充用户 deny，不将内部 host_error 暴露为客户端决定，不改写历史 I。
- 全局默认变更：安全 GET 返回最新布尔值；旧轮生成快照不变，新轮继承新值；显式本轮覆盖有效。
- 暂停、审批和断线恢复：固定计划身份、工具归属及生成快照；只恢复订阅，不重发消息或 task.open。
- 严格顺序：只能结束当前步，宿主自动推进；未完成或受阻不误报轮次成功；混批与子代理不能改主计划。
- 缓存与历史：完整字段和 cursor 一致往返，已保存旧记录只读保留，不补造身份或执行时间。

此前客户端 [SPEC029 第 64～73 行](/Users/fangyuan/Desktop/code/AgentComputer/SPEC/029-octop-box-integration.md:64) 已记录原阶段树、工具行/命令卡、占位互斥、缓存与整轮计时接线，以及核心协议定向 48 项、应用定向 133 个用例和 macOS/iOS Simulator 双端构建通过。真实 G 展示两阶段及结果、一次实际 execute 和整轮计时，冷重开保持轮次与游标。随后 H 的真实待审批已补充观察中性静态状态，H/I 的具体 brief 及缺失补正结果见上节。真实 iOS、VoiceOver、真正断网、中途暂停/失败和更长链路仍未验收。以上均来自客户端既有记录，本次文档工作未修改或重新构建 APP，也不将它们计作宿主错误归因修正的最新联合测试或在线加载结论。
