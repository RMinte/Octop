# Task 执行引擎与专家分离

2026-10-27。实现基线：`release/1.0.0@a12ec61e`，在该基线上的增量改造；未发布。本机部署验收见文末。

## 对象与执行边界

- Task 持有公开记录、项目、工作目录、附件、审批与当前引擎选择；切换引擎不创建新 Task，不移动目录。
- 专家持有人设、专业指令、Skill/MCP 选择与长期记忆，和执行引擎独立。无专家仍为通用助手。
- 一次执行由引擎、连接和模型共同确定。`octop` 使用 Octop Provider/model_ref；`acp` 的连接为该账号的 runner，模型为该 CLI 广告的原生 ID。
- 旧 Task 与旧 Turn 缺少引擎字段时解释为 `octop`。每轮冻结所选路由与能力；原生初始化确认的模型和恢复事实另记入该轮快照。

`infra/projects/engines/base.py` 定义共同流边界，native adapter 保留原 Harness 流程，ACP adapter 使用真实 ACP SDK。文本、思考、工具与审批投影进入同一 Task 记录。底层工具循环由各自运行时持有。

## 选择与切换

创建 Task 或空闲时 PATCH：

```json
{
  "engine": { "engine_id": "acp", "connection_id": "codex", "model_id": null },
  "action_mode": "craft"
}
```

PATCH 沿用任务配置版本检查，运行中不能更换；从下一条消息生效。`model_id=null` 使用 CLI 默认模型。ACP 不接受 Octop 的 `options.model_ref`、推理参数或自动模型路由。模型候选来自显式能力检测，用户输入不能凭目录声明冒充已实现路由。

切回同一连接时，仅在目录、账号配置、专家与资源身份一致，并且此前轮次已完成、引擎确实支持 load 时尝试恢复原生会话。恢复期间的历史回放不重复写入公开记录；恢复成功后补充离开期间的增量。恢复失败或不支持恢复则建立新原生会话、发送全量交接。

交接有长度预算，包含最近记录、早期提要、公开工具结果及未完成标记。完整历史由 Task 保存，通过只读的 `octop_task_history` MCP 工具按需检索；该 MCP 只收到当前 Task 的导出副本，不持有数据库凭据。新分支复制公开记录与引擎选择，不继承原生 session 或进行中的授权。

## 能力与目录

- ACP 支持盒子目录、专家上下文、所选 Skill 文件、标准 MCP、专家记忆召回与采集、图片（需原生广告支持）、工具事件、原生权限请求和取消。
- 专家与项目指南以明确上下文块发送。ACP 没有标准 system_prompt 注入接口，不将其称作原生 system。
- 当前 ACP 仅支持 `craft`；不伪装实现 Octop 的 ask/plan 执行限制、手动暂停/原轮断点恢复或 `/compact`。
- Octop 插件、子代理/团队、知识库及客户端工具尚无 ACP 工具桥，所选配置不支持时在受理前明确拒绝。已有配置不被静默清空。
- 专家配置了工具禁用规则时，ACP 受理前拒绝执行；Octop 的工具名不能可靠映射到各原生引擎，禁用规则不会被静默忽略。
- 有 `client_directory` 的项目仍以用户电脑目录为文件目标；ACP 当前不支持该目标，不在盒子另建目录替代。客户端路径始终不在服务端探测或读取。
- 联网、原生 CLI 的工具和内置功能由执行引擎管理，Octop 的联网偏好不被当成 native 工具沙箱。未验证 IM 身份不能继承拥有者已登录 CLI 的权限。
- 原生累计用量不混入 Octop Provider 的单轮费用；未报告用量不推算费用。

## 盒子内安装与授权

`/acp` 的受管安装、依赖准备、登录进程、连接检测都在当前 Octop 执行主机运行；网页展示进度、设备码、授权链接及所需应答，不要求 SSH。

程序文件可共用，认证环境按账号与 runner 隔离到 `acp/users/<user>/<runner-hash>/home`，统一用于登录、检查、能力检测与任务执行。宿主 API Key、SSH agent 和宿主 HOME 不被自动继承；用户明确配置的凭据只进入自己的进程环境。不会复制原先共享 HOME 的 token。

旧的连接观察缺少 `auth_scope_version=1` 时视为待检查，需在隔离的账号环境重新检测/授权。该目录划分不等同操作系统沙箱；native CLI 仍在盒子的 Octop 进程权限下运行。

远程 Task 的目录、引擎列表与能力检测来自执行盒子，经统一 Bridge 协议转发；控制端本机状态不能替代远端。安装授权由执行盒子的管理页完成，未扩大原本只允许 Task 操作的 Bridge 通道为任意安装/凭据通道。

## 审批、中断与持久化

原生 permission RPC 保存为现有 Task 审批卡，保留活连接和 worker 心跳。只映射原生的允许一次/拒绝选项，不扩大为永久原生授权；取消会关闭会话、清理进程，并允许后续任务正常运行。

`load_session` 恢复的是历史，不能在重启后恢复原权限 RPC。已派发但结果未知的 prompt 标记为中断及待核对，禁止自动重放。恢复加载失败只允许在 prompt 尚未派发时新建会话。用户可以核对工作目录后明确取消/重试。

迁移 `058_task_execution_engines.sql` / `.pg.sql` 增加 `project_tasks.execution_json`。所选引擎和内部 session 绑定随 Task 删除；公开列表/sync 不包含内部 session。绑定写入校验账号、任务、活动轮和 worker lease，旧 worker 不能覆盖接管后的状态。

## API 与 CLI

- `GET /api/project-tasks/resources`：`execution_engines`，只读安装与保存的检测状态。
- `GET /api/project-tasks/engines`：同一执行主机的引擎目录，不启动进程。
- `POST /api/project-tasks/engines/inspect`：body 为 EngineSelection；在盒子创建临时空目录会话，读取能力/模型后关闭，不发送模型 prompt。
- Task create/PATCH/Fork 的 `engine` 使用上述结构；Fork 可显式传 `action_mode`。Turn 的 `execution_engine` 保留该轮身份与非敏感实际模型/恢复事实。
- CLI `chats send/repl` 支持 `--engine acp --connection <runner>` 与可选 `--engine-model`；未指定则沿用已有 Task 的选择。
- OpenAPI 在 `/api/docs`；WebSocket schema 已同步到 `docs/project-task-websocket.schema.json`。

## 验证边界

定向验证覆盖真实 Task worker 调度、跨引擎往返、增量/全量交接、专家/目录/技能/记忆边界、审批与取消、账号和 worker 隔离、旧数据库升级、HTTP/Bridge/CLI 及网页选择接线。协议测试使用本地替身，不使用外部模型额度；下文另记本机隔离 Codex 真实账号和供应商推理验收。其他供应商、目标 Linux 盒子及 PostgreSQL/Windows 实机仍需部署验收。

2026-10-27 本地验证结果：

- `make all RUN='uv run --no-sync' PYTEST_JOBS=6` 通过：格式、Ruff、严格类型检查，以及 6903 项测试通过、31 项跳过。
- 前端构建与 `npx tsc -b` 通过；相关五个测试文件共 116 项用例均已通过。并行高负载时一例曾超过默认限时，未调整限时，随后完整重跑任务对话文件 99/99 通过，其余四个文件 17/17 通过。
- 本次修改文件的 ESLint 通过。全仓前端 ESLint 仍受两处未修改文件的既有错误影响：`Agent/Channels/components/constants.test.ts` 的未使用导入，及 `Chat/hooks/chatStore.ts` 的冗余 Boolean 调用。
- `/api/docs` 已检查引擎目录、能力检测和 Task 引擎字段的摘要与类型；`uv lock --check --offline` 和 `git diff --check` 通过。

## 本机隔离实际链路验收（2026-10-27，续）

使用独立临时 HOME、Octop 数据库、账号、项目目录和全新无凭据浏览器。没有连接正常用户服务，也没有复制宿主 CLI token。安装与登录进程、生产 Task worker、原生 CLI、ACP SDK、HTTP/WebSocket 和构建后的 SPA 均实际运行；模型响应由本机 HTTP 协议替身返回，不验证模型推理质量，也不产生外部模型用量。

- 真实安装 API 将 OpenCode `1.19.4` 和 Codex ACP `2.2.2` 安装到临时受管目录；安装 job 成功后仍为禁用。Codex 原生 CLI 为 `0.160.1`，检测正确返回待登录并禁止启用。
- Codex 网页登录启动真实设备授权进程，页面取得 `auth.openai.com` 官方链接和设备码，然后明确取消；未完成第三方账号授权，不能据此宣称供应商账号已可用。
- OpenCode 使用显式配置的本机模型端点，连接检测不发送模型消息；网页明确启用后可用于 Task，无需专家委派开关。原生能力检测发现模型候选和会话恢复能力。
- 同一个 Task 完成 `Octop → OpenCode → Octop → OpenCode` 四轮。Task ID、公开历史和项目目录保留；OpenCode 两次实际执行 `read`，通过网页审批后读到相同的 `seed.txt`。第四轮记录 `resumed=true` 和实际模型 ID，证实原生会话恢复发生。
- 截图复核后补齐 native 工具事件的两阶段语义：pending 不标执行开始，in_progress 细化同一调用的真实参数和开始事实，terminal 更新结束事实。权限 RPC 先等待此前工具通知处理，再补入同一调用已报告的原生参数；空参数保留真实的标题、位置和内容。第五轮真实 OpenCode 实读确认审批卡显示 `seed.txt` 的实际路径，完成工具具备参数和开始/结束时间，页面显示已完成，会话继续成功恢复；旧轮记录没有被重写。对于省略中间运行状态、直接报告 completed 的 ACP 工具，页面也显示完成，不补造缺失的开始时间和耗时，Octop 原有执行事实判定保留。
- 独立 `/acp` 页面不再显示专家选择或委派配置，也不读取顶部所选专家的配置。普通用户没有无权访问的管理链接；远端任务常驻执行盒子管理说明。
- 另有真实 Task worker → 本地 ACP 协议替身 → 真实 `octop_task_history` MCP 子进程的两轮验证：工作目录匹配、Skill 内容哈希一致，第二轮可检索第一轮公开历史。这里的 native agent 是协议替身，与上述真实 OpenCode CLI 验收分开记录。

续轮发现并修复了 HTTP/SSE MCP 使用错误 SDK wire 类型、独立引擎页被专家沙箱设置阻止、设备码多行提示未解析和原生工具事实缺失等问题。HTTP/SSE 现使用带 transport discriminator 的正式类型，并保留原生引擎能力检查；设备码仅在收到完整一行后展示，普通提示文字和不完整代码不被识别为验证码。

本地临时证据：`/private/tmp/octop-engine-live-local/evidence-ui/report.json`、`task-evidence.json`、`final-tool-evidence.json` 及该目录下页面截图；安装证据为 `/private/tmp/octop-engine-live-local/installation-evidence.json`。MCP/Skill 进程验证日志为 `/private/tmp/octop-native-mcp-acceptance.log`。临时 session JSON 仅供验收登录，不作为可公开证据；收尾清除其令牌，并关闭临时服务和浏览器。

续轮前端回归为 7 个文件 142 项通过，工具状态专项文件另有 39 项通过；`npx tsc -b` 和本次修改文件的 ESLint、格式检查通过。上述模型替身验收未覆盖真实第三方账号授权与供应商推理；Codex 后续真实验收另记于下一节。目标 Linux 盒子及 PostgreSQL/Windows 实机尚未验收。

续轮完整 `make all RUN='uv run --no-sync' PYTEST_JOBS=6` 通过：6923 项测试通过、31 项跳过，耗时 461.03 秒；包含最终 ACP/MCP、设备码、工具事实和审批修复。pytest 的临时垃圾目录清理仍有警告，没有测试失败。

最终 `make build-frontend` 通过，包含最后的 ACP 直接完成状态兼容修复；8 个相关前端测试文件合计 181 项通过，最后两处工具组件修改的 Prettier 检查通过。源码和产物未提交或推送。

## Codex 真实账号与模型验收（2026-10-27）

用户明确授权后，在上述隔离 Octop 账号的 Codex 私有 HOME 内完成真实设备码登录。账号需先在 ChatGPT 安全设置开启设备码登录，再发起新的登录流程。授权 job 最终为 `succeeded`，连接为 `ready`；之后显式启用并检测模型能力。没有读取或复制宿主登录凭据，授权凭据仍保存在该隔离 runner 的文件存储中，不写入验收文档或公开证据。

本轮使用 Codex ACP `2.2.2`、原生 CLI `0.160.1` 和真实 `gpt-6.1-sol`。没有配置 Codex API Key 或模型端点覆盖，本机模型替身端口已关闭；两轮的 Octop `model_ref` 均为空，`routing` 均为 null，真实调用走 Codex 自己的账号和模型。

- 创建无专家、无记忆/团队和额外资源的独立合成项目。第一轮提示只包含文件名，不含随机标识；真实 `read` 工具读取 `proof.json`，回复 JSON 精确匹配文件内的随机标识及 `17 + 25 = 42`。完成耗时 18.650 秒。
- 验收控制器核对源文件哈希后，将其移出项目目录保存为证据。第二轮提示没有重复随机标识或计算结果，要求沿用上一轮记录，计算加一并写 `result.json`。真实工具只执行了向当前项目写入该结果文件的命令，没有重读源文件；独立读取实际文件，内容精确匹配原随机标识和 `43`。完成耗时 15.052 秒。
- 两轮均为 `completed`、错误码为空，Task ID、项目目录与 `workspace_scope` 一致，所选专家始终为空。第一轮 `resumed=false`，第二轮 `resumed=true`，证明同一 Task 的 Codex 原生会话恢复成功，记录和工作目录保持连续。
- 本轮没有出现原生权限请求，因此未据此宣称完成 Codex 的审批卡验收；原生配置为 `workspace-write` 与 `on-request`，验收脚本不自动放行权限请求。
- 构建后的 SPA 也已复核：独立 ACP 页面显示 Codex 已安装、已启用和连接可用；同一任务页显示两轮真实回复、实际模型、原生会话恢复进度及完成工具。全新隔离浏览器没有页面脚本错误，截图后已关闭。

本轮脱敏证据位于 `/private/tmp/octop-engine-live-local/evidence-real-codex/`：`report.json` 为通过断言，`task-evidence.json` 为合成任务的字段白名单，`inference-boundary.json` 为授权与模型端点边界检查，`seed-after-first.json` 为移出的源文件；`ui-report.json` 及两张截图记录网页核对。真实结果仍在对应合成项目的 `result.json` 中。证据不保存登录令牌、设备码、原生私有 session ID 或思考内容。

收尾已关闭临时 API 服务与验收浏览器，并移除临时网页登录令牌文件；隔离 Codex 授权文件保留，后续可在同一隔离环境继续验证。清理状态记录在 `cleanup-report.json`。

本轮仅验证真实 Codex 授权、调用、文件执行和同引擎续轮；此前的跨引擎往返继续以本机模型替身及协议测试为依据，未扩称为多个供应商的真实推理验收。没有修改产品源码，无需重复完整测试套件；收尾仅对新增验收文档检查格式与差异。

## Codex 一键连接（2026-10-27，后续改造）

Codex 卡片统一为「连接 Codex」，已有连接时为重新连接。`POST /api/acp/codex/connect` 返回 `202 ACPSetupJob`，`kind="connect"`；后续安装、已有授权检查、必要设备码授权、连接检测和启用都由盒子上的同一个后台 job 连续执行。用户只在浏览器完成供应商授权，已有有效授权时直接复用；不再单独点击开始登录、检测或启用。其他 runner 仍保留各自的分步流程。

客户端沿用 owner 范围的 job 轮询、取消和 `/api/acp/setup.active_jobs` 恢复。关闭或刷新面板不取消任务；服务重启后重新连接会复用已完成的安装与凭据。原生设备码登录需要时展示官方链接和设备码，Codex 不展示终端应答框；首次账号未开启设备码登录时仍需在 ChatGPT 安全设置开启。Codex 的登录等待限时为 15 分钟，设备码过期时间与进程 deadline 一致，其他 runner 限时保持不变。

成功必须同时满足 `setup.connection="ready"` 与 `setup.enabled=true`；检测不发送模型消息。每阶段检查同一 runner 配置，最终只合并更新该 runner，使用条件写入保护其他并发配置和已检测状态。失败或已取消的 job 不自动启用；最终启用为短原子提交，界面在该阶段禁用取消，取消请求也会等待真实提交结果，避免先报取消后又启用。

独立 ACP 页面及专家工具抽屉共享该流程，安装/授权接口保留管理员权限。APP 后续可复用同一服务端接口，使用系统浏览器打开授权页；本轮未修改 Swift/UI，接线约定见 [APP 交接增量](agentcomputer-unified-conversations-handoff.md#执行引擎接线增量2026-10-27)。

两条实际链路均使用构建后的正式 SPA、真实 Octop 服务和真实 Codex 程序，未发送模型 prompt：

- 已授权实例先关闭 Codex，再只点击一次「连接 Codex」。后台重新检测，复用此前私有账号授权，最终 `succeeded/ready/enabled=true`，不要求再次授权或另点启用，页面无脚本错误。
- 全新临时 HOME、数据库与管理员账号从 `not_installed` 开始，只点击一次即完成真实受管安装并进入 `auth.openai.com` 设备码等待。刷新页面可恢复同一个 job；随后明确取消，最终 `cancelled`、设备码清除、Codex 保持未启用，页面无脚本错误。此路径没有再次让用户授权，不扩称为新账号完整授权成功。

本轮证据为 `/private/tmp/octop-engine-live-local/evidence-codex-connect/report.json`（已有授权）、`openapi-report.json`（连接路由的摘要、202 类型及文档页），以及 `/private/tmp/octop-codex-connect-fresh-20261027-r2/evidence-codex-connect/report.json`（全新安装与取消）。此前真实模型两轮验收继续作为模型调用证据，与本轮不发送模型消息的连接检查分开记录。

完整 `make all RUN='uv run --no-sync' PYTEST_JOBS=6` 通过：6946 项通过、31 项跳过，耗时 388.81 秒；包含格式、Ruff、760 个源码文件的严格类型检查。最终 ACP 页面和卡片两个测试文件共 38 项通过，`npx tsc -b`、修改文件的 ESLint/Prettier 及 78 项 i18n 检查通过。最后的授权提示文案修正后已重新执行 `make build-frontend`，并在两套隔离实例只读核对最终产物：Codex 只有连接/重新连接入口，既有授权已启用，新实例取消后未启用，没有旧的手动检测提示，均无页面脚本错误。该最终截图和状态在各证据目录的 `02-final-card.png`、`final-ui-report.json`。

两套临时服务及验收浏览器均已关闭，临时网页登录令牌文件已移除；原隔离账号的 Codex 授权继续保留，全新实例未完成账号授权。清理记录为各证据目录的 `cleanup-report.json`。本轮未提交或推送，目标 Linux 盒子和 APP UI 尚未部署验收。

## 本机服务部署与提交范围

用户要求重启后，本机 `127.0.0.1:8088` 已于 2026-10-27 15:19（Asia/Shanghai）按原参数和环境重启，PID 为 `23331`。启动前已备份主数据库和历史库；健康检查、数据库就绪、新连接接口及 ACP 网页均通过，用户和运行专家数量保持一致。记录为 `.runtime/8088/codex-connect-restart-latest.json`；该运行记录和数据库备份不进入代码提交。

本轮代码交付使用 `codex/task-execution-engines` 分支，范围为专家与执行引擎分离、Task 引擎切换、账号隔离及 Codex 一键连接。工作区此前已有的「继承历史再次分支」改动按开工快照分离，完整保留在工作区，不混入本轮提交；上文验收时的未提交状态为当时记录，当前交付提交以 Git 为准。APP Swift/UI 与目标 Linux 盒子验收仍留待后续。
