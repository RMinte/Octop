# 项目和任务工作区：功能保留与验收清单

本清单区分原有 Task 能力、此次页面重排、新补交互以及仍保留的旧入口。它不是“全站旧会话已退场”的声明。2026-10-23 已完成本轮针对性回归、构建、服务重启及下述真实模型与页面验收；没有修改 AgentComputer Swift/UI。首次验收时尚未提交或推送，后续用户已授权整理提交到独立分支并尝试合入 `feature/agentcomputer-foundation`，不推送。

首次验收基线为工作树 `codex/task-workspace-redesign@0d150a33`，当时主目录 `feature/agentcomputer-foundation` 处于同一提交且干净。本轮实现及此前 `fb52` 的项目删除修复当时均为工作树中的未提交修改，以下结果针对该工作树及其构建产物。后续整理保留两个独立提交：删除修复以 `6352b8d5` 保留原 `fb52` 来源，正式页面改造另行提交；实际提交与合入结果以 Git 历史和交付说明为准，提交均正常执行仓库钩子。

## 输入区

| 功能 | 此前状态 | 本轮处理与核对点 |
| --- | --- | --- |
| 可选项目、可选专家/团队、通用助手 | Task 已有 | 在工作区与输入区重排；项目和专家不是两套对话。 |
| Ask / Plan / Craft | Task 已有 | 保留紧凑模式选择，权限仍由服务端执行。 |
| 模型与推理开关、强度 | Task 已有 | 由 `TaskExecutionSettings` 提供紧凑入口；按模型 `reasoning_config` 展示，不能向不支持关闭的模型发送非法参数。 |
| Skill、MCP/连接器、插件、子代理 | Task 已有执行配置 | 资源菜单集中展示，已选资源可见、可移除；资源凭据仍由当前主机按使用者解析。 |
| 知识库 | Task 已有 | 保留可见知识库选择、继承与禁用；不是把旧专家的私有库无条件带入 Task。 |
| `@文件` | 旧 Chat 有，Task 尚无快捷选择 | 本轮新增目录选择与键盘操作；只插入相对路径引用，不读取正文、不上传、不执行。 |
| `/Skill` | 旧 Chat 有，Task 尚无快捷选择 | 本轮新增搜索和选择，绑定实际包/SkillHub 引用，同时设置本轮完整 `options.skills`。 |
| 附件、粘贴、拖放、预览、移除、上传反馈 | Task 已有 | 保留；不以引用专家工作区附件接口代替 Task 附件。 |
| 语音输入、识别状态、回复朗读 | Task 已有 | 保留 `TaskVoice`，仍受设备/HTTPS 能力限制。 |
| 提示词润色 | Task 已有 | 保留生成后预览、修改、显式应用；草稿已变化时不能覆盖。 |
| 网络搜索、记忆开关、审批策略 | Task 已有 | 状态与入口重排；审批策略的作用域是当前用户在当前主机上的策略，不应呈现为旧 Thread 局部设置。 |
| 上下文用量、账单用量、压缩 | Task 已有 | 保留 `TaskContextUsage` 与压缩操作；上下文估算和真实模型账单不能混为一项。 |
| 草稿、发送、执行中排队、队列取回/删除/重试 | Task 已有 | 保留消息、附件、本轮选项与版本快照；请求结果不明时复用原 request_id。 |

关键源：`dashboard/src/pages/Chat/components/ChatInput.tsx`、`ChatInputActionsRow.tsx`、`ChatInputPreviewBar.tsx`、`dashboard/src/pages/Chat/hooks/useSlashMentionInput.ts`；Task 对应 `TaskComposer.tsx`、`TaskExecutionSettings.tsx`、`TaskComposerResources.tsx`、`useTaskComposerResources.ts`、`TaskKnowledgeField.tsx`、`TaskPromptPolish.tsx`、`TaskVoice.tsx`。

### 文件引用边界

- 已有 Task：通过 `TaskHostContext.taskWorkbenchApi.scope/tree` 使用当前 Task 的权威目录及 scope revision，不查询专家目录。
- 项目草稿：`GET /api/projects/{project_id}/workspace/tree-children` 必须提供 `expected_project_revision`；infra 校验当前用户、项目归属、归档、删除 fence、版本和允许的服务端根目录，复用 `BackendWorkspace` 列表和路径约束。
- 无项目的新草稿，或项目没有服务端目录：明确禁用文件选择，保留附件入口；不会为搜索创建空 Task、Thread 或临时项目工作区。
- 当前是按目录筛选与逐级浏览，不承诺全仓递归搜索。系统目录及越界软链接不作为候选，不探测客户端目录。
- `@relative/path` 与 `@"path with spaces"` 仅是交给模型的路径提示。后端文件工具仍做权限和目录检查；引用不代表已读取或执行。

对应源：`src/octop/infra/projects/composer_resources.py`、`workbench.py::public_path/tree`、`src/octop/api/routers/projects.py::composer_project_files`。

### Skill 完整选择语义

- `undefined`/`null` 表示继承；完整绑定列表是专家已安装默认 Skill 与项目 Skill 的去重并集；`[]` 表示本轮禁用，不能解释成恢复默认。
- 实际 key 是包 ID + slug 或 SkillHub + slug。同 slug 不同来源不能静默覆盖。
- 专家额外工作区 Skill 无法用当前 `options.skills` 引用表达。切到自定义完整选择前必须说明并确认替换；不能声称这些内容已被合并保留。
- 当前专家默认元数据尚未取得，或旧远端未提供默认列表时，继承态先禁用自定义快捷选择并提示刷新；已经显式指定的完整数组仍可编辑，不用项目列表冒充未知专家的完整默认。
- 资源目录新增 `experts[].default_skills` 与 `has_workspace_skills`。仅处理已安装目录/配置和工作区目录元数据，不为 Composer 读取 SOUL、USER 或 Skill 正文。
- `/Skill` 快捷并不把整个旧 slash 命令分发器移入 Task。新建、停止、压缩等快捷应映射 Task 操作；专家管理命令不能伪装成 Task 操作成功。

## 会话、任务和工作台

| 功能 | 保留位置与限制 |
| --- | --- |
| 全部任务/项目筛选、搜索、专家筛选、状态筛选 | `TaskConversations` 与工作区导航；不能因新布局丢失无项目任务。 |
| 置顶、归档、恢复、删除 | `TaskConversations` 的 Task 操作菜单；活动状态和确认规则继续遵守领域约束。 |
| 编辑后重跑、重新生成、从某轮分支 | 通过 `openBranch` / forks API 创建新分支，不改写原始执行记录。已完成的 `history_only` 轮次也保留这三种操作入口，可在当前 Task 直接再分支；不能作为旧执行的重试或恢复轮。 |
| 失败重试、暂停、停止、继续、中断恢复 | 保留现有 Task lifecycle；不将取消请求已保存误呈现为所有外部动作已停止。 |
| 审批卡片、问答、恢复提示 | `TaskInteractions` 保留 revision、request_id、状态与服务端过期检查；本轮补 `TaskRecoveryNotice`，仅可安全恢复的 interrupted 原轮提供恢复按钮，未决外部执行先核对，失败轮保留原重试。 |
| 团队发言、工具进度、来源轨迹 | `TaskTurnContent` 与 `TaskSourceTrajectory`；保留 speaker/来源，不能折成只有一段 reply。 |
| Task 记忆、专家 scope/general/legacy 记忆 | `TaskMemory`、`TaskExpertMemory`；保留作用域、只读 legacy 与策略权限。 |
| 产物、下载、媒体预览、导出 | 每轮 Task 产物、产物库与 JSON/Markdown 导出继续可达。 |
| 文件、浏览器、终端、录制、Tool UI | 已有 `TaskWorkbench`，按 Task scope 和能力开放；重排为上下文工具栏不改变其权限。 |
| 旧历史、迁移状态、显式续接 | `LegacyConversationContinuation` / `TaskMigrations`；原有链接、附件与只读记录继续可达，draining/target_deleted 不得复活旧写入。 |
| 私人助手 | 全局独立入口 `/resident`，不经普通专家/通用 Task 选择器。固定私人身份、长期对话、独立历史与记忆；专业工作通过明确委派进入 Task。 |
| 高级历史兼容入口 | 工作区“更多”保留旧历史入口；不以兼容入口绕过普通 Task 的来源与权限限制。 |

关键源：`TaskConversations.tsx`、`TaskTurnContent.tsx`、`TaskInteractions.tsx`、`TaskWorkbench.tsx`、`LegacyConversationContinuation.tsx`；服务端 `infra/projects/branches.py`、`interactions.py`、`task_submission.py`、`runtime.py`。

## 本轮没有宣称完成的整合

Task API 原已返回 `turn.recovery`，本轮补上薄恢复卡及折叠诊断，展示 phase、reason_code、diagnostic_id、资源和错误类型。只有 interrupted、非 history_only、can_resume 且不需核对未决执行时，才能恢复同一轮；不会把失败重试伪装成恢复，也不虚构 DTO 不存在的 mode 字段。

独立 `/workbench` 仍提供手动浏览器和终端。它的 `BrowserAiPanel`、终端 `AiPanel` 使用 `hooks/useAgentThreadChat.ts`，仍会 list/create/rebind 旧 Thread 并通过 `useChat` 发送；终端 Autopilot 还在 `useTerminalAutopilot.ts` 执行命令。这些面板不经过普通 Chat 页的 LegacyHistory guard。此次保留可达，不将其宣传为已切换 Task，也不把它们直接复制进 Task Composer。

普通专家/共享专家/团队的 Chat 页是只读历史与显式续接；特殊类型兼容分支及旧协议的收口要另外实施。改名或隐藏菜单不等于消除了这些执行链。

2026-10-24 补充私人助手正式 Web 入口：`/resident` 沿用独立 Resident API，不创建普通 Task 作为私人身份。记忆面板接入显式手工新增、修正、废弃、采集策略版本校验、候选提炼与人工批准/拒绝及有限来源证据。打开页面只读取，原始采集默认关闭；提炼需显式确认调用模型，候选不会自动晋升。此新增入口的最终部署、浏览器与真实模型验收以本次交付记录为准，不能套用上文 2026-10-23 的页面验收结果。

本次实际验收使用现有私人绑定：真实模型回答 `17+9=26`，刷新后追问得到 `28`；自然语言委派创建无项目 Task，完成后的三条检查清单回到原私人会话；历史搜索与定位、记忆手工新增/修正/废弃、一次性提醒暂停/恢复及按服务端时区投递均已通过。合成记忆已废弃，一次性提醒已完成且没有后续调度；未开启原始采集。候选提炼/批准和上下文整理的行为通过定向测试核对，本次未对已有用户原始材料发起真实模型提炼或整理。

真实流程发现并修复了非空旧私人历史首次接入的上下文写入错误：调用仓储方法在同一事务内为已完成的导入轮次首次保存上下文，重复接入不会覆盖已有上下文。没有修改数据库结构、原私人身份或用户模型配置。服务已从当前工作树重启，重启前后的原项目、Task、Thread 及 Resident 记录校验一致。

### 与旧 Chat 的快捷操作差异

本轮没有发现原 Task 已接能力因页面重排而被删除；这不代表正式 Task 页面已兼容全部旧 Chat 指令。

- 新建、停止、模型、行动模式、标题编辑和置顶仍有实际 Task 控件，但 `/new`、`/stop`、`/model`、`/mode`、`/title`、`/pin` 文本指令尚未映射这些操作。无附件的 `/compact` 是已有明确支持的 Task 控制指令；其他 `/词` 快捷当前主要用于 Skill，不能声称完整旧 slash dispatcher 已迁入。
- 旧 `/memory` 的备份瘦身与维护、`/history` 的 Thread 统计，不等同于 Task/专家记忆管理或会话列表；不能用新控件的存在来声称这些旧命令语义已完成统一。
- 旧 `@专家`、`@子代理` 快捷仅插入文本点名，再由模型按已有授权调用工具。正式 Task 目前提供 `@文件` 和 `/Skill` 快捷，未迁入上述点名菜单；专家选择器改变执行身份，子代理选择器改变本条角色资源，两者均不等价于文本点名，也不自动授予额外委派权限。
- 录制回放的实际旧入口在独立工作台 `BrowserAiPanel`，本轮仍保留。`ChatInputActionsRow` 中的回放按钮是可选回调能力，普通 Chat 页没有传入该回调；不能据此断言普通会话本来已有可用回放按钮。Task 录制面板保留开始、停止生成、审阅与应用，不宣称拥有回放接口。

## 针对性验证

以下组件和 HTTP 自动化测试不调用模型或外网，不连接真实用户库。真实模型、现有服务与页面的验收另列于后文。

```bash
cd dashboard
npx vitest run src/pages/BoxProjects/TaskComposerResources.test.tsx \
  src/pages/BoxProjects/TaskRecoveryNotice.test.tsx --maxWorkers=1
```

最终两文件 22 项通过：快捷组件 14 项覆盖文件/Skill token、项目草稿目录、已有 Task scope 与带空格路径、无项目禁用、系统路径与 traversal 拒绝、专家和项目默认绑定合并、显式空列表、工作区技能替换确认、来源冲突、默认元数据缺失、保留 `/compact` 命令、选择确认不误触发送、跨 Task 迟到响应隔离。恢复卡 8 项覆盖安全恢复、未决结果/不可恢复/失败/历史禁止恢复、操作中禁用、折叠诊断和普通状态不渲染。

```bash
PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/private/tmp/octop-unify-uv-cache \
  uv run --no-sync python -m pytest --no-testmon \
  tests/integration/test_project_task_composer_resources.py -q --tb=short \
  --basetemp=/private/tmp/octop-composer-resources-backend
```

新 HTTP/SQLite 8 项通过（5.47 秒）：不创建 Task/Thread/模型调用；owner、项目 revision（精确返回 PROJECT_REVISION_CONFLICT）、归档和删除 fence；越界/系统目录；越界软链接；仅客户端目录时拒绝；资源目录不读取专家 persona/Skill 正文。

`composer_resources.py`、`workbench.py`、`expert_profiles.py`、`tasks.py`、`api/routers/projects.py` 严格 mypy 5 源文件通过；新组件/hook/测试的 ESLint、后端相关文件 Ruff 和差异空白检查通过。

## 最后整合验证

### 自动化回归与构建

当前基线的受影响 UI 测试按各文件最新成功结果去重，共 **168 项**，包含最后新增的 Unified 工作区未保存状态下删除确认用例。该数字不是把重复执行或初轮失败日志中的通过数累加，也不是一次完整前端门禁。

| 测试文件 | 最新通过数 |
| --- | ---: |
| `TaskConversations.test.tsx` | 78 |
| `TaskConversations.queue.test.tsx` | 13 |
| `UnifiedConversations.test.tsx` | 12 |
| `TaskHostDirectory.test.tsx` | 1 |
| `sidebarNavLayout.test.ts` | 9 |
| `sidebarNav.test.ts` | 4 |
| `BoxProjects/index.test.tsx` | 12 |
| `TaskComposer.test.tsx` | 7 |
| `TaskWorkbench.test.tsx` | 4 |
| `TaskExecutionSettings.models.test.tsx` | 1 |
| `TaskExecutionSettings.knowledge.test.tsx` | 3 |
| `TaskPromptPolish.test.tsx` | 2 |
| `TaskComposerResources.test.tsx` | 14 |
| `TaskRecoveryNotice.test.tsx` | 8 |
| **合计** | **168** |

核对记录：`/private/tmp/octop-workspace-final-regression.log` 提供基础文件结果；队列以 `/private/tmp/octop-task-queue-confirmed.log` 的 13 项通过替换初轮失败结果；Unified、Composer、Workbench 和导航采用 `/private/tmp/octop-workspace-final-followup-ui.log` 的最新结果。项目表单、模型/知识库/润色和资源/恢复分别见 `/private/tmp/octop-workspace-project-tests.log`、`/private/tmp/octop-composer-workbench-targeted.log`、`/private/tmp/octop-composer-recovery-ui-final.log`。

当前基线的后端针对性回归 **137 项通过**：`/private/tmp/octop-workspace-final-backend.log` 记录 133 项，`/private/tmp/octop-workspace-expert-regression.log` 补充专家回归 4 项。这里的 137 是最终后端回归合计，前述新接口 8 项不再额外重复相加。

TypeScript 检查、ESLint 与生产构建均通过。ESLint 为 0 errors，保留 `Experts/components/EditAgentDrawer.tsx` 中两条既有 React Hooks 依赖 warning；构建日志 `/private/tmp/octop-workspace-final-build.log` 记录 `tsc -b && vite build` 成功。本轮未执行或宣称 `make all`、完整测试门禁全部通过。

### 服务重启与已有数据

本轮服务已重启，验收时 PID 为 `77157`；健康检查正常，数据库状态为 `true`，schema 仍为 **56**。

在新增真实 UI 验收任务之前，对重启前后下列数据逐表比较行数和行内容哈希，结果一致：

| 数据 | 重启前后行数 |
| --- | ---: |
| 项目 | 18 |
| Task | 187 |
| 轮次 | 797 |
| 产物 | 15 |
| 附件 | 38 |
| 旧 Thread | 4 |

这项检查证明重启未改变所比较的已有记录；随后有意新增的 QA Task 和模型对话不属于这次重启一致性比较。

### 真实模型与页面验收

在实际页面新增 QA Task `task_01M5MJR1QTDJ0NJ3X4Z2BS0QH9`，使用真实 `Box/RM-01 VLM` 完成两轮：首轮回答 `5`，在同一 Task 追问后回答 `6`。这部分经过实际服务与模型，不使用自动化测试中的 fake model。

- 文件、浏览器、终端、录制、Tool UI 五类工作台面板均可达；这不表示五类工具的全部外部操作都已实测。
- 终端通过真实 PTY 的 `pwd` 核对该 Task 的独立工作目录，验收后已结束 PTY。
- 将该 QA Task 从通用助手切换为「通用专家」后，旧轮的历史 speaker 仍为通用助手；记忆开关区分 Task 记忆与专家长期记忆两种作用域。
- Octop 项目的概览和完整编辑表单已完成只读验收：分别检查基本信息、能力与协作、行动指南，取消退出，未保存项目修改。由项目概览新建草稿会继承 Octop 项目、使用通用助手；未发送消息，不创建空 Task。该项目没有服务端目录，文件引用据此禁用，客户端路径只展示。
- 知识库入口保留。当前环境没有配置可用的 embedding 能力，页面显示“当前不可用”，属于环境能力状态，不是移除知识库功能；本轮未据此宣称真实检索通过。

麦克风输入、真实浏览器接管和录制尚未实测。独立 `/workbench` 的 AI/Autopilot 仍走前文说明的旧 Thread 链路；本轮没有将其改成统一 Task，也没有宣称全功能均经过真实模型或真实外部工具验证。

## 2026-10-24 至 10-25 第二轮私人助手与 Task 验收

验收基线为 `feature/agentcomputer-foundation` 与 `codex/private-assistant-workspace` 同一提交 `3f9cde04`，开工时两处工作区干净。验收结束时，三处修复保留在独立工作树中，尚未提交或合回。2026-10-26 用户授权整理到独立分支并尝试合入 `feature/agentcomputer-foundation`，正常运行提交钩子、不推送；实际提交与合入结果以 Git 历史和交付说明为准。验收时 8088 服务已从该工作树重新启动，运行基线加本节三处修复，PID 为 `66940`。当时健康和数据库检查正常，schema 仍为 56，两个正式页面均返回验收构建。

### 实际页面及真实模型

- 私人历史搜索 `RESIDENT-WEB-1024-B` 命中原记录，点击后进入历史片段，原回答 `26` 及后续 `28` 仍在；“返回最新”恢复实时会话。
- 手工新增 `QA-PRIVATE-R2-1024` 合成记忆，将“三行”修正成“四行”。修正和服务重启之后，开启单条只读授权，通过真实模型读取，得到“四行”；发送提示未提供该数字。普通 Task 记忆搜索该标记不返回私人事实。
- 合成记忆验收后已废弃，默认有效列表恢复为空；“包含已废弃”仍能查到原内容和替代记录。原始采集始终关闭，单条只读授权已恢复关闭，没有新增提醒或对旧私人材料发起模型提炼。
- 在原私人会话发起一次自然语言无项目委派，实际创建 `task_01M5P5ZJBQ3SD48N47WJV0G4RZ`，标题为“QA委派验收三行文本输出”，专家为空。任务完成后，包含 `QA-RESIDENT-R2-1024-CLOSED` 的真实结果回到原会话及助手动态，打开任务链接可查看完整 Task 记录。
- 返回私人页面后，记忆读取回答及回报仍存在。只读核对本轮两条发送意图各一份、委派 Task 一份、回报一份；回报状态为 `completed` / `delivered`，归档和上下文写入均完成，无投递错误。没有用助手口头宣称替代正式完成收据。
- 普通 Task 的专家“无”、模型、Ask/Plan/Craft、附件、文件引用、Skill、语音、润色、分支/编辑/重新生成、会话工具与任务记忆入口仍可达；工作台能打开文件、浏览器、终端、录制 Skill、工具界面五类面板。此次未启动真实麦克风、浏览器接管或录制，也未为检查入口改写项目目录或执行文件操作。

### 发现并修复

1. **目录变更后重试绕过确认。** 手动重试会组装当前项目版本并执行新一轮，但缺少发送路径已有的目录确认。现在按钮和处理函数均受 `directoryChanged` 约束；确认当前目录后才可重试。原轮暂停/中断恢复仍沿用冻结配置，队列保留原 request/body 和版本。
2. **重复停止打断收据清理。** 首次取消进入最终清理时，重放停止或关闭服务会再次取消同一协程，可能使收据停在运行中。现在取消信号只发送一次，后续停止与关闭继续等待原清理完成；数据库取消收据仍幂等。两个受控阻塞回归均先在旧实现复现，再确认修复后收据取消、部分回答保留、worker lease 清除且可继续追问。
3. **私人记忆提炼缺少配额和用量记录。** 原调用未建立模型观察作用域，也未检查用户 token 配额。现按私人身份在每次实际调用前校验配额，包含解析重试；写入 `resident_memory_extract` 调用收据及实际用量。失败不写入提示、返回正文或私人异常内容，手工记忆 CRUD 仍不调用模型。

### 定向验证与边界

以下组别存在重复覆盖，不累计为一个测试总数：

| 验证组                                    | 结果                                                      |
| ----------------------------------------- | --------------------------------------------------------- |
| `TaskConversations`、队列、恢复卡         | 107 项通过，含目录变更及原轮恢复新增 6 项                 |
| 私人历史、导入种子、恢复与回报重入        | 29 项通过                                                 |
| 私人 Hook/API                             | 25 项通过                                                 |
| 重复停止新回归与原取消/恢复组合           | 18 项通过                                                 |
| 私人记忆、候选、配额、用量及跨 scope 隔离 | 15 项通过                                                 |
| 私人页面、统一布局、模型与知识库 UI       | 各组通过；知识库首轮动画时序断言抖动，单文件复跑 3/3 通过 |

2026-10-26 的正常提交门禁还纠正了数据库兜底投递测试的旧前提：仅修改 Resident 收据会被权威 Task 的后续同步覆盖，不能用抢先读到投影来证明兜底可靠。该用例现经真实 Task 持久化链路提交合成内容，在订阅前屏蔽所有 Resident 本机通知，验证周期检查仍能投递正文和完成终态；未调整生产逻辑、共享等待器或超时上限。

修改源文件的严格 mypy、Ruff、ESLint、差异空白检查及 `npm run build`（`tsc -b && vite build`）通过。本轮使用定向验证，未重新执行或宣称完整 `make all`。关键日志位于 `/private/tmp/octop-task-retry-directory-regression.log`、`octop-resident-cancel-reentry-final.log`、`octop-resident-extract-audit-fix.log`、`octop-memory-scope-audit-3f9.log` 和 `octop-resident-round2-build.log`。

本轮未发现已接入的 Task 功能因页面改造被删除；入口可达、定向回归及真实主链不代表所有外部工具均实测。上文旧 Chat slash/文本点名和独立工作台 Thread 链路的边界仍在。Resident 的知识库选择、完整项目/资源委派参数、定时执行 Task、按日历史浏览等服务端能力尚未全部接到新 Web 页面，不能据此声称接口全覆盖。候选提炼/批准/拒绝仅在隔离临时库验证，本轮没有对用户旧材料执行真实模型提炼。
