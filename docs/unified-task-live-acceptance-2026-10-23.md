# 统一任务与会话：真实模型验收记录（2026-10-23）

本轮团队协作、高级工作台和专家记忆的真实 GLM-5 验收已通过，修复了记忆抽取问题和三处工作台问题。验收在独立的 macOS、SQLite、Chrome 环境完成，覆盖范围见下文；本轮未提交、未推送，也未执行完整 `make all`。

本报告记录上述真实验收当时的版本与环境。后续基线对齐到 `f7500b3` 并新增远程/IM/CLI/cron、resident 和自动批迁工作后，新增部分的实际状态见[外围入口与迁移状态表](unified-task-all-entrypoints-plan.md#当前实施与验收状态)；这里的 GLM 通过结论不自动覆盖这些新增入口。AgentComputer 本阶段只更新交接文档，未实施 Swift/UI 或设备验收；真实语音供应商也未列入已通过范围。

## 1. 版本与隔离环境

- 主目录分支为 `feature/agentcomputer-foundation`，基线提交为 `98d15120eaf1ce62a3121ea72faeb7b9b4a56fac`；隔离工作树对齐同一提交，并保留此前全部未提交改造。本报告对应该基线叠加当前未提交改造，不能只检出该提交就视为包含本轮功能。
- 模型为 `Zhipu Coding Plan (BigModel)/glm-5`。源 Provider 数据库由环境变量指定，通过 SQLite `mode=ro` 读取选定 Provider 与模型；不启动或修改用户原有服务，不向源数据库写配置、Task 或记忆。
- API 验收使用 `tests/live/unified_task_support.py` 和 `live_task_env`：每次创建独立临时 home、SQLite 数据库、测试用户、专家目录和项目目录。选定 Provider 配置只复制到临时环境，模型输出预算在该环境限制为 4096 token；普通 Task 关闭可关闭的思考模式。
- 模型请求实际发往已配置的 GLM 服务，使用真实 Provider 凭据并产生真实用量。凭据不写入本报告或安全证据；临时数据库、会话配置及原始日志不应作为公开验收附件。
- 团队和记忆均使用新建的合成内容与随机标记，不读取用户项目资料。记忆第二轮另建临时环境，没有重放第一轮结果未知的抽取请求。
- 收尾再次只读核对源数据库版本仍为 55；主目录 HEAD 仍为上述基线。已关闭临时 HTTP、UI/演示网页进程、录制 daemon 和验收专用 Chrome，未关闭日常浏览器或用户服务。

## 2. 真实与模拟的边界

| 验证 | 实际执行的部分 | 输入与边界 |
|---|---|---|
| 团队 live | 真实 HTTP Task、GLM-5、协调者委派、成员工具、项目文件写入与调用账本 | 专家、用户、项目文件和 nonce 均为临时合成数据；未使用 fake model |
| 记忆 live | 真实 Task 完成后采集、持久抽取 job、GLM-5 抽取、canonical atom、后续新 Task 召回、用量账本 | 项目事实与通用偏好均为合成用户原话；未人工预置最终 atom，未使用 fake model |
| 抽取诊断探针 | 库实际抽取提示词、真实 GLM-5、JSON 与候选解析 | 独立合成 raw，不执行或恢复原 uncertain job；只输出脱敏统计 |
| 工作台 | 真实 SPA、文件编辑与预览、PTY、Chrome 推流与输入、录制应用、GLM-5 工具调用与结果展示 | 使用临时项目及本地演示网页；不能代表所有外部网站或第三方插件覆盖 |
| 198 项定向回归 | 确定性单元与集成测试，覆盖权限、路径、团队、记忆、流式及国际化等 | 包含 fake model 或 mock 的无网络测试，不能替代 live 结果 |

## 3. 团队协作完整验收：通过

入口为 `tests/live/test_unified_task_team_live.py`。最终结果：**1 passed，50.27 秒**。

协调者先调用 `agent_list`，再通过 `ask_agent` 委派两名成员。成员分别真实调用 `read_file` 读取项目 `seed.txt`，调用 `write_file` 将随机 nonce 写入各自的 `member-a.txt`、`member-b.txt`。验收同时检查了：

- 两个文件实际位于项目目录，内容匹配 nonce；源专家工作目录未出现这些文件，源专家配置保持一致。
- 成员工具与文本带有对应专家身份；`team_member` 账本包含两名成员的成功调用，归属当前 Task 用户与项目。
- 其中一名成员是另一用户共享的专家，其私有 `USER.md` 与私有标记未进入本轮快照或输出。
- 其他用户读取 Task 返回 404；撤销成员共享后，再次发送消息返回 404，且未产生新的模型调用。

安全证据记录了 6 条成员模型调用，Task 轮次统计为输入 71749、输出 548 token；这些是该轮账本统计，不代表单个请求或整轮验收所有后台请求的费用。

首轮团队流程实际上已完成工具操作与文件写入，验收失败来自账本状态断言将 `succeeded` 写成 `completed`。修正测试断言后完整复验通过；这项断言修正不计为团队生产故障修复。

安全证据：`/private/tmp/octop-live-team-2/test_live_team_uses_both_membe0/live-octop/team-live-evidence.json`。

## 4. 专家记忆完整验收：修复后通过

入口为 `tests/live/test_unified_task_memory_live.py`。最终结果：**1 passed，45.81 秒**；临时库中 5 个 Task 轮次均为 `completed`，2 个抽取 job 均为 `completed/done`。

### 首轮失败与修复

首轮真实 Task 已完成，后台记忆模型请求在 30 秒边界超时。job 进入 `uncertain/extracting`，错误码为 `extraction_failed`。该状态表示无法可靠确认请求结果，未将其视为成功，也未自动重试或重放。

核对发现，普通 Task 已发送关闭思考的配置，后台抽取却直接使用模型默认配置。GLM-5 的已配置能力支持关闭思考。修复位于 `src/octop/infra/projects/expert_memory_extraction.py`：按实际解析后的抽取模型读取 reasoning capability，复用现有 `reasoning_request_parameters` 装配请求；支持关闭时关闭，不支持关闭时遵从能力约束。既有 provider 参数、30 秒上限、单次请求、用量记录和未知结果不重放语义保留。

修复后，使用记忆库实际提示词与合成 raw 的独立真实探针在 4.61 秒返回，`finish_reason=stop`，JSON 有效，解析出 1 个合法候选、0 条 warning。没有放宽超时来取得该结果。新增无网络测试覆盖可关闭思考、不可关闭思考及 `status_only` 模型，并检查原模型与能力配置未被修改。

### 全新环境中的完整链路

1. 在 Alpha 项目创建真实 Task，提供仅属于该项目的合成代号与口令；完成后自动采集并进入持久抽取队列。
2. 真实抽取生成 scope canonical atom，核对其 candidate、raw、原始引用及来源 Task/Turn。
3. 临时关闭后续采集，创建同项目的全新 Task。冻结的召回快照与真实模型答复均包含正确口令，证明召回来自已持久化记忆。
4. 在 Beta 项目创建新 Task。其召回快照、答复与 general 查询均不包含 Alpha 的口令。
5. 重新启用采集，通过 Alpha 的新 Task 提供一条明确适用于所有项目的用户偏好。真实抽取经 `explicit_universal_user_preference_v1` 规则晋升 general，保留原话及来源 namespace、Task/Turn、candidate 链。
6. 再次关闭采集，在 Beta 的新 Task 询问该偏好。冻结召回快照与真实答复均包含偏好标记，且没有 Alpha 项目口令。
7. 核对两条 `memory_extract` 调用均为 `succeeded`，用量汇总恰有两条记录，合计输入 8557、输出 740 token。该统计仅指两次记忆抽取，不含五个 Task 的全部调用。

首轮 uncertain job 留在首轮临时库，第二轮使用新 nonce、新 Task 和新临时库完成上述流程。最终 live 的 warning 类别为 `DeprecationWarning`。

安全证据：`/private/tmp/octop-live-memory-2/test_real_model_extraction_can0/memory-live-report.json`。

## 5. 工作台本轮已修复的问题

| 问题 | 修复 | 位置 |
|---|---|---|
| 文件右栏布局未提供有效高度，Monaco 与预览实际不可见 | 将右栏设为 flex 列并提供 600 像素高度，使编辑器与预览获得可用空间 | `dashboard/src/pages/BoxProjects/TaskFiles.tsx` |
| 真实 GLM 使用项目 host 绝对路径调用 `write_file`，工具返回成功，文件却被写到项目下的 `private/tmp/...` 子树 | 新增 `TaskFilePathMiddleware`，将属于本轮 host workspace 的绝对前缀转换为虚拟根路径；仅处理原生文件工具的 `file_path/path` 参数并拒绝 `..`。接入主 Task 与团队成员运行时，并补充本地化提示 | `src/octop/infra/projects/file_paths.py`、`runtime.py`、`expert_team_runtime.py` |
| Task 工作台显示了没有点击处理的桌面“打开浏览器窗口”按钮 | 在 Task 环境隐藏该无效入口 | `dashboard/src/components/BrowserWorkspace/index.tsx` |

以上为已完成的生产修复，最终真实页面、工具操作及截图验收结论见下一节。

## 6. 工作台最终验收：通过

验收入口为 `scripts/verify_task_workbench_live.py`，在独立 localhost 服务和临时浏览器 profile 中操作实际构建的 SPA。最终五组流程均取得真实执行证据：

| 流程 | 核对结果 |
|---|---|
| 文件编辑、上传与预览 | Monaco 保存后的文本与项目物理文件一致；PNG 的真实图片尺寸和 PDF 的实际 canvas 已检查 |
| PTY 终端 | 真实终端当前目录为项目目录；输入命令生成文件，断开后重连保留历史，页面明确结束会话成功 |
| Chrome 浏览器 | 接收实际 screencast frame，显示本地演示网页；Task 中无无效桌面按钮 |
| 录制与应用 | 通过浏览器画面输入并点击后，页面实际显示 `Saved: ui-browser-proof`；录制包含 4 个事件、3 个步骤，生成 1611 字符 Skill，明确应用并保存 |
| 下一轮真实模型 | 同一新 GLM-5 轮次完成 `write_file`、`read_file`、`browser_use`；随机标记文件落在项目根目录 `model-proof.txt`，未产生重复主机路径；该轮 `resources/skills.json` 的 Skill 内容与应用文件逐字节一致；页面实际渲染工具参数及成功的 DOM 结果 |

录制流程最初的验收断言固定要求名称 `acceptance-skill`，而实际 UI/SDK 返回名称为 `Octop acceptance playground`。这属于验收脚本错误；已按实际名称修正，并对已保存的录制、应用文件和同一模型轮次的资源快照作只读补验，结果通过。没有为补验重发模型请求，也没有把这项断言修正计为生产修复。

证据目录为 `/private/tmp/octop-workbench-live-hjh5zydx/evidence-ui/`。`final-report.json` 汇总五组通过结果、真实 Turn、文件落点、Skill 摘要和进程清理；`flow-report.json` 保留原流程及名称误断言，`report.json` 记录纠正名称后的录制、快照和结果展示验证。`actual-rendered-tool-result.png` 已人工查看，展示真实工具参数与成功 DOM 结果；加载中的旧截图不作为通过证据。

本地 HTML/PDF 是验收输入，PTY、Chrome、模型请求、录制和文件持久化均真实执行。本轮没有配置真实外部 MCP、自定义 renderer 或语音供应商，因此这些外部集成不列入 live 通过范围；已有确定性回归结果仍单独记录。

## 7. 本轮定向回归与静态检查

最终后端定向回归 **198 passed，28 warnings，14.66 秒**，覆盖文件如下。另有记忆抽取模块 33 项定向测试通过。

```sh
PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 \
UV_CACHE_DIR=/private/tmp/octop-unify-uv-cache \
uv run --no-sync python -m pytest -q \
  tests/unit/projects/test_file_paths.py \
  tests/unit/projects/test_expert_team_runtime.py \
  tests/unit/projects/test_expert_memory_extraction.py \
  tests/unit/projects/test_expert_memory_management.py \
  tests/unit/projects/test_expert_execution.py \
  tests/integration/test_project_task_public_text_stream.py \
  tests/integration/test_project_task_expert_runtime.py \
  tests/integration/test_project_task_expert_teams.py \
  tests/unit/i18n
```

五个生产源文件的 strict mypy 通过，Ruff 与 diff 检查通过。前端 `npm run build` 成功，包含 `tsc -b` 与 Vite 构建；Vite 构建耗时 10.54 秒。

```sh
PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 \
UV_CACHE_DIR=/private/tmp/octop-unify-uv-cache \
uv run --no-sync python -m mypy --strict --follow-imports=silent \
  src/octop/infra/projects/runtime.py \
  src/octop/infra/projects/file_paths.py \
  src/octop/infra/projects/expert_team_runtime.py \
  src/octop/infra/projects/expert_memory_extraction.py \
  src/octop/api/routers/project_tasks_ws.py

git diff --check
(cd dashboard && npm run build)
```

最终运行日志分别为 `/private/tmp/octop-live-final-regression.log`、`/private/tmp/octop-live-final-types.log`、`/private/tmp/octop-live-final-build.log`。这些结果针对本轮受影响范围，不等于完整门禁或所有 Provider 的兼容性验收。

## 8. 重复真实模型验收

在包含当前改造、依赖已准备的仓库中执行。源数据库路径使用本机明确选定的只读配置源；以下占位路径需替换，不能把临时验收目录指向用户正常 home。每次创建新的临时根目录，团队与记忆串行运行。

```sh
export OCTOP_LIVE_PROVIDER_DB='/absolute/path/to/source/octop.db'
export OCTOP_LIVE_MODEL_REF='Zhipu Coding Plan (BigModel)/glm-5'
export PYTHONPATH=src:.
export PYTHONDONTWRITEBYTECODE=1
export UV_CACHE_DIR=/private/tmp/octop-unify-uv-cache
OCTOP_LIVE_ACCEPTANCE_ROOT="$(mktemp -d /private/tmp/octop-unified-live.XXXXXX)"

uv run --no-sync python -m pytest \
  tests/live/test_unified_task_team_live.py -m live -q \
  --basetemp="$OCTOP_LIVE_ACCEPTANCE_ROOT/team" \
  > "$OCTOP_LIVE_ACCEPTANCE_ROOT/team.log" 2>&1

uv run --no-sync python -m pytest \
  tests/live/test_unified_task_memory_live.py -m live -q \
  --basetemp="$OCTOP_LIVE_ACCEPTANCE_ROOT/memory" \
  > "$OCTOP_LIVE_ACCEPTANCE_ROOT/memory.log" 2>&1
```

`live_task_env` 负责创建临时 Octop 环境，不连接正常用户服务。网络受限的执行环境需允许本次已授权的真实模型请求。不要公开原始全日志、临时数据库或含认证信息的会话 JSON；对外核对采用上述白名单证据。

工作台复验另需先准备独立的 localhost 服务与临时 session JSON，其 `root`、`project_directory` 必须位于同一可丢弃临时目录，并配置该服务的临时登录信息与 `model_ref`。当前脚本使用本机 Chrome 路径，因此应在具备对应 Chrome 的验收机器上运行：

```sh
OCTOP_LIVE_SESSION_JSON='/absolute/path/to/disposable/session.json'
uv run --no-sync python scripts/verify_task_workbench_live.py \
  "$OCTOP_LIVE_SESSION_JSON"
```

该命令用于重复本机验收，需自行准备独立临时服务；不要将真实用户服务或日常浏览器 profile 用作验收环境。

## 9. 扩展范围：远程统一 Task 的真实双实例验收

扩展范围基线为 `feature/agentcomputer-foundation @ f7500b3` 叠加当前未提交改造。入口为 `tests/live/test_unified_task_remote_live.py`，独立临时本机/peer 两个 OctopServer，peer 由 uvicorn 在临时 localhost 端口提供真实 HTTP/Bridge WebSocket。两个实例均未替换模型；选定 Provider 仍只从源库只读复制到临时环境。

首次完整流程 **1 passed，19.40 秒**；所有后续修正合并后，最终代码再用新隔离环境完整复验，**1 passed，22.78 秒**。本机通过 Bridge 的能力握手、Task WebSocket 和 HTTP 代理创建远端项目与任务，远端专家真实调用模型、读取合成 `seed.txt` 并写入项目 `proof.txt`，文件内容匹配随机标记。

| 核对项 | 实际结果 |
| --- | --- |
| 执行主机与账本 | peer 记录 4 条模型调用，本机 0 条；本机没有同 ID 可执行 Task |
| 工作目录 | 文件实际位于 peer 项目目录；专家源目录没有产生 `proof.txt` |
| 附件 | 通过代理上传、重写 peer 预览 URL 后鉴权读取，内容逐字节一致 |
| 保存与导出 | 断开观察后仍可从 peer 读取已完成轮次，完整导出包含回复 |
| 离线索引 | 明确断开 Bridge 后，本机仍能读取缓存的远端任务摘要 |
| 会话令牌 | 观察到的转发帧中 peer `session` 令牌帧为 0；不覆盖本机登录状态 |

最终安全证据：`/private/tmp/octop-remote-live-final4/test_live_remote_task_uses_pee0/live-octop/remote-live-evidence.json`；运行日志为 `/private/tmp/octop-remote-live-final4.log`。首次通过记录保留在 `/private/tmp/octop-remote-live3/`。前两次验收脚本使用过短的命令 ID，被真实协议拒绝，未创建 Task、未产生模型调用；修正脚本后完整通过。这不计为模型或远程执行故障，也未重放结果未知的请求。

独立审读后补充了 Bridge 双侧 Task 命令白名单，阻断共用原生流中的私人 resident 订阅；初始化失败会清理 stream 并关闭观察者。单元测试覆盖这两个边界、错误认证帧以及令牌/响应头过滤。Bridge 仍使用既有 32 MiB 消息上限，Task HTTP 每请求/响应限制为 16 MiB，超限返回 413，不断开其他复用流；这不是无限文件流能力。

本项覆盖同机双实例的真实网络、模型与文件链路；不宣称已经验证跨操作系统远端 Chrome/PTY、真实 IM 平台投递、断网重连、所有共享撤权场景、STT/TTS 或 APP 真机。迁移的真实数据副本演练和 IM/cron/resident 定向结果分别记录在[扩展状态表](unified-task-all-entrypoints-plan.md#当前实施与验收状态)。

最终后端增量整合 **263 passed，18 warnings，33.78 秒**，日志 `/private/tmp/octop-unified-final2.log`。覆盖迁移、入口/删除重放、外部 IM 私人记忆与本人审批、团队/自动抽取、频道/cron、远程流与 HTTP、会话四尾项及服务生命周期。此前 281 项整合集合、旧 gateway 465 项、来源/记忆/团队/cron/i18n 292 项和私人助手 73 项分别通过，集合有重叠，不加总。最新源文件的 strict mypy/Ruff 与 diff 检查通过；WebSocket Schema 87 个定义、106 处引用均能解析。未重复完整发布门禁。
