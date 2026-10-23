# AgentComputer 盒子集成进度

## 2026-10-23 统一会话接线与后续范围

当前 Task 可选专家，项目始终独立；本地高级工作台、团队/共享和记忆自动抽取已接入，真实 GLM-5 验收范围见 [实际验收记录](unified-task-live-acceptance-2026-10-23.md)。APP 的现有 DTO、请求版本、Task WebSocket 与工作台适配见 [最新接线文档](agentcomputer-unified-conversations-handoff.md)，本次未修改 APP 源码。

本轮批迁、远程专家和 IM/CLI/cron 的实现与验证见 [外围入口方案](unified-task-all-entrypoints-plan.md)。已新增 Bridge 统一 Task 转发并完成双实例真实模型验收；批迁工具已用真实数据副本演练，尚未修改运行主库。以下旧专用协议与排除范围保留为历史记录，以新文档区分当前实现和后续提案。

## 2026-10-15 移除专用协议和项目任务转发

用户确认当前客户端没有使用 AgentComputer 专用接口，项目暂时不需要远程转发。本轮移除 `/api/agentcomputer/v1` 和 `/api/bridge/connections/{connection_id}/agentcomputer/v1` 路由，以及专用鉴权、错误信封、请求体限制、能力查询和 HTTP 操作关闭等待；Bridge 白名单不再允许项目和任务请求。标准 `/api/projects`、`/api/project-tasks` 及其分页、增量同步和多轮会话服务保留，不新增远程替代入口。

开工基线为主目录 `feature/agentcomputer-foundation @ ab1f5172b9f89a21190ec529234e2001f169b727`。修改前已保存当前工作区和暂存区补丁快照。期间主目录推进到 `c1235c0d8a5a519ebde35f75258be9644fe0abb0`，已核对该提交恰好包含开工时的暂存改动；本次移除在此版本上验证。未修改实际数据库、已有项目/任务/历史数据或原生 App，未推送或部署。下文专用协议与旧转发记录仅说明历史实现。

验证：项目/任务、路由契约、原有 Bridge、服务启停、安装锁、OpenAPI、认证与异常处理定向 **85 passed**。隔离 `/api/docs` 返回 200；标准接口仍有类型化说明，专用路由、错误模型和分组均不在 OpenAPI 中。旧 GET 地址返回 404，旧写入地址受现有网页 GET 兜底影响返回 405，均不触发转发。9 个相关源文件严格 mypy、12 个文件 Ruff/格式检查和 `git diff --check` 通过。按约定不跑完整 `make all`；未修改前端源码，因此未重复前端构建。

复核命令：

```bash
UV_CACHE_DIR=/private/tmp/octop-uv-cache uv run --offline --no-sync pytest \
  tests/integration/test_projects.py tests/integration/test_project_tasks.py \
  tests/integration/test_project_api_contract.py tests/integration/test_bridge_api.py \
  tests/integration/test_server_lifecycle.py tests/integration/test_agentcomputer_bootstrap.py \
  tests/unit/api/test_openapi_meta.py tests/unit/api/test_jwt_auth_middleware.py \
  tests/unit/api/test_exception_handlers.py tests/unit/bridge/test_tunnel_policy.py \
  -q -n 0 --disable-warnings
```

## 2026-10-15 独立项目任务数据库收敛

当前数据库初始化只在 main 的 v20 基础上新增 `projects`、`project_tasks`、`project_task_turns`、`project_create_requests` 四张业务表；不再修改 `agents`、`threads`、`sessions` 等原有表的结构。

- 删除早期专家绑定模型的 021–027 SQLite/PostgreSQL 迁移，以及相应的 `ac_*` 仓储、项目准备、专家创建、任务映射、Run 和启动恢复实现。原有专家、会话、网关、定时任务和历史归档恢复 main 的实现，不再查询旧表。
- 保留独立模型已经在本地应用的 028–032 编号，schema 水位仍为 32；已有数据库中的旧 `ac_*` 表、旧托管字段、行数据和工作目录原样保留，不重建、不删除、不迁移到新模型。
- 网页 `/api/projects`、`/api/project-tasks` 和 AgentComputer `/api/agentcomputer/v1` 继续共用独立服务。保留安装身份、HTTP 关闭等待和主进程锁；删除旧文字任务的 opt-in/回环监听限制及恢复逻辑。独立任务仍使用现有模型选择与配额统计。
- 删除随旧实现退役的专用测试和真实模型预检脚本；更新现有迁移用例验证 main 升级时原表结构不变，以及旧 schema/数据保留。没有新增测试页面或修改原生客户端。

验证：数据库、备份、原有历史归档、HITL、隧道策略与 OpenAPI 定向回归 **254 passed**；项目/任务、专用协议、Bridge、安装身份、CLI 配置及启动关闭用例通过。Ruff 和 29 个相关源文件的严格 mypy 通过。临时 SQLite 中 main v20 → v32 只新增四张表，原表定义和原有行不变；重复迁移和新建库结构一致。用清理前代码创建的 v32 历史库，全部表定义和全部行在清理后迁移两次仍完全一致，完整性与外键检查通过。未运行完整 `make all`，未推送或改动实际业务数据库；PostgreSQL 本轮仅核对 SQL 配对及迁移实现，未连接实库。

以下交付记录保留历史过程；其中旧模型的实现路径、测试与预检命令已退役。


## 2026-10-15 AgentComputer 专用协议边界修正

用户确认：保留 `/api/agentcomputer/v1` 专用入口，改接独立项目与多轮任务服务，与网页共用同一份数据。此前把“共享底层服务”错误落实为“App 直接改用网页接口并删除整个 v1”，该方向撤销。

- 网页仍使用 `/api/projects`、`/api/project-tasks`；AgentComputer 使用 `/api/agentcomputer/v1/projects`、`/tasks`。15 个专用操作及对应 Bridge 转发共同调用独立模型，不恢复旧专家绑定、UUID 准备回执、Run/事件/文档协议。
- 专用认证仅接受 Bearer 请求头；沿用协议错误信封、禁止缓存、65,536 字节变更体上限和 HTTP 操作关闭等待。创建幂等 key 与任务 request_id 跨网页/专用入口共用；项目编辑冲突保留，客户端目录仍只存路径。
- `/capabilities` 声明 `contract=independent-project-tasks`、`contractVersion=1`，返回原安装身份、服务端时区与实际模型/worker 可用性；未接入的事件、文件、审批、问题、旧命令回执明确为 false。
- 基线仍为 `feature/agentcomputer-foundation@c34f5b73`。本轮仅修改 Octop，未提交、未推送，未修改原生 App、生产数据或重启现有 8088；旧客户端仍需适配新独立模型契约。

验证：独立项目/任务、专用协议、Bridge、隧道策略、OpenAPI、认证和异常处理定向 **75 passed**；严格 mypy 8 个相关源文件、Ruff 检查与 13 个文件的格式检查通过，`git diff --check` 通过。隔离实例 `/api/docs` 返回 200，OpenAPI 中网页与专用协议分组、15 个专用操作和对应 Bridge 都已核对。用例使用本地模拟模型，不调用真实供应商；本轮不将自动化结果计为客户端或 Ubuntu 实机验收。按约定未跑完整 `make all`。

复核命令：

```bash
uv run pytest tests/integration/test_project_api_contract.py tests/integration/test_bridge_agentcomputer_api.py tests/integration/test_projects.py tests/integration/test_project_tasks.py tests/unit/bridge/test_tunnel_policy.py tests/unit/api/test_openapi_meta.py tests/unit/api/test_jwt_auth_middleware.py tests/unit/api/test_exception_handlers.py -q
```

以下“接口收敛”记录保留当时过程，其删除整个专用协议的决定已被本节取代。

## 2026-10-15 项目任务接口收敛与安全编辑

本轮只落实接口 review 的第 1、2 项，基线仍为 `feature/agentcomputer-foundation@c34f5b73`；未提交、未推送，没有修改 AgentComputer 原生客户端。

- 网页与 App 的唯一项目/任务业务契约为 `/api/projects`、`/api/project-tasks`，共用独立服务和数据表。旧 `/api/agentcomputer/v1/*`（含旧能力查询）、对应 Bridge 转发和隧道白名单已退出公开入口；旧文字 worker 不再自动启动。旧 `ac_*` 和专家数据保留，不迁移、不删除。历史领域实现/单元测试仍保留，原公开 HTTP 测试由退役检查替代，不把旧协议当成当前能力。
- 新增 `PATCH /api/projects/{project_id}`：未提交字段不变，目录 null 解除关联，Skill/MCP 的 [] 清空；文字/列表不接受 null。`expected_revision` 冲突返回 409。完整 PUT 必须显式提交全部字段，缺失返回 422。
- `POST /api/projects` 必须携带 `Idempotency-Key`。项目与创建回执同事务写入；同账号相同 key/内容返回最初 201 响应，不因项目后来改名或目录不可用而重复创建；换内容返回 `PROJECT_REQUEST_CONFLICT`，失败不消耗 key。网页改用 PATCH，保存失败后复用本次创建 key；没有增加测试页面。
- schema 升到 32，增加 `project_create_requests`，SQLite/PostgreSQL 迁移成对提供。8088 已更新，升级前数据库备份与工作区快照位于 `/private/tmp/octop-before-api-unification-20261015-133656/`。升级后 2 个项目、5 个独立任务、6 轮对话和全部旧 `ac_projects/ac_tasks/ac_runs` 内容摘要均与备份一致；专家 ID 集合不变，仅正常启动刷新 `updated_at`。SQLite quick_check=ok、外键问题 0。

验证：新项目/任务、接口退役/OpenAPI、Bridge 隧道策略定向 55 项通过；正式网页 11 项通过（包含创建丢失响应后复用 key）；i18n 用例通过；严格 mypy 625 个源文件通过，Ruff、目标 ESLint、前端类型检查与 `make build-frontend` 通过。实际 `/api/docs#tag/projects` 已看到 PATCH 和新的创建契约，旧 agentcomputer 分组已消失。

扩展数据库/备份/完成检查得到 528 passed、6 failed；6 项均已在本轮开工前代码快照复现，涉及旧 v23/v24 fixture 缺 `standalone_box_id`，以及旧 PostgreSQL SQL 断言。它们不是本轮新增回归，也没有以跳过或改产品代码掩盖。按用户约定未运行完整 `make all`，不宣称全套门禁通过。后续 review 的取消目标、分页/事件流、会话状态和新能力声明仍未实施。

> 以下为历史交付记录，当前契约以上方“专用协议边界修正”为准。

## 2026-10-15 独立项目任务会话已接通

本轮基线为 `feature/agentcomputer-foundation@c34f5b73`，未提交或推送；仅修改 Octop 正式页面、接口和任务运行域，没有修改 AgentComputer 原生客户端。

当前切片进度：`[✓ 独立项目  ✓ 创建任务  ✓ 连续追问  ✓ 历史恢复  ✓ 配置继承  ✓ 本机 GLM]`。这是当前独立模型的交付状态，下面旧 M0–M5 表格和托管专家/Bridge 叙述仅作历史记录，不代表新模型需要它们。

项目详情默认展示任务列表与会话：第一条消息原子创建任务和首轮，后续消息继续同一任务；支持无项目任务、幂等重试、忙碌保护、停止回复、工具进度、用户隔离、后台持久化与失效 worker 恢复。每轮冻结项目配置版本，使用服务端目录、行动指南、所选 Skill/MCP；客户端目录仍只记录路径。任务不创建或关联专家。

8088 正式页面点验：项目 `proj_01M4YT3G8Q5VNQ261M9SVED1G2` 的任务 `task_01M4YX2AW334VMHB54P46PYM9H`，GLM-5 首轮回复“已记住海星-73142”，第二轮仅询问暗号，回复“海星-73142”；刷新后两轮仍在，状态均为 completed。无项目任务 `task_01M4YX53MRCRC1138WMYQZKCVE` 返回“无项目任务可用”。早期两次点验失败记录保留：一次默认模型选择不符合现有 GLM-5 配置，一次旧用量表要求专家外键；已修复模型选择与独立任务记账，没有改写失败历史。

数据库现为 v31，新增独立任务、轮次和轮次 Token 用量；用量纳入用户累计配额，原专家 Token 统计页面暂未合并展示。升级前备份分别为 `/private/tmp/octop-before-task-conversations-20261015-122002/octop.sqlite`（v29）和 `/private/tmp/octop-before-task-usage-20261015-122639/octop.sqlite`（v30），仅本机受限权限保存，不纳入提交。已有两个项目的所有字段保持不变。

验证：任务集成 8 项通过（含真实 Harness、项目文件读取、模拟 MCP 调用、技能/指南提示及 Token 记账）；项目/任务页面 10 项通过；数据库迁移与 i18n 108 项通过；现有用量/用户配额相关 33 项通过；严格 mypy、目标 ESLint、前端类型检查和构建通过。按用户约定未重复运行完整 `make all`。

当前限制：浏览器/桌面/附件、审批恢复、原生客户端适配新独立接口、真实 MCP 供应商和 Ubuntu 盒子尚未交付或实机验证。取消不回滚已发生的工具操作，也不等于证明远端模型停止计费。接口用途见 [独立项目和任务 API](api.md)。


> 当前最新交付见上方独立任务会话记录。以下是历史路线与验收记录。

更新于 2026-10-15。这里按阶段退出标准计数，不按代码提交或迭代轮数推算百分比。`●` 表示整个阶段已验收，`◐` 表示只有部分可复核证据，`○` 表示阶段尚未开始。

**整体进度：`[◐ ◐ ◐ ○ ○ ○]`，0/6 阶段完整验收；M0、M1、M2 进行中。** 当前可用切片已覆盖本地项目内直建并关联盒子项目、项目页的远端资料与 Task 目录、独立远端 Task 会话页的历史及同 Task 追问、项目首屏 Task 的离线只读目录，以及原生窗口中“本地项目小文本资料 → 盒子文档 → GLM Task 受控读取”的完整路径。Octop 网页会话页也已接入 Run 文字快照与项目文档读取摘要；这不改变整阶段的验收计数。

**产品模型校正：**AgentComputer 的 Task 就是用户可见的会话，同一 Task 内的后续消息形成 Turn，各 Turn 的处理形成 Run；内部 session/thread 只是该 Task 的一对一实现映射。Task 可以关联真实项目，也可以不关联项目。当前开发树已为无项目 Task 增加独立的持久作用域、受控 Agent、工作区、Thread/Session，以及本人创建、读取、发送和执行接口；`projectID=null` 是真实的无项目关系，不使用隐藏项目。8088 已按原数据目录备份并升级至 v27，网页登录页面使用既有 GLM 完成同 Task 双轮问答；AgentComputer 原生 New Task 的无项目入口、目录和多轮会话也已构建并通过定向测试，真实 8088 的原生窗口登录点验尚未做。目标路径是 AgentComputer 直连一个 Octop，读取和写入这个 Octop 自己的 Task 数据；本机 8088→8090 Bridge 仅是历史联调手段，不是部署依赖。

**2026-10-15 原生页面收敛：**项目与最近 Task 目录只投影当前认证盒子的完整列表。macOS/iOS 项目行进入沿用原工作区骨架的正式项目详情，项目加号打开正式创建弹窗；New Task 使用原欢迎页布局，无项目 Task 打开沿用原会话视觉的多轮页面。新建无项目 Task 在准备就绪后自动发送首轮文字，重开最近 Task 不重复发送，失败时保留原命令供恢复。旧“盒子项目/盒子任务”验收页面已从原生主流程移除，服务端接口由正式页面消费。macOS 与 iOS Simulator 构建及相关定向测试已通过；这一版尚未在真实 8088 登录的原生窗口做人工交互复验，完整门禁仍后置。

| 阶段 | 状态 | 已有可复核结果 | 尚缺的退出条件 |
|---|---|---|---|
| M0 环境与协议 | ◐ | 本机 GLM-5 工具续轮、文本流，以及离线故障和取消夹具 | 目标 Ubuntu 盒子的资源、模型协议、执行隔离与容量实测 |
| M1 盒子服务端 | ◐ | 稳定身份、持久领域、项目准备、内部派发与对账、精确取消；AgentComputer HTTP 操作关闭等待；受托 Turn 的原生 v2 历史与无工具自然完成证明；本机固定 GLM 模型的公开项目/Task/Turn、项目下 Task 和同 Task Turn 分页读取、单进程 worker、验证过的 Run 结果，以及只读阶段事件、文字快照和一致的 Task 控制面基线；8090 实例已完成同一 Task 多轮真实模型问答 | Ubuntu 盒子部署、真实工具授权、已交接停止证明、完整恢复与控制库/历史库跨库一致性 |
| M2 原生客户端 | ◐ | AgentComputer“设置 → 连接”已可查看能力；“盒子项目”可创建项目、提交文本 Task、轮询 Run 与查看结果，命令丢失响应可按原 ID 回查；新建或选择的盒子项目可加入 App 主项目列表，按盒子、用户、恢复代次和项目 ID 验证后展示名称与状态；macOS 实包已点验项目页远端资料/Task 目录、独立 Task 会话页及同 Task GLM 追问，iOS 对应路由已通过模拟器构建；已有本地项目可显式关联已验证盒子项目，也可在项目内直建直联，保留本地工作区与 Task，macOS 原生窗口已完成两种关联路径、真实模型 Task 与在线冷重开点验；Task 控制事件与 cursor 有最小原子缓存；同进程断线只读、最近一个 Task 的冷启动离线正文及项目首屏 Task 离线只读目录均已在 macOS 实包点验；当前 Run 的受控文档读取摘要已接入原生页面，本地项目小文本资料可选入草稿并经再次确认后导入盒子 | 设备配对、真实第二设备、iOS 真机点验、完整事件/审批/取消/正式成果与重连验收 |
| M3 配置与恢复 | ○ | 规划已形成 | 指令/技能版本、资源、Outbox、多设备和异常对账验收 |
| M4 调度与面板 | ○ | 规划已形成 | Schedule、Git/终端、受认证预览与资料库验收 |
| M5 迁移与发布 | ○ | 规划已形成 | 显式导入、备份恢复、TLS/配对、升级回滚和盒子实机发布验收 |

本轮[原生历史绑定](agentcomputer-native-history.md)的受托引擎定向回归为 65 passed；最终 `make all` 的 Ruff、严格 mypy（600 个源文件）和非 live 全套通过，5333 passed / 23 skipped。跳过项不计为通过。首轮受限沙箱无法绑定测试所需的 `127.0.0.1` 端口；在允许本地回环监听的环境下完整复验通过。服务关闭测试只证明本地流关闭与保守对账，不构成远端模型或工具子进程停止证明。

后续[启动只读对账](agentcomputer-native-history.md#启动时的只读历史对账)已把 ACK 后的归档缺失、Turn 缺失、归属不符、六种历史状态和初始消息落盘窗口纳入内部恢复报告及无身份信息的汇总警告日志。它只做诊断：即使历史为 `complete`，Run 仍保守中断并保留双活动占用；不会因为这项证据开放执行。定向恢复测试 37 passed；最终完整 `make all` 的 Ruff、严格 mypy（600 个源文件）和非 live 全套通过，5342 passed / 23 skipped。

**当前开发树的用户切片**是 Octop 网页 `/box-projects` 直接读取本实例项目与 Task，创建无项目 Task，在同一 Task 发送和展示多轮消息。此改动已部署到原 8088 数据目录并完成真实网页登录点验。AgentComputer 原生客户端仍保留早期“设置 → 连接”、盒子项目、关联本地项目和项目内 Task 会话的既有能力；New Task 无项目远端 Task 的可见入口、目录、会话及多轮发送已接入，但尚未在真实 8088 登录的原生实包中点验。以下按日期保留的 8090 独立实例、Bridge 和原生 GLM 点验是历史实验记录，不是当前单 Octop 产品结构或新的验收结论。用户会话认证仍在使用，设备配对未完成。

第二十六轮[自然完成内核](agentcomputer-run-completion.md)已把无工具真实 Harness 流的严格历史/持久检查点证明、完成收据、`run.finished` 和双占用释放串起来；取消造成的正常 EOF、空回答或缺证据仍保守对账。当轮 schema 升至 26，公开 HTTP 和能力开关尚未放行，8090 实例也尚未升级；本页下方记录了后续本机文本 Task 升级与实测。

第二十六轮完整 `make all` 通过，5396 passed / 23 skipped；严格 mypy 覆盖 602 个源文件。数据库与备份的旧“最新 schema=25”断言已同步到 26，旧 v25 故障注入条件仍针对 v25 本身。

**2026-10-14 本机 Task 试用验收：**以 `feature/agentcomputer-foundation @ 74ce4d82` 为基线，8090 独立控制目录先备份后升级到 adapter 0.15.0，配置已存在的 GLM-5 Provider 与固定 `textModelRef`。认证能力返回 `projects/tasks/runs.available=true`；新项目 `ready/executionReady=true`，Task 创建成功，Run 从 `queued` 到 `succeeded`，严格结果接口返回“2+2 等于 4。”。`/api/openapi.json` 中新路由的摘要、说明与类型化 2xx 响应已核对。本轮最终 `make all` 通过，5416 passed / 23 skipped；运行中的 8088 用户实例未重启。这个证据只覆盖开发机本地、无工具、单 Run 的真实模型路径；下方另记客户端实际界面点验。

**2026-10-14 主项目列表接线验收：**AgentComputer 最终 `make verify` 通过，含全量 Swift Package 测试、macOS 单测、macOS 和 iOS 构建；macOS 单测 638 passed / 7 skipped / 0 failed，项目与 Task 相关定向测试 48/48 passed。远端项目以独立身份加入主列表，创建和移除都先持久保存；账号、盒子或恢复代次不匹配时遮蔽真实名称与状态。Mac 和 iOS 均可从主项目入口打开远端文本 Task，iOS 的状态变化会刷新原生列表行。8090 实例健康接口仍返回 `ok=true` 且数据库正常。本轮未改动 8088 用户实例。

**2026-10-14 macOS 实包界面验收：**从 AgentComputer 主目录 `feature/0.0.2/octp @ 488778b7` 对应的 Debug 包实际启动并核对进程加载路径；签名信任检查曾返回 `CSSMERR_TP_NOT_TRUSTED`，但未阻止本机运行。在原生窗口中将已有 ready 项目“GLM 本机任务验收”加入主项目列表，打开项目并发送“请只回复：项目接线测试成功。”；新 Task 的 Run 完成，界面显示“项目接线测试成功。”。AgentComputer `make verify` 通过，改动已推送至用户 fork 的同名分支。

**2026-10-14 远端 Task 发现验收：**`GET /api/agentcomputer/v1/projects/{projectID}/tasks` 按创建时间和 Task ID 倒序分页，返回 `items[].task`、`items[].latestRun` 与 `nextCursor`；游标绑定盒子、用户、项目及恢复代次，权限和关联事实校验失败时不返回部分列表。新构建的 AgentComputer 原生窗口发现了服务器直接创建、App 本地索引中不存在的 Task“跨设备发现验证”，并显示其成功结果“跨设备发现成功。”。该点验验证了远端任务发现，没有使用第二台设备。

**2026-10-14 同 Task 第二轮与历史读取：**在 8090 隔离实例上，对已有 Task `f112ee1b-16de-4741-bd38-39a8375836c2` 再提交 `contextMode=newMessage`；新 Run `7c907cff-59c2-452f-ab61-8e64de5450ca` 成功。第二轮询问上一轮的原句，GLM 准确回复“跨设备发现成功。”，证实本机同线程上下文可用于追问。服务端新增 `GET /api/agentcomputer/v1/tasks/{taskID}/turns`，按持久 Turn 顺序倒序分页，逐项返回原始 `prompt`、经完整绑定校验的 `run` 与通过完成收据和 v2 历史核验的 `result`；未成功时结果文本为空。游标绑定盒子、用户、Task 和当前恢复代次；旧代次已接受的历史仍可从第一页只读，旧游标失效。8090 实例已实际读出两页两轮历史。定向新增测试 10/10、相关回归 50/50、Ruff 和定向严格 mypy 通过，本轮按用户要求暂缓完整门禁。

**2026-10-14 macOS 原生多轮点验：**最新 Debug App 由 AgentComputer 主目录的 DerivedData 构建并启动，已核对运行进程来源。在同一远端 Task 中从原生窗口发送第三轮“请只回复：原生追问验收成功。”；Run 从排队到完成，窗口展示三轮原文和回答，第三轮回答为“原生追问验收成功。”。这验证了本机 App 到 8090 GLM 的多轮交互与历史展示，未使用第二台设备。

**2026-10-14 主项目 Task 导航点验：**AgentComputer 的普通 macOS 项目侧栏和 iOS 项目目录已接入经过当前账号、盒子身份、恢复代次和项目绑定核验的远端 Task 首屏；Task 行使用独立远端路由，不进入本地执行器。macOS Debug 实包从普通侧栏展开“GLM 本机任务验收”，显示四条盒子 Task；点开“你是谁”只显示其对应回答，再切回“跨设备发现验证”仍完整显示原有三轮对话。页外 Task 的精确打开使用已有 `GET /tasks/{taskID}` 和 Turn 历史接口，先核对项目归属。四组相关 XCTest 结果包为 52 tests、53 runs（含一项动态参数化双运行）、0 failed、0 skipped；macOS 与 iOS 模拟器构建通过。iOS 真机和第二设备仍未点验。此轮只扩展 AgentComputer 客户端，8090 服务端接口未变。

**2026-10-14 阶段事件和文字快照：**新增本人 `GET /tasks/{taskID}/events` 的连续 Task 序号、固定分页窗口和轮询游标，返回白名单事件元数据，不返回内部载荷；新增本人 `GET /runs/{runID}/output`，仅在原 Run/Turn/输入与 v2 归档严格绑定后读取已落盘纯文本，未成功文本标为 `provisional=true`，成功文本仍经原完成收据核验。这是只读控制阶段与全文快照，尚非模型 token 流或工具过程；完整 `events` 能力开关仍关闭。8090 独立实验实例在备份后用新代码重启，真实认证 HTTP 读出一个三轮 Task 的 15 条连续阶段事件、一个成功 Run 的 8 字文字快照，OpenAPI 两个类型化接口均存在；8088 实例未改动。事件、输出、Turn 历史和原结果定向回归 35/35，Ruff 与定向严格 mypy 通过。按用户要求，这轮未运行 `make all`。

**2026-10-14 控制面快照与缓存恢复切片：**新增本人 `GET /tasks/{taskID}/snapshot`，在一个只读数据库快照中核对 Task、最新 Run、连续控制事件水位及可直接续读 `/events` 的 cursor；未知和他人 Task 同为 404，存储图损坏整体 503。SQLite 使用不预占 WAL 写锁的延迟读事务，PostgreSQL 使用只读 repeatable-read；跨控制库与 v2 历史库的消息、成果一致快照仍未实现。服务端快照、事件、相邻读取接口和数据库事务定向回归 57/57，Ruff 和定向严格 mypy 通过。8090 实验实例确认无活动 Run 后备份到忽略目录 `.runtime/backups/iteration-37-before-control-snapshot-20261014T104544Z` 并重启；真实认证 HTTP 读取已有三轮 Task 的水位 15、最新 Run `succeeded`，cursor 续读返回空页，OpenAPI 含类型化路由。8090 和 8088 健康接口均为 200。AgentComputer 同轮增加按盒子/用户/项目/Task/恢复代次隔离的控制事件原子缓存，事件和 cursor 同文件落盘；旧 cursor 或缺口可用新快照建立基线，但不能补造被跳过的阶段，正文仍按原 Turn/Run 接口读取。完整 C18/C19、真实断线/第二设备/Ubuntu 实机仍待验证；按用户要求继续暂缓完整门禁。

**2026-10-14 原生续读与在线冷重开点验：**从本轮明确路径的 macOS Debug App 发出同 Task 第四轮“请只回复：缓存续读验收成功。”，GLM 返回同一句；服务端 Run `succeeded`，App 展示四轮原文及回答。该 Task 的原子缓存由 15 条控制事件续到 20 条，已应用序号与 `coveredSequence` 均为 20，cursor 已保存。随后平滑退出该测试版 App 并从同一新包重启，核对新进程路径，重新打开项目后仍可看到第四轮和最新 Run 的五个阶段。此项覆盖客户端进程在线冷重开；尚未覆盖 8090 断线期间的离线读取、盒子重启或第二设备。

**2026-10-14 Task 导航与第五轮点验：**修复父级刷新重新传入旧侧栏 Task ID 导致的页面回跳，并保护本地待接收 Task、账号/恢复代次切换和首屏外精确 Task 读取。最终 macOS Debug App 从新进程启动；在主项目侧栏先选“2+2 简答验收”，再从项目页选“跨设备发现验证”，侧栏同步到目标 Task，刷新后仍留在目标 Task。原生界面提交第五轮“请只回复：导航修复验收成功。”，GLM 返回同一句，页面保持目标 Task 与五条最新阶段；本地缓存已应用和覆盖序号均为 25，cursor 已保存。AgentComputer 定向 XCTest 42/42 与 macOS/iOS Simulator 构建通过；完整门禁按用户要求后置。

**2026-10-14 盒子进程平滑重启：**第五轮全部完成后，停止仅供实验的 8090 进程，控制库及 v2 历史库 `PRAGMA quick_check` 均为 `ok`，备份隔离 home 到忽略目录 `.runtime/backups/iteration-38-before-box-restart-20261014T191417` 后以同配置重新启动。8090 和未改动的 8088 健康接口均返回 200；保持运行的原生 App 在 8090 重启后刷新目标 Task，仍显示五轮原文与回答、最新 Run 的五条阶段且不回跳。这验证已完成 Task 在同一盒子身份下的平滑服务进程重启；异常断电、断线期间读取和跨设备恢复仍未验。

**2026-10-14 同进程断线只读点验：**[AgentComputer 提交 a984fdb3](https://github.com/RMinte/AgentComputer-SwiftUI/commit/a984fdb3)让当前登录会话中已核验的项目、Task、Turn 和 Run 在网络暂断时保留为只读快照，显示上次在线确认时间；认证失败或身份变化会清空，断线和重新核验期间禁用写入。三组定向 XCTest 为 59 tests / 60 runs、0 failed / 0 skipped，macOS 和 iOS Simulator 构建、字符串目录及 `git diff --check` 通过。最终 macOS 实包 PID 40419 打开已有五轮 Task，8090 停止后刷新仍见原文、回答和阶段；项目卡片显示“可执行：否”，其他 Task 及写入入口禁用。8090 恢复后健康检查为 200，早一版实包在线刷新已验证离线标记消失且五轮内容仍在；8088 始终为 200。此项不覆盖 App 冷启动离线正文、盒子异常断电或第二设备；本轮依用户要求未跑完整门禁。

**2026-10-14 项目文档与受控读取：**服务端新增本人项目的文本资料导入、列表和读取，以及唯一可用的 `read_project_document` 模型工具。一次工具效果必须和原生 v2 历史中的调用、返回及最终答复严格绑定，才可生成 v2 完成证据并释放 Run 占用；无工具 Turn 保持原证明路径。8090 实验实例在备份与数据库检查后用 GLM-5 完成真实 HTTP 导入、Task 提交和工具续轮，结果正确返回文档中的验证码；旧无工具结果也可读取。接口、约束、证据及后续边界见[项目文档与受控读取](agentcomputer-project-documents.md)。这是单文件只读能力，不代表任意文件工具、项目资料库或写入授权已经开放。

**2026-10-14 Task 工具活动展示：**本人 Run 新增只读 `/tool-activity` 摘要，验证原 Run、派发、工具效果与完成关联后，仅投影受管文档名、读取状态和效果结果；无工具 Run 返回空列表，未知或他人 Run 同为 404，损坏记录整体 503。原生客户端在当前 Task/Run 下显示这条摘要，并在切换 Task、Run、盒子身份或恢复代次后清理。模型调用已入历史但效果日志尚未建立时，`/output` 对合法文档调用返回空的进行中快照，避免正常轮询短暂误报存储损坏；非法工具调用仍拒绝。服务端相关回归 578 passed，Ruff、定向严格 mypy 与差异检查通过；8090 真实认证 HTTP 读出旧工具 Run 的 `documents/brief-iteration40.md`、`ok/succeeded`，无工具 Run 返回空列表，OpenAPI 有类型化 200 响应。AgentComputer 定向 XCTest、macOS 和 iOS Simulator 构建、双语目录及差异检查均通过。新 Debug 实包已在原生窗口打开历史 Task“项目文档路径修复后复验”，可见“项目文档读取 → brief-iteration40.md 已读取”和最终答复“海星-73142”。8090 与原有 8088 健康接口均为 200；完整门禁按用户要求后置。

**2026-10-14 冷启动离线 Task 正文：**AgentComputer 为已持久绑定的盒子项目缓存最近打开的一个 Task 的首屏 Turn 和成功回答；按盒子、账号、项目、Task、恢复代次、服务地址及当前 Keychain 令牌指纹隔离。文件有限额、原子替换和本机权限限制；冷启动只读入口显示上次在线确认时间，不提供离线写入、项目级 Task 目录或实时 Run 状态。定向 XCTest 为 54 个测试、55 次运行、0 failed / 0 skipped，macOS Debug 与 iOS Simulator 构建、目录校验通过。新 macOS Debug 实包在线打开“GLM 本机任务验收”下已有 Task，缓存确认含一轮成功回答；仅停 8090 后完全退出并重启 App，已绑定项目页实际显示“离线快照 · 只读”、原问题及“海星-73142”，无创建或追问入口。停服时控制库与历史库 `PRAGMA quick_check` 均为 `ok`；8090 重启后与原有 8088 健康接口均为 200，原生页面刷新后离线标记消失、在线 Task 与回答恢复。iOS 真机、盒子异常断电、第二设备和完整事件投影仍未点验；完整门禁依用户要求后置。

**2026-10-14 已有本地项目关联盒子项目：**AgentComputer 为本地项目增加独立的盒子关联身份；显式关联只接受当前在线核验过的盒子项目，将同一远端项目原有的独立导航行合并到本地项目，保留本地项目 ID、工作区授权、Composer 和本地 Task。macOS 侧栏与 iOS 项目目录同时呈现本地和盒子 Task；盒子 Task 仍走独立远端页面与身份校验。解绑先持久化，再清理旧只读缓存，不删除盒子项目或本地 Task。数据/身份定向 XCTest 38/38、iOS 目录定向 XCTest 17/17，macOS Debug 与 iOS Simulator 构建、双语目录和差异检查通过；macOS UI 定向 XCTest 因 Xcode 测试宿主启动卡住而中止，不能计为通过。8090 实验服务与原有 8088 的 `/api/health` 均返回 200。完整门禁依用户要求后置。

**2026-10-14 本地项目关联原生点验：**在独立测试本地项目“盒子关联验收-20261014”中，从盒子页创建同名 ready 项目并显式关联，主列表由本地项目与远端专用行收敛为一行；本地 Composer 保持原项目，盒子 Task 从本地项目入口独立打开。原生窗口创建 Task 并启动 Run，8090 GLM 返回“项目关联任务已运行。”，页面显示完成状态。完全退出并从同一 Debug 包重启后，关联、远端 Task、原文、回答和本地 Composer 选中态仍可见。这是在线冷重开与开发机真实模型证据，不代表离线冷启动、第二设备或盒子实机。

**2026-10-14 项目内直建直联验收：**AgentComputer 的 macOS/iOS 项目页加入“创建并关联盒子项目”；目标本地项目 ID 在异步创建期间固定，离页或切换目标使旧回调失效。盒子页预填本地项目名，并继续使用原有幂等命令、原收据重试与当前身份精确核验。macOS 新 Debug 包中，从本地项目“盒子直连验收-20261014”一次点击进入，创建同名 ready 盒子项目后自动返回原本地项目，主列表保持一行，本地 Composer 仍选中该项目。随后从其盒子 Task 入口创建 Run，8090 GLM 完成并回答“直连项目任务成功。”；完全退出新包并冷启动后，关联、Task 和回答仍可见。macOS/iOS Simulator 构建、双语目录及差异检查通过；iOS 真机、第二设备和完整工程门禁依用户要求留待后续。

**2026-10-14 本地与盒子 Task 入口分流：**[AgentComputer 提交 d39e18bb](https://github.com/RMinte/AgentComputer-SwiftUI/commit/d39e18bb6ba26992e6a6972bc66e16f8b7b59f2a)使已关联盒子的本地项目在 macOS 与 iOS 的本机 Composer 上方标明执行位置，并提供直达盒子纯文本任务表单的独立按钮。仅复制当前文字草稿；预填只在盒子身份和项目重新核验、Task 加载成功后发生一次，刷新不会覆盖用户在盒子表单中的编辑。本机附件、技能和执行设置不自动转移，空文字草稿也有边界提示；实际远端写入仍由盒子表单的“创建并运行任务”触发。iOS 项目页把盒子入口移至本地 Task 长列表之前。双端 Debug 构建、双语目录及差异检查通过；macOS 新包已显示“本机执行”和远端按钮，但本轮新测试进程未保有 8090 登录会话，故草稿带入和新 Run 的原生窗口端到端点验仍待登录后完成。8090 与 8088 健康接口均返回 200；8090 本地实验账号的认证能力核对返回项目、Task、Run 均可用，服务端本轮没有改动。

**2026-10-14 本地项目资料显式导入：**AgentComputer 已关联本地项目的 macOS/iOS 页面增加“项目文档”入口，复用受认证的盒子项目文档区。用户可从本地项目已保存的 `.md/.txt` 副本选择单份严格 UTF-8、至多 16 KiB 的文本；选择只填入可编辑草稿，点击“添加文档”才调用已有的盒子 PUT 接口，不覆盖同名文档，也不读取原工作区或自动迁移网站、PDF、Word。盒子、账号、恢复代次、远端项目和本地项目 UUID 的作用域变化会清除旧选择；坏文件选择后清空旧草稿，避免误上传。资源副本逐层用不跟随符号链接的文件描述符打开，在同一描述符上校验类型、大小和有界读取。定向客户端测试分别为 17/17、8/8，macOS 与 iOS Simulator 构建、双语目录和差异检查通过。新 macOS 实包在已登录窗口打开已关联项目，实际显示“项目文档”“从本地项目资料选取”及空资料提示；该项目没有本地文件，本轮未从原生界面完成真实文件导入。8090 和原有 8088 健康接口均为 200；服务端 API 本轮未改动，完整门禁继续后置。

**2026-10-14 本地资料到受控 Task 全链路点验：**在已关联本地与盒子项目的 macOS 原生窗口中，导入仅含合成数据的 114 字节 `bridge-proof-735fbf.md`，从“从本地项目资料选取”填入草稿，再显式点击“添加文档”。盒子认证 GET 读回的正文与本地文件逐字一致。随后原生窗口创建 Task，提示只给文件名、不包含随机校验词；Run `c6a49e93-2f07-46d1-8682-13ab32cf1bc8` 为 `succeeded`，`/tool-activity` 唯一记录为 `read_project_document`、`documents/bridge-proof-735fbf.md`、`ok/succeeded`，最终结果为文件内校验词“云杉-95944”。Task、Run 的项目和 Task 归属均与已关联项目一致，原生页面同时显示“已读取”和最终回答。这证明本机当前 GLM 路径确实读取了盒子文档；它不证明模型对任意提示都会调用工具，也不覆盖第二设备或 Ubuntu 盒子。

**2026-10-14 本机 Composer 到盒子 Task 点验：**在同一已关联本地项目的原生 Composer 写入仅含合成内容的“请只回复：本地草稿转盒子成功。”，点击“在盒子创建纯文本任务”后，盒子 Task 表单显示完全相同的可编辑文字和“已带入文字草稿”提示。返回本地项目时 Composer 保留原文；再次进入盒子表单并显式点击“创建并运行任务”，Task `A932F0FA-515A-4A30-B9D6-753F58BA74E2` 的 GLM Run 完成，界面返回“本地草稿转盒子成功。”；运行后回到本地项目，原草稿仍保留。本机 Composer 的“发送”按钮未被触发，本机 Task 未被创建。

**2026-10-14 从盒子文档起草 Task：**AgentComputer 在已打开的盒子文档下提供“用此文档起草任务”，只向同一已验证项目的可编辑 Task 表单填入 `read_project_document` 和裸文件名；有现存草稿时不覆盖，点击按钮不会自动提交。关联项目和直接盒子项目均接线，Run 面板仍以真实 `/tool-activity` 判定读取。定向 XCTest 为 65 tests / 66 runs，0 failed / 0 skipped；双端 Debug 构建、双语目录和差异检查通过，完整 `make verify` 后置。新 macOS 实包在已关联项目中选中 `bridge-proof-735fbf.md`，点击按钮后确认草稿预填且编辑器获得焦点，再显式运行 Task `759465FE-6BA6-44FC-A24C-7DCE06EB1422`；Run `2d75a534-9a2b-4349-8a29-9e59ce9f1f43` 为 `succeeded`，工具活动唯一项为该文件的 `ok/succeeded`，回答包含文件内校验词，而提示本身不含该词。直接盒子项目“GLM 本机任务验收”的旧文档 `brief-iteration40.md` 也已在新实包点验：按钮把裸文件名填入当前项目草稿并聚焦编辑器，有草稿时禁用；清空后恢复可用。该项目未再次运行新 Task。

**2026-10-15 项目级离线 Task 目录：**AgentComputer 在项目已在线精确核验后，原子保存首屏最多 50 个 Task ID/标题、项目名、确认时间和是否还有下一页；不保存旧 Run 状态或列表 cursor。macOS 侧栏、iOS 目录及共用盒子项目页在冷启动瞬断时只读展示该目录，只有另行缓存了正文的最近一条 Task 可打开，其余标题提示连线后查看。读取时同时核对持久项目绑定、当前 Keychain 令牌指纹、服务地址、盒子、账号、项目和恢复代次；在线已确认 404/身份变化、解绑或认证失败会撤销旧投影与缓存。旧版仅有单条正文缓存时仍可只读打开；双窗口解绑或换令牌后，已显示的旧正文也会在渲染时重新核验并隐藏。四组定向 XCTest **30/30 通过，0 failed / 0 skipped**；macOS 与 iOS Simulator 构建、双语目录及差异检查通过。在 macOS 最终实包中先在线加载 GLM 项目与 Task 正文，再暂停独立 8090、退出并冷启动 App：原生项目页和侧栏只读显示 6 条 Task 标题，仅已缓存正文的一条可打开，其他条目禁用；恢复 8090 后刷新，项目文档、6 条在线 Task 与最近运行结果重新出现。8090 与未停过的 8088 健康检查均为 200；本轮没有修改服务端或调用模型。iOS 真机、第二设备及完整 `make verify` 仍待后续。

**2026-10-15 项目页与独立会话页接线：**macOS/iOS 的盒子项目页保留远端项目资料、文档及 Task 目录；选中 Task 后进入独立会话页，按精确 Task 查询和分页 Turn 历史显示用户消息、回答、Run 状态及受控文档读取，并在同一 Task 内发送追问。已关联本地项目的导航链为“本地项目 → 盒子项目 → Task 会话”。macOS 原生实包中先从项目页打开已有 Task，看到远端历史；再在会话页发送“请只回复：会话页面已接通。”，8090 的 GLM Run 完成且同一会话显示“会话页面已接通。”；返回项目页后从项目内 Task 行再次打开独立会话。macOS 与 iOS Simulator 构建及差异检查通过；完整 `make verify`、iOS 真机与第二设备仍待后续。本轮服务端未改动，8088 用户实例未重启。

**2026-10-15 独立盒子项目 Task 入口接线：**AgentComputer 的独立盒子项目概览现可从 Task 行打开对应的远端独立会话；在该项目中创建 Task 后也进入新 Task 的远端会话，无需先新增或关联本地项目。已关联盒子的本地项目页获得焦点时，只刷新当前项目的远端资料与 Task 目录；Run 进入终态后会刷新所属 Task 的状态。iOS 从独立会话返回项目后可再次打开 Task。macOS 与 iOS Simulator 构建、差异检查通过；Mac 处于锁屏状态，新入口的原生窗口点验未完成，本轮也未跑完整门禁，不计入新的阶段验收。独立直达会话在同进程暂断时只读保留当前内容；离开后再进入或冷启动离线正文仍限于已绑定项目的缓存范围。

**2026-10-15 Octop 网页项目与 Task 页面：**`/box-projects` 现有独立侧栏入口，可在“此 Octop”和已连接的 Bridge 目标间切换。项目页列出远端项目及所属 Task；`/box-projects/{projectID}/tasks/{taskID}` 显示分页 Turn 历史、持久 Run 状态和经过服务端核验的最终回答，并可在同一 Task 追问。创建项目、创建 Task 和发送 Turn 均使用原命令 ID 回查与同命令重试；确定拒绝的 4xx 可修改输入后重试。8088 的 Bridge 代理只开放这些页面所需的 AgentComputer 路径，沿用连接所有权和双方用户会话。前端生产构建、TypeScript、改动文件 ESLint、18 项路由/侧栏测试及 101 项后端聚焦测试通过；全量 ESLint 被两处未改动的旧文件错误阻挡，完整 `make all` 后置。8088 和 8090 均已按原数据目录重启；8090 认证 API 实际读出 4 个项目及首个项目的 Task、Turn，两个实例的页面返回 200。当前两库的 Bridge 连接均为零，8088 需由 `fangyuan` 在 `/bridge` 添加到 8090 的连接后，才能在来源选择器中看到该实例的项目；网页登录态和跨实例可视化点验仍待完成，不计入整阶段验收。

**2026-10-15 网页项目资料与连接入口：**`/box-projects` 在没有出站 Bridge 连接时直接显示地址、对端用户名和密码表单；建连成功后清除密码并切换到新来源。新建项目处于 pending/preparing 时每 2.5 秒查询精确项目状态，ready/failed 后停止；轮询失败在页面显示错误。ready 项目可列出、读取、新建 `.md`/`.txt` 文档，浏览器和 Bridge PUT 均检查 16 KiB UTF-8 限额；现有文件不能覆盖，写入结果不明时页面保留表单并要求先刷新列表核对。Bridge 代理仅新增这些文档 GET/PUT 路径，依旧校验连接归属、双方认证、单层文件名和类型化响应。后端文档/策略定向测试 21 passed；前端生产构建、改动文件 Prettier/ESLint、路由与侧栏 18 项测试通过。跨实例页面点验仍需 8088 登录用户实际建立连接，阶段进度维持不变。

**2026-10-15 网页会话执行过程：**`/box-projects/{projectID}/tasks/{taskID}` 对活动 Run 每 3 秒请求 `/output`，只在快照与当前 Run 一致、快照及 Turn 都非终态且 `provisional=true` 时展示“生成中 · 尚未验证”的文字。Run 完成时主动刷新 Turn 列表，最终答复只取已通过服务端完成收据核验的 Turn 结果；轮询失败则保留已有历史并提示可能过期。每轮的“查看文档读取”按需请求 `/tool-activity`，可刷新；仅显示受控项目文档相对路径、读取状态与工具结果，进行中空结果不会误写成“本轮没有读取”。8088 Bridge 只新增这两个精确 UUID 的 GET 代理；工具摘要模型同时约束最多一项及安全相对路径，拒绝额外私有字段、绝对路径和多项恶意对端响应。前端 TypeScript、生产构建、改动文件 Prettier/ESLint、路由/侧栏 21 项、后端输出/工具/Bridge 19 项及 i18n 78 项通过。8088 更新后与保持运行的 8090 健康接口均为 200，`/box-projects` 为 200，新匿名 Bridge 路由均返回 401；跨 Bridge 页面点验仍待 8088 用户实际建连，完整 `make all` 按用户约定后置。

**客户端验收边界：**本机原生窗口已点验首轮纯文本 Task、远端 Task 发现、普通项目下直达 Task、同 Task 追问与五轮历史展示、本地项目显式关联和关联项目的新 Run，并完成在线客户端进程冷重开、盒子服务进程平滑重启后的在线刷新、同进程断线只读浏览，以及最近一个 Task 正文和项目首屏 Task 目录的离线冷启动与重连。本地项目资料的实际文件选择、远端创建及受控 Task 读取已用合成 Markdown 在原生窗口与认证 API 双重核对；本机 Composer 文字草稿带入盒子表单、返回保留及再进入后运行也已在原生窗口点验；从已选文档一键起草并运行 Task 也已在新实包点验。单次只读文档工具摘要已接入，通用工具过程与正式成果尚未接入。真实第二设备、盒子异常断电、iOS 真机离线正文、目标 Ubuntu 盒子和设备配对仍未验。先前跳过的 7 项测试不计作通过。

**2026-10-15 无项目 Task 当前实现：**数据库 v27 允许 `ac_tasks.project_id=NULL`，并将 Turn、Run、派发及完成记录关联到 Task；无项目 Task 持有唯一盒子、工作区、Agent 和配置快照，受控 Agent 只能绑定一个 Task 或一个项目。本人 `POST/GET /api/agentcomputer/v1/tasks`、`GET /tasks/{taskID}`、`POST /tasks/{taskID}/turns` 与原命令收据查询已接入。准备完成且固定模型可用时显示 `ready/executionReady=true`；临时准备失败可复用原命令与 Task 身份重试。公开 HTTP 加本地 worker 的两轮纯文字测试已读回 Run 结果、Turn 历史、Task 快照及事件，项目表保持空；旧 Task revision 返回 409。`/box-projects` 默认显示本 Octop 的无项目 Task，也可选项目内 Task；历史 Bridge 连接选择已从该页移除。创建无项目 Task 要求配置 `agentcomputer_text_model_ref`，可使用已有 GLM Provider；默认空配置下返回 `executionUnavailable`。原 8088 数据目录已先完整备份，再配置原有 GLM Provider 并升级到 v27；启动后健康检查、外键检查和数据库完整性检查通过，原用户和 Agent 数量未变。

**2026-10-15 8088 网页实测：**在已登录的 8088 `/box-projects` 页面创建无项目 Task“单实例 GLM 会话验收”（`c21bc229-03d4-4a31-ae47-4b25b8dbe3f4`），首轮发送“请只回复：无项目会话已接通。”，页面收到同句；第二轮要求原样重复上一轮回复，仍得到“无项目会话已接通。”。刷新该 Task 页面后，两轮提问和回答仍完整显示，列表标记“已成功”。该测试仅覆盖本机单 Octop、已有账号、已有 GLM 的无工具会话；原生实包对 8088 的登录点验、异常恢复、目标 Ubuntu 盒子和设备配对仍未验。升级前完整备份位于忽略目录 `.runtime/backups/one-octop-pre-v27-20261015T003950Z`，含敏感本机数据，不提交。完整 `make all` 按用户快速迭代要求后置。

**2026-10-15 项目关联回归：**仍在原 8088 的已登录页面创建项目“单实例项目验收”（`b6854fc9-f650-4e15-84b1-d0fe86c1ac07`）和其 Task“项目关联会话验收”（`72e41f76-de38-4f8f-954d-a41a24919370`）。首轮 GLM 返回“项目关联会话已接通。”；新增合成文档 `integration-smoke.md` 后，第二轮只给文件名，GLM 经 `read_project_document` 返回文档中的 `PROJECT-DOC-20261015`，工具摘要显示 `documents/integration-smoke.md`、已读取、执行成功。刷新页面后历史仍在。四轮执行完成后的只读数据库核查：v27、1 项项目、1 条无项目 Task、1 条项目 Task、4 条 Turn/Run，`foreign_key_check=0`、`quick_check=ok`。

**2026-10-15 原生入口与接口说明：**AgentComputer 的 macOS/iOS New Task 已接入“盒子任务”，可创建真正 `projectID=null` 的远端会话、打开历史并在同一 Task 内继续发送；原命令及收据支持响应丢失后的恢复。无项目定向 Swift 测试 3/3、macOS 与 iOS Simulator 构建通过；真实 8088 原生窗口尚未登录点验，离线会话快照未完成。8088 的 `/api/docs#tag/agentcomputer` 已补全 25 条接口的中文用途速查和中文操作标题，实际 OpenAPI 25/25 覆盖核对通过。

**2026-10-15 接口验证状态：**`/api/docs#tag/agentcomputer` 的 25 条接口现逐条标注验证层级。25 条均有本地 HTTP 自动化覆盖；其中 14 条还被当前 8088 已登录网页的项目、无项目 Task、多轮 GLM 和项目文档流程调用，另外 11 条没有逐项 8088 实测记录。暂停、审批、提问只是只读查询，取消能力未开放，不能把路由存在或夹具测试等同于完整用户能力。聚焦集成回归首轮 262 passed / 5 failed，5 项均为新增中文摘要和独立 Task 收据造成的旧 OpenAPI 断言；同步断言后复跑这 5 项及文档元数据单测为 6 passed。目标 Ubuntu、真实原生客户端直连 8088 和完整 `make all` 仍待验收。

**2026-10-15 Task 最近活动排序（开发树）：**无项目 `GET /api/agentcomputer/v1/tasks` 现按持久 `updatedAt`、Task UUID 倒序，让最近有活动的会话排在前面；项目内 Task 列表仍按 `createdAt`、Task UUID 倒序。Task view 的 `createdAt`/`updatedAt` 是可选 UTC 字段，本版从已有非空数据库时间戳填充；客户端应按可选字段解码。静态数据集上的同毫秒分页、跨用户/作用域游标与 HTTP 读取已覆盖，相关测试 27/27、Ruff、严格 mypy 与差异检查通过。8088 已使用原 `OCTOP_HOME` 短重启；实机 OpenAPI 的 TaskView 已包含两个时间字段，`/api/docs` 返回 200。认证后实际 Task 列表排序尚未单独点验，完整 `make all` 后置。分页期间 Task 更新前移不构成严格历史快照。

**下一步按依赖推进：**在已登录的 AgentComputer 原生实包中直连这个 8088，完成无项目会话双轮点验。随后补通用执行过程、审批、正式成果、第二设备及 Ubuntu 盒子部署。完整阶段退出标准没有因本机试用而降低。

当前实施分支是 `feature/agentcomputer-foundation`，交付目标为 [RMinte/Octop](https://github.com/RMinte/Octop)。每轮在主[集成记录](agentcomputer-integration.md)中注明基线、改动、验证与剩余门禁。
