# AgentComputer 与 Octop 集成

本项目把 Octop 作为 AgentComputer 的盒子执行引擎。首轮已建立认证能力查询和稳定服务身份；第二轮继续建立持久数据层、内部原子接收与已有收据查询，详细规则见 [第二轮数据层说明](agentcomputer-core.md)。

第三轮实现项目意图、停止的默认专家和受限工作区准备，详见 [项目准备说明](agentcomputer-projects.md)。当前完整项目执行能力仍未开放。

第四轮建立内部 Task/thread/session 原子映射和本人只读 Task 查询，详见 [Task 映射说明](agentcomputer-tasks.md)。真实任务创建和执行仍等待后续门禁。

第五轮增加受执行能力门禁保护的 `POST /tasks/{taskID}/turns`、本人 `GET /runs/{runID}` 和原 Turn 收据的完整快照/事件绑定校验，详见 [Turn 登记与 Run 查询](agentcomputer-turns.md)。当轮复用 schema 22；真实新命令仍受关闭的执行门禁约束。

第六轮增加内部 claim/dispatching/应用入站收据和未知交接阻塞，详见 [内部派发与接收证据](agentcomputer-dispatch.md)。当轮配对 023 保存每个 Run 唯一派发 journal，尚未装配启动扫描。

第七轮增加 daemon runtime 前的保守恢复扫描、当前安装目录的生命周期互斥和既有公开 saver 的检查点关联元数据探测，详见 [保守启动恢复](agentcomputer-recovery.md)。当轮 adapterVersion=0.7.0、capabilityRevision=7，复用 schema23；embedded 默认跳过扫描，未新增公开 HTTP。互斥不证明旧0.6进程或模型已停止，应用 ACK/checkpoint 不原子，原生 History Turn 仍未绑定。

第八轮增加内部工具效果意图、交接标记和结果 Journal，详见 [工具效果边界](agentcomputer-effects.md)。当轮 adapterVersion=0.8.0、capabilityRevision=8，024 配对迁移保存单一工具效果表；同 Run 的未收敛工具互斥、failed/unknown 保留对账与双 lane。默认引擎工具入口继续拒绝，可信隔离 callback 不代替生产授权目录、实际文件 before/after 或成果发布。真实执行、用户取消和完整恢复继续关闭；下方第1–7轮历史保留当时范围。

第九轮增加内部持久审批、提问和精确 Run 取消，详见 [持久暂停与取消边界](agentcomputer-pauses-and-cancellation.md)。当轮 adapterVersion=0.9.0、capabilityRevision=9、schema25；未派发 Run 可原子停止，已交接 Run 只记录取消请求，暂停裁决只保留输入事实。真实引擎继续与专属停止证明未接线，公开审批、问题和取消写入口及对应能力仍关闭。

第十轮将精确 Run 取消接入认证 HTTP，并在 Run 当前视图中呈现持久取消请求，详见 [取消命令与状态查询](agentcomputer-cancellation-http.md)。当轮 adapterVersion=0.10.0、capabilityRevision=10，沿用 schema25；取消接口只按明确 Run ID 操作，已派发运行仍不能宣称已停止。生产 Run 创建和 Run 专属引擎停止证明未接通，客户端可用性继续关闭。

第十一轮扩展已有的命令收据查询，按原裁决命令 ID 只读回查审批或提问的持久收据，详见 [暂停裁决收据查询](agentcomputer-pause-receipts.md)。当轮 adapterVersion=0.11.0、capabilityRevision=11，沿用 schema25；历史收据不表示审批/提问写入或引擎续跑已经对客户端开放，生产执行能力仍关闭。

第十二轮开展 M0-07 的离线模型协议预检，详见 [Provider wire 预检](agentcomputer-provider-wire-preflight.md)。合成 Chat Completions 的 Provider → harness 工具续轮已有定向证据；旧 App 的 Responses 和目标 Qwen 服务仍未验证，Ubuntu 盒子的 M0 门禁继续阻塞，生产执行能力不因离线结果开放。

第十三轮增加本人 Run 的[暂停状态只读查询](agentcomputer-run-pause-read.md)。adapterVersion=0.12.0、capabilityRevision=12，沿用 schema25；它回查持久 pending、decided 或 expired 元数据，不公开原始动作或问题内容，也不提交裁决、恢复引擎或放行生产能力。

第十四轮增加按明确审批或提问 UUID 的[暂停身份只读查询](agentcomputer-pause-identity-read.md)。adapterVersion=0.13.0、capabilityRevision=13，沿用 schema25；未来客户端可用已知 ID 在深链或断线后核对本人暂停的当前持久状态，但查询只返回脱敏元数据。公开裁决写入、安全动作或问题展示、引擎续跑及生产执行仍未开放。

第十五轮按用户选择复用现有 GLM，完成[本地真实 Provider → harness 工具续轮预检](agentcomputer-glm-live-preflight.md)：两次 GLM-5 Chat Completions 请求完成合成 `ping → pong` 和最终答复。它是 M0-07 的本地子门禁；目标盒子 Qwen、生产托管 Run 与执行隔离未因此放行。adapterVersion、capabilityRevision 和 schema 均不变。

第十六轮完成[本地 GLM 流式与取消边界预检](agentcomputer-glm-stream-cancel-preflight.md)：真实 GLM-5 单请求 SSE 文本流自然结束；另用离线阻塞流夹具验证按 thread 取消本地消费后没有执行未完成的工具调用。远端生成停止、生产 Run 精确停止和盒子 Qwen 仍无证据。adapterVersion、capabilityRevision 和 schema 均不变。

第十七轮增加[精确 Run 的本地取消信号](agentcomputer-run-cancel-signal.md)：持久取消请求提交后，服务器只对身份匹配的活动 Run task 发送进程内取消信号；取消传播时，现有派发 fence 将已交接 Run 保守收紧为 `interrupted/reconciliationRequired` 并保留双活动占用。没有生产 worker、远端模型或子进程停止证明，不能将已交接 Run 宣称为 `stopped`。adapterVersion、capabilityRevision 和 schema 均不变。

当前只能在开发机本地部署。用户提供的目标设备描述是“可能为 Ubuntu、约 16 GB 内存的普通电脑，并有可部署 Qwen 27B 的独立算力”；系统、CPU 架构、容器条件、网络与实际可用资源尚未核实。它们是待确认输入，不是已验收的部署规格。

## 仓库与版本依据

| 项目 | 位置或来源 | 依据 |
|---|---|---|
| 交付 fork | [RMinte/Octop](https://github.com/RMinte/Octop) | 用户指定的后续交付仓库 |
| 上游 | [TencentCloud/Octop](https://github.com/TencentCloud/Octop) | 保留上游来源，后续升级先验证兼容性 |
| 首轮 Octop 主目录基线 | `main` | `0c5a46ab5f82e5ad9d1a56fa09b00e542f042fda` |
| AgentComputer 主目录基线 | `feature/0.0.2/all` | `81e7acb653865f5c31342e844f2e90a36116caa9` |
| 实施分支与提交 | `feature/agentcomputer-foundation`，逐轮沿主目录最新本地提交开发；交付提交见 Git 历史 | 提交前重新核对主目录最新 HEAD |
| 第五轮开工基线 | `feature/agentcomputer-foundation` | `bdff37f31340e9ceecf18aac7f848d6e51c93647`，开工时主目录干净 |
| 第六轮开工基线 | `feature/agentcomputer-foundation` | `ce3f0b5830cf79c497fb98f31973c581034d8111`，开工时主目录干净 |
| 第七轮开工基线 | `feature/agentcomputer-foundation` | `ca904ae0f629fbbf49c1747b365a1277eb5a18bf`，开工时主目录干净 |
| 第八轮开工基线 | `feature/agentcomputer-foundation` | `ab0f0391eaa5f88d236b646862832efa63f71b23`，开工时主目录干净 |
| 第九轮开工基线 | `feature/agentcomputer-foundation` | `37bcca34f1d12063ab844d64995b1eea679913ff`，开工时主目录干净 |
| 第十轮开工基线 | `feature/agentcomputer-foundation` | `5247cd2fb78e588eba827086f1183353a114e374`，开工时主目录干净 |
| 第十一轮开工基线 | `feature/agentcomputer-foundation` | `2075257b8ecd13d363813f346cb1051ed52a1d6d`，开工时主目录干净 |
| 第十二轮开工基线 | `feature/agentcomputer-foundation` | `161b06201ec3a5693c18935236d517a8ba1fa7e8`，开工时主目录干净 |
| 第十三轮开工基线 | `feature/agentcomputer-foundation` | `2c1fab9a6db561bb9b0afb76c17d4b03cb03c011`，开工时主目录干净 |
| 第十四轮开工基线 | `feature/agentcomputer-foundation` | `2962db3f57a1d80b1f4b00780160dd25c55e4659`，开工时主目录干净 |
| 第十五轮开工基线 | `feature/agentcomputer-foundation` | `a225e52b91928d9a837633fb46c250cc21964c43`，开工时主目录干净 |
| 第十六轮开工基线 | `feature/agentcomputer-foundation` | `d97c03ca33f85c8cc105c48a318f907861ad57ec`，开工时主目录干净 |
| 第十七轮开工基线 | `feature/agentcomputer-foundation` | `64472405bb8205ba37124b05d8be5f0890288282`，开工时主目录干净 |

Git remote 的实际名称、推送分支、提交和 PR 由交付记录填写；仓库网址不等于已经推送。工作开始核对主目录 status、worktree、branch 和 HEAD，保留已有文档及代码改动。提交运行仓库 hooks，完成默认 `make all` 门禁后再交付；不跳过红测试。

本文是 fork 内的集成入口文档；API 与运行说明见 [API](api.md)、[配置](configuration.md)、[CLI](cli.md)。AgentComputer 客户端的详细规划仍保存在其 `SPEC/029-octop-box-integration.md` 及步骤附录，本轮不把客户端代码移入 Octop。

## 阶段安排

| 阶段 | 交付内容 | 当前边界 |
|---|---|---|
| M0 环境和协议验证 | 部署输入、模型协议、隔离与容量、最小身份及能力入口 | 离线 wire、真实 GLM-5 工具续轮与文本流、离线本地取消边界已有分项证据；目标盒子 Qwen、执行隔离和实机条件仍待验 |
| M1 领域和命令接收 | 项目、Task/thread、Run、固定输入、命令幂等、最小派发和恢复 | 已有 schema 25、本地项目准备、内部 Task/Turn 登记、内部派发/工具 journal、持久暂停及本人 Run/审批/提问状态与原裁决收据回查、精确取消及匹配活动 Run 的本地 task 信号、应用入站收据、daemon 保守扫描与 Run 查询；生产 ready、真实模型/工具和完整恢复仍待验 |
| M2 原生闭环 | 客户端远程会话、事件、审批、取消、正式成果及重连 | 不以实时连接成功代替可靠执行 |
| M3 配置与恢复 | 指令/技能版本、资源、Outbox、多设备与异常对账 | 保留配置和执行快照，不盲重放 |
| M4 调度与面板 | Schedule occurrence、Git/终端、受认证预览及资料库 | 工具隔离和项目写入规则先通过 |
| M5 迁移与发布 | 显式导入、完整备份恢复、TLS/配对、容量、升级和回滚 | 生产能力逐项实机验收后放行 |

阶段不代表固定工期。本地 GLM 子项通过不能宣称目标盒子模型工具续轮、Linux Shell 沙箱、真实盒子恢复或生产配对已经完成。

## 第一轮具体范围

以下为首轮历史交付范围；第二轮能力和表变化见文末记录。

新增 `GET /api/agentcomputer/v1/capabilities`，复用 Octop 当前登录用户认证。只有认证及系统初始化条件满足后才能查询，未认证请求不得取得服务身份信息。

本轮返回的是“这个 Octop 服务的稳定身份及当前未开放的业务能力”。创建项目、执行 Task、审批、成果、文件、Shell、终端、Cron 和设备配对均不由该接口实现，也不创建项目/Task/Run 业务表。

| 内容 | 本轮约定 |
|---|---|
| `boxID` | 稳定 UUID 服务身份；同一控制目录重启、改端口和重建服务后不变 |
| 身份存储 | 独立控制目录，位于专家工作区和可替换业务数据库之外；`agentcomputer-control/identity.json`，相对于 OCTOP_HOME |
| 身份故障 | 已有身份文件损坏或读取失败时明确拒绝，不能静默生成新身份 |
| `deviceID` | `null`；当前没有可信的配对设备上下文 |
| `authenticationMode` | `octopUserSession`；登录账号身份不冒充生产设备配对 |
| 业务能力 | 全部 `unavailable`，并提供明确原因；不根据原 Octop 工具是否安装推断 AgentComputer 协议已实现 |
| `limits` | 尚未测量的额度返回 `null`/unknown；不填写假定数字，不解释为无限制 |
| 版本及时间 | 返回实际可确认的协议、服务版本、服务 UTC 时间和配置时区 |

独立控制目录仍需要纳入后续完整备份。稳定 boxID 不等于硬件证明、设备认证或任意复制后可以同时上线的身份；正式设备绑定、恢复代次和灾备交接属于后续契约与验收。

首轮实现落点遵循现有层次：HTTP 路由做认证和序列化，稳定身份与能力状态属于 `infra`，通过服务装配访问。`infra` 不反向依赖 `api`，不把运行逻辑塞入路由，不增加另一套模型执行循环。

## 第一轮验收记录

首轮本地确定性验证与实际 HTTP 结果如下；完整工程门禁另行记录。跳过和未运行均不计为通过。

| 验收项 | 预期结果 | 实际证据 |
|---|---|---|
| 未认证查询 | 拒绝请求，不返回 boxID 或能力载荷 | 已验证 401；URL token 同样拒绝 |
| 已认证查询 | 类型化响应，authenticationMode 为 octopUserSession | 实际独立实例返回 200 |
| deviceID 与业务能力 | deviceID 为 null，全部业务能力 unavailable | 已验证 deviceID=null，18 项业务能力均未实现 |
| 未测限额 | null/unknown，不承诺无限额度 | 10 个限额均为 null，权限模式为空 |
| 重复读取和进程重启 | 同一控制目录保留同一 boxID | 单测及真实进程重启后 identity 指纹一致 |
| 不同独立数据目录 | 两个实例身份不同 | 身份单测通过 |
| 业务数据库更换 | 控制身份不因业务库替换而重新生成 | 独立身份单测通过；完整恢复保护未实现 |
| 身份文件损坏 | 明确失败，不重置为新 boxID | 503 identityInvalid，原内容保持；并发及 FIFO 单测通过 |
| API 文档 | `/api/docs` 的 summary、认证说明与响应 schema 可读 | Scalar 实际卡片可读，typed schema/错误/仅 header 认证已检查 |
| 本地部署 | 8090 使用新隔离数据目录，既有 8088 仍可使用 | 8090 登录、健康、重启通过；8088 健康仍返回 200 |
| 工程门禁 | 定向检查及 `make all` 结果分别记录 | 105 passed / 6 Linux 专用 skipped 的定向集合；完整门禁见交付状态 |

定向测试重点是身份持久化、认证边界、类型化响应和损坏行为；全部使用临时目录、Fixture 用户和内存/隔离数据库。测试不读取真实用户凭据、不连接模型、不迁移 Mac 数据。

## 本地隔离启动

仅使用回环地址 `127.0.0.1:8090`。不要复用默认 `~/.octop`，也不要改变用户已经使用的 `127.0.0.1:8088` 服务。

当前开发机已在此 checkout 使用独立 `.runtime/agentcomputer` 数据目录完成初始化、登录与重启验证。新克隆应先按 [CLI 初始化](cli.md) 在自己的独立目录创建管理员，后续重启继续使用同一目录；不要使用 --force 覆盖已有资料。启动模板：

```sh
OCTOP_HOME="$PWD/.runtime/agentcomputer" \
OCTOP_ENABLE_API_DOCS=1 \
OCTOP_HISTORY_V2_ENABLED=false \
.venv/bin/octop run --host 127.0.0.1 --port 8090 --workers 1
```

打开 [本地实验控制台](http://127.0.0.1:8090) 或 [协议文档](http://127.0.0.1:8090/api/docs#tag/agentcomputer)。本机专用账号为 local-admin，生成的密码仅在忽略目录 `.runtime/agentcomputer/local-credentials.json` 保存；不提交或写入普通日志。登录后查询能力接口。不得在该实验导入原 `~/.octop`、真实项目或云厂商秘密，不点击模型探测或启动模型任务。

如 8090 已被占用，先核对监听进程；不终止不明进程，也不自动回退到 8088。启动命令的 host/port 会写入所选数据目录配置，必须核对 `OCTOP_HOME`。日志和本机人工验证结果在交付时另行填写。

该回环 HTTP 方式只用于当前开发机。生产客户端仍需受认证 HTTPS、可信证书和设备绑定，不能照搬为 LAN 或公网部署方式。

## 模型协议前置与限制

AgentComputer 当前 RM-01 开发适配具有可复用的协议依据：

- 历史模型 ID 为 `RM-01 VLM`，开发服务地址仍保留在客户端配置；联调使用经确认的入口，该模型接口不承担设备业务协议。
- App 使用 Responses、原始 output item 重放、`store=false`，不发送 Authorization、`previous_response_id` 和厂商 reasoning 配置，发送 `chat_template_kwargs.enable_thinking=false`。
- 历史记录含合成 `box_echo → BOX_OK` 工具续轮；旧成功不证明服务当前在线或 Octop 工具循环已经兼容。
- “独立算力能部署 Qwen 27B”是用户提供的目标条件说明；不能据此将它与 RM-01 模型 ID、协议或权重版本视为同一服务。

Octop 的 `ProviderStore` 会筛除空 API Key，当前锁定的 `octop-harness 1.0.1` 的 `ProviderConfig` 也要求非空 Key。仅去掉 Octop 一处筛选不能实现免凭据访问。

Harness 普通 OpenAI 提供方默认采用 Chat Completions；已有 Swift Responses 实现不代表 Python 工厂同样工作。不得虚假声明 hosted tool search 来切换协议，也不使用未说明的占位 Key 掩盖认证差异。

第十二轮的[离线 Provider wire 与工具续轮预检](agentcomputer-provider-wire-preflight.md)已通过一项定向用例：临时 Provider 经实际 harness 对合成 Chat Completions 服务完成工具调用与结果续轮、SSE 文本终态和 HTTP 401 错误传递，使用的 Key 仅在测试夹具中。它没有验证 Responses、免凭据、真实服务的流式/错误行为、取消或 Qwen。后续依据目标服务确认免凭据适配或盒子内部认证入口；真实模型探针需明确地址、模型 ID、鉴权、请求预算和合成数据，不把合成响应写成盒子已配对。

第十五轮已按用户决定使用现有 GLM 进行[本地真实模型预检](agentcomputer-glm-live-preflight.md)。Octop 只读复用 8088 已配置的智谱 Coding Plan Provider，在临时 harness 工作区中完成 GLM-5 的合成工具调用、同 ID 结果续轮和最终答复；8090 实验实例不导入 Key 或更改项目状态。AgentComputer 自身当前本机设置仍选中历史 box 模型；它的 GLM Chat Completions 实现与 Octop 路径协议相近，但本次没有运行 Swift 客户端端到端任务。目标 Qwen 的路由、鉴权、协议、流式与取消仍在盒子上单独确认。

第十六轮沿用同一 Provider 完成[真实 GLM 文本流与离线取消边界预检](agentcomputer-glm-stream-cancel-preflight.md)：一次 `stream=true` 请求收到 SSE、非空 token 和最终标记；另以阻塞的合成 SSE 验证 `agent.cancel(thread_id)` 截断本地消费、关闭流且不执行未完成工具。真实请求没有取消，离线夹具没有接触远端 GLM，二者均不能证明服务端停止生成或生产 Run 已停止。目标 Qwen 仍待盒子实测。

第十七轮在[认证取消命令](agentcomputer-run-cancel-signal.md)持久提交后尝试通知同服务器内精确绑定的活动 Run task。隔离 HTTP 夹具核对“先落库、后发信号”和取消传播后的不确定状态；没有调用普通 thread 取消，也没有把 GLM 流式探针当作生产 Run。目标盒子模型取消及远端生成停止仍需实测。

## 仍未验证的实机条件

| 条件 | 当前已知 | 需取得的证据 |
|---|---|---|
| 操作系统与架构 | 可能 Ubuntu，CPU 架构未知 | 实机版本、架构、运行时 |
| 内存与存储 | 用户估计约 16 GB，可用余量未知 | 现有占用、可用量、文件系统和备份目标 |
| 独立推理 | 可部署 Qwen 27B，部署协议未知 | 模型版本、内部路由、认证、工具调用及取消 |
| Docker/执行隔离 | 未确认 | daemon 条件、UID/挂载/网络/限额、越权负向验收 |
| 网络与安全入口 | 未确认 | LAN 访问、TLS、证书、设备确认与撤销 |
| 故障恢复 | 未执行 | 进程重启、实机断电、完整备份恢复和容量竞争 |

现有 Dockerfile 默认 root，Compose 直接映射端口；Linux 启动还可能后台尝试安装 bwrap。首版 Shell 保持不可用，安装包存在或配置了项目目录都不是隔离验收证据。参考 [工作区后端](agent-backend-file-io.md)、[单进程模型](adr/001-single-process-model.md)。

默认系统备份不含聊天，并排除 `.git` 等目录；`history_v2` 目前没有跨机器身份重绑定和多库自动协调恢复工具。因此本轮不开展迁移、清理、升级或恢复。参考 [版本化历史](versioned-history.md)。

## 后续 M1 领域与派发

公共 ID、owner 归属、项目准备、内部 Task/thread、原子 Turn/Run 登记、命令幂等和固定输入已有本地实现与失败 Fixture。第七轮增加保守启动扫描，第八轮补充工具效果 Journal，第九至十一轮增加内部暂停、精确 Run 取消和裁决收据回查，第十七轮再接入同服务器活动 Run task 的精确本地信号。认证 `POST /runs/{runID}/cancel` 已能持久接收本人明确目标的取消请求；信号不等于停止证明，已交接 Run 仍保持保守对账。受控 Turn 写入保持执行门禁，真实模型/工具运行和暂停后的续跑尚未开放。

后续核实真实项目 ready、原生 harness 历史/幂等及引擎开始/停止证据，再装配生产 worker，完成审批/问答触发与安全续跑、已交接 Run 的专属停止和完整恢复。当前应用收据不替代这些证明；托管专家的 Dashboard、IM、Cron 和直接工作区写入口继续受保护，不能把普通会话入口作为执行门禁的替代路径。

M1 的本地 Fixture 可以继续迭代；真正派发模型与工具前仍需 M0 模型续轮及执行隔离门槛通过。生产配对、资料迁移、Schedule 切换和正式发布保留各自放行条件。

## 第一轮交付状态（历史）

- 实施分支：`feature/agentcomputer-foundation`，基线 `0c5a46a`；当前迭代的交付版本以该分支 Git 提交为准。
- 实际改动：`infra/agentcomputer` 身份/能力领域、PathLayout 控制路径、版本化薄路由、scoped 认证与错误、OpenAPI、离线单测/集成测试及文档导航。
- 验证：定向集合 105 passed / 6 Linux 专用 skipped；`make all` 的 Ruff、mypy 和非 live 全套通过，4342 passed / 23 skipped。跳过不计为通过；Linux/Windows 实机执行尚未完成。
- 完整门禁命令：`make all RUN='uv run --offline --no-sync' PYTEST_JOBS=4`，使用当前已经安装的锁定环境；本机日志 `/private/tmp/octop-iteration-01-make-all-final.log`。首次沙盒拒绝本地端口，正常权限复验后补齐测试替身及平台标记，再取得通过结果。
- 本地 8090：已完成初始化、登录、认证/未认证检查、header-only 文档、进程重启及稳定身份核对；当前平台 Darwin/arm64，未将其当作 Ubuntu 实机结果。没有配置 Provider 或启动 Agent。
- fork：`origin` 指向 RMinte/Octop，TencentCloud 来源保留为 `upstream`；功能分支推送状态在交付时核对，主分支不直接更新。
- 真实模型、盒子部署、数据迁移及生产发布：未执行。

离线门禁复核补齐了既有 Bridge DNS 和 ONNX/Knowledge 依赖替身；FnOS 进程生命周期用例依赖 Linux /proc，改为仅在 Linux 执行。macOS 跳过明确披露，Linux CI 仍执行这些测试。测试修正不改变业务行为。

## 第二轮交付状态

- 基线：主目录当前 `feature/agentcomputer-foundation @ a6015dd`，保留首轮提交；仍推送同一用户 fork 功能分支。
- 数据层：成对 021 迁移、六张控制表、RepoBundle/SharedServices 装配及同连接 repo；原子内核固定配置/请求快照、登记 Turn/Run、取得 Task/project 许可、追加事件并保存收据。
- API：新增本人 `GET /api/agentcomputer/v1/commands/{commandID}`；adapter 0.2.0、能力 revision 2，仅 commandReceipts 查询可用。HTTP 写入与模型/工具派发仍未接入。
- 工程验证：最终 `make all RUN='uv run --offline --no-sync' PYTEST_JOBS=4` 的 Ruff、mypy 与全套非 live 测试通过，4432 passed / 23 skipped；必需提交 hook 另运行 testmon 与 Dashboard 构建。首次全套发现 9 个旧断言仍期望 schema20，已按语义改为 21，保留原低 watermark 和未来备份版本输入。
- 关键评审修复：持久收据必须显式包含全部字段，缺失字段不使用模型默认值补造事实；重放还校验消息与 epoch 绑定。故障/竞争 Fixture 验证一次提交全部登记，响应丢失可查原收据；真实 PG 并发尚未测试。
- 本地升级：停止并核对自有 8090 进程，完整备份忽略目录后启动新代码；schema20→21、原用户数量及 boxID 保持，六表为空、Agent/Provider 为 0。实际认证 capabilities 200、缺失收据 404、非法 ID 422、匿名/URL token 401，8088 健康仍 200。Scalar 的收据卡片、UUID、类型和示例已实际检查；200 收据由隔离 HTTP Fixture 验证，实验实例不人为填入已接受任务。
- 本机证据：`/private/tmp/octop-iteration-02-make-all-final.log`、`/private/tmp/octop-local-02-evidence.json`；升级前完整目录位于忽略的 `.runtime/backups/iteration-02-before-schema-21-20261012T081646Z`。备份包含控制身份和本地凭据，保留原访问权限，不提交到 Git。
- 完整 M1、生产设备绑定、模型工具续轮、恢复 fencing、Ubuntu/Linux/Windows 实机和 PostgreSQL 实测：尚未通过。

## 第三轮交付状态

- 基线：主目录 feature/agentcomputer-foundation @ d77b37f，继续同一 fork 功能分支。
- 已实现：022 准备日志和托管标记、POST /projects 与 GET /projects/{projectID}、原 Project 收据、停止的默认专家及受限 BackendWorkspace；同键重试核实已有资源，创建/marker 写入确认丢失可续做。
- 入口保护：托管专家的原启动/执行/配置/workspace/CLI/Cron/Channel/proactive 入口关闭，普通专家行为回归验证。Provider 缺失为 failed/providerUnavailable，不启动模型、harness 或后台 bootstrap。
- 协议：adapter 0.3.0、能力 revision 3，projects supported/ unavailable，完整 Task/Run 仍不可用；已实施实际控制命令上限 65536 字节，其他执行限额未测。
- 工程验证：最终 make all 的 Ruff、mypy 和全套非 live 测试通过，4573 passed / 23 skipped。首次全量发现旧 HITL 单元夹具缺普通专家记录、users-only 旧库缺基础表，两处修复后复验通过；迁移按 canonical 001 事务补缺表，保留已有用户、配置和水位后继续正常升级。必需提交 hook/testmon 与 Dashboard 构建随提交执行，交付仍使用用户 fork 的 feature/agentcomputer-foundation。
- 关键复核：收据缺字段不能补默认事实，project/workspace/preparation 身份绑定均核对；PG 接收事务重放复用原连接。OS 锁不依赖 TTL，重复取消请求保持对资源准备的等待责任；旧入口的托管检查不因 enabled/state 改变被绕过。
- 本地实操：核对并停止自有 8090 进程，完整备份后升级 schema21→22，原用户和 boxID 保持。合成项目 POST 201；原收据持续 pending，GET 为 failed/providerUnavailable，agent/workspace 步骤 verified。实际重启后同命令返回相同资源，只有一个 disabled/stopped 托管专家、一个项目/journal/命令；Provider、Task、Turn、Run、事件均为 0。旧启动入口 403、超限 413、匿名/URL token 401，Scalar 项目与收据卡片及类型已检查。8088 健康仍 200，没有模型请求。
- 本机证据：/private/tmp/octop-iteration-03-make-all-final.log、/private/tmp/octop-local-03-evidence.json、/private/tmp/octop-local-03-evidence-restart.json；完整备份位于忽略的 .runtime/backups/iteration-03-before-schema-22-20261012T114129Z。凭据、控制身份和运行数据不提交到 Git。
- 阶段边界：M1-03 的本地资源准备部分已验证，ready 放行和托管执行入口尚未实现。M1-04 Task/thread、M1-06 派发、真实模型与执行隔离、设备配对和恢复 fencing 继续按步骤推进；PostgreSQL 只有静态核对，Ubuntu/Linux/Windows 实机未验，skipped 不计通过。

## 第四轮交付状态

- 基线：主目录 feature/agentcomputer-foundation @ ab454c54，继续同一 fork 功能分支。
- 已实现：schema22 的同 conn Task 预留/绑定、严格 thread/session/历史投影写入及 TaskCreateReceipt，一次事务提交；无新增迁移，资源不随 Dashboard 当前线程切换。同命令重试、竞争和各写入点回滚均有确定性 Fixture。
- API：新增本人 GET /tasks/{taskID}，commands 查询兼容三种原始收据；adapter0.4.0/revision4，tasks supported/unavailable，项目 ready、Task/Turn 创建 HTTP 和引擎执行仍未开放。
- 入口保护：CLI/GlobalProcessor 在会话副作用前核对专家与路由；ThreadRegistry 保护保留 key/channel 和实际 Task 关联，普通专家不能用任意 session key/thread hint 重绑或改变 Task 线程。
- 工程验证：最终 make all 的 Ruff、mypy 和全套非 live 测试通过，4671 passed / 23 skipped。初次多进程收集发现两个新测试参数使用随机 UUID，改为固定值后复验通过；必需提交 hook/testmon 与 Dashboard 构建随提交执行，交付仍为用户 fork 功能分支。
- 关键复核：Task/thread/session/投影/收据全程同 conn；预留和任意后续失败整事务回滚，原收据保留 ready 映射事实与 executionReady=false。原始 session 缺字段、坏 JSON 或关联错配拒绝，不用兼容回填补造。当前 Task revision/状态不覆盖原 ACK。
- 本地更新：核对并停止自有 8090 进程，完整备份后启动 0.4 适配；schema22 保持，原用户、停止专家、失败项目与收据字节、boxID 保持。真实数据库仍 0 Task/thread/session/Turn/Run/event；缺失 Task 404、非法 ID422、匿名/URLtoken401，Task 写路由不存在。Scalar Task 卡片、UUID、typed view 和不执行说明已检查；200 Task 由隔离 HTTP Fixture 验证。8088 健康仍 200，没有模型请求。
- 本机证据：/private/tmp/octop-iteration-04-make-all-final.log、/private/tmp/octop-local-04-evidence.json；完整备份位于忽略的 .runtime/backups/iteration-04-before-adapter-04-20261012T122715Z，凭据和运行数据不推送。
- 阶段边界：M1-04 的内部映射与只读查询部分已验证。真实项目 ready、受控 Task 创建/列表/状态管理、M1-05 入站/固定配置、M1-06 托管引擎派发和完整 M0/M1 仍待后续；PG 只有 SQL/约束静态核对，盒子和 Windows/Linux 实机未验，skipped 不计通过。

## 第五轮交付状态

- 基线：主目录 `feature/agentcomputer-foundation @ bdff37f`，继续同一用户 fork 功能分支；复用 schema 22，无新增 023。
- 已实现：受执行能力门禁保护的 `POST /api/agentcomputer/v1/tasks/{taskID}/turns`、本人 `GET /api/agentcomputer/v1/runs/{runID}`，以及同连接共享的原子登记和原 Turn 收据图校验。输入仅接受 `newMessage`；Task HTTP 创建继续不存在。
- 真实入口边界：新命令针对有效本人 Task 时，当前返回 `503 runtimeUnavailable/executionUnavailable`，不登记 Turn/Run、不取得执行许可、不派发模型；未知或跨 owner Task 统一 404。关闭门禁没有请求、配置或环境变量绕过入口。隔离测试只对可信能力函数打补丁，使用实际准备的托管 Task 验证 201/queued/notDispatched；这些 Fixture 不修改真实原型。
- 重放和历史：已接收的同键同体命令在能力关闭后仍返回 201 原收据；先校验当前恢复代次、本人托管映射、命令身份和完整原接收图。原收据 queued、消息/Turn/Run 身份、时间、sequence 和字节保持；Run GET 读取当前 state/dispatch/epochs/revision/reconciliationRequired 和实际 UTC 时间，不把原 queued 当作当前运行状态。
- 持久事实：严格原请求/配置快照与摘要、Turn/Run/Task/Project 归属、命令 payload digest、唯一 run.accepted 的 sequence/payload 均核对。缺失、重复或损坏拒绝，不补造事件、快照或输入；本轮未实现公开事件流、审批、问答、取消或 worker/engine dispatch。
- 协议：adapterVersion=0.5.0、capabilityRevision=5；projects/tasks/runs 为 supported=true、available=false、executionUnavailable，commandReceipts 查询可用；权限模式仍为空。实际控制正文限额为 65536 UTF-8 字节，其他执行容量未测，deviceID 仍为 null。
- 工程验证：最终 make all（Ruff、strict mypy、全套非 live 测试）通过，4757 passed / 23 skipped；skipped 不计通过。实际交付必须通过仓库提交 hook/testmon 与 Dashboard 构建，结果保存在本机提交日志。真实模型、live PG 和 Ubuntu/Linux/Windows 实机及完整 M0/M1 尚未验收。
- 本地 8090 更新与证据：自有 8090 经 PID/命令/工作目录核对、完整备份后升级 adapter0.5/revision5；schema22、原用户、stopped 托管专家、failed/providerUnavailable 项目、boxID 与原 Project 收据字节保持。真实库仍 0 Task/thread/session/Turn/Run/event；新增有效格式指向未知 Task 的 POST404、未知 Run404、非法正文/ID422、匿名/URL token401、实际超限413。有效本人 Task 的关闭门禁503及可信登记201由隔离 HTTP Fixture 验证，未在原型写入 ready 或 Task。typed OpenAPI 与 Scalar Turn/Run 卡片可读，8088 健康200，无模型请求。升级前完整备份位于 /Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-05-before-adapter-05-20261012T141944Z；运行数据与凭据均在忽略目录。
- 本地原型边界：原失败项目保持 providerUnavailable，真实 Task/Turn/Run 数量为 0；没有为证明接口成功而把项目置 ready 或填入已接收命令。用户原 8088 服务继续作为独立实例核对；没有真实模型请求。
- 下一步：先完成真实 ready 与托管引擎入站的独立门禁，再推进派发、取消、审批/问答和恢复；本轮不能代表完整 M1-05/M1-06、生产设备配对、PostgreSQL 实测或真实盒子验收已经通过。


## 第六轮交付状态

- 基线：主目录 `feature/agentcomputer-foundation @ ce3f0b5830cf79c497fb98f31973c581034d8111`，继续同一用户 fork 功能分支；023 配对迁移保存每 Run 唯一派发 journal，原数据和原收据格式保持。
- 已实现：仅内部 `claim_run`、`dispatch_run`、`get_claim`、`accept_engine_entry` 与 `interrupt_uncertain_run`；已有 Run GET 读取当前 phase/epoch/revision。没有生产 worker、启动调度、HTTP claim/dispatch/recover、运行开关或客户端 Swift/UI 变更；生产 claim 能力关闭时写入前拒绝。
- 接收证据：固定 Run/Turn/快照、主体、双 epoch 与意图摘要；dispatching 先提交再调用内部 callback。EngineEntryReceipt 是应用控制层的入站 ACK，只证明接收，不能当作模型 started/completed 或原生 harness exactly-once。原生 History Turn 未创建或预留，未来仍需明确历史与执行身份衔接。
- 未知交接：dispatching 后异常或确认丢失由派发器收紧为 interrupted/reconciliationRequired，保留 phase 和两级 lane；后台事务取消等待最终结果，晚到应用确认按 owner→Run 锁顺序核对当前版本，已有终态保留。重启后的人工核实可使用内部收紧函数，本轮没有启动扫描或自动恢复。不会自动 queued、reclaim 或按 TTL 释放。原 CommandReceipt 的 queued、消息/Run/Turn/时间/sequence 和 JSON 字节保持。
- 离线接线：公开 harness beforeAgent/beforeModel 与工具拒绝边界使用纯 Fake Memory model 和合成输入核对；这不构成真实 Provider、模型工具续轮、执行隔离或原生引擎恢复验收。
- 协议：adapterVersion=0.6.0、capabilityRevision=6；projects/tasks/runs 均 supported=true、available=false/executionUnavailable，commandReceipts 查询可用；权限模式空、maxCommandBytes=65536、其他限额 null、deviceID=null。GET Run 不增加内部 receipt/claimant/history ID 或 executionReady 字段；Project/Task executionReady 仍 false。
- 工程验证：最终 make all 的 Ruff、strict mypy（587 源文件）及全部非 live 测试通过，4942 passed / 23 skipped；提交仍必须通过仓库 pre-commit/testmon 与 Dashboard 构建。跳过不计通过；真实模型、live PostgreSQL 和 Ubuntu/Linux/Windows 盒子实机未验收。新 Fixture 核对派发状态、双连接竞争、fencing、重复接收、确认丢失、lane 保持、本人/admin 查询和原收据；skipped/deselected 不计通过，真实模型和实机/PG 门禁按最终证据分别记录。
- 本地：自有 8090 完整备份后升级 adapter0.6/revision6、schema23；身份、原 Project 收据字节和旧数据计数保持，stopped 托管专家及 failed/providerUnavailable 项目保持。Task/thread/session/Turn/Run/event 与新增派发 journal 均为0，usage/model calls为0；typed OpenAPI 和 Scalar 五个互斥阶段选项已核实，engineAccepted 样例仍为 queued。未知 Task POST/Run GET404、非法正文/ID422、匿名/URLtoken401、超限413；没有公开 claim/dispatch/recover、运行开关或 Task 创建入口，原8088健康200。升级前备份为 /Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-06-before-adapter-06-20261012T152548Z。本机原型不写 ready Fixture、Task/Turn/Run 或 journal；failed/providerUnavailable 项目、stopped 专家、原收据、boxID 和凭据保持，既有 8088 独立实例另行核对。备份和运行数据仅存忽略目录，不推送。
- 阶段边界：M1-06 内部派发和应用收据部分验证；生产项目 ready、原生历史/幂等、实际模型与工具、取消/审批、公开事件/快照、恢复 fencing、设备配对和盒子实机仍未通过。下一步按依赖完善引擎证据和工具 journal，不以 app ACK 宣称完整执行闭环。


## 第七轮交付状态

- 基线：主目录 `feature/agentcomputer-foundation @ ca904ae0f629fbbf49c1747b365a1277eb5a18bf`，继续同一用户 fork 功能分支；复用 schema23，没有客户端 Swift/UI 或新增公开 HTTP。
- 启动边界：daemon 持有当前 OCTOP_HOME 的生命周期 OS mutex，冲突为本地化 CONTROL_PLANE_BUSY/409；在 runtime 创建前执行可信内部恢复扫描，embedded 默认跳过。mutex只协调采用07约定的当前home，不能证明旧0.6进程、模型或子进程已停止，不构成集群/全机器互斥。
- 保守扫描：当前box、内部高水位和100条keyset分页，逐Run owner→Run锁内核对原接受图与journal，将未收敛claimed/dispatching/engineAccepted收紧为interrupted/reconciliationRequired，保留phase/epoch/双lane；吸收晚到确认，保留终态，重复扫描不重写原事件或ACK。没有重领、排队、模型resume、用户cancelRequested、停止/杀进程或许可释放。
- 检查点边界：只经既有agent公开saver保存/探测固定关联元数据，不冷开文件、不恢复模型循环。匹配仅是保存过关联的证据，不证明started/completed/停止或无效果；应用ACK和checkpoint不原子，原生History Turn仍无映射或假预留身份。
- 协议：adapter0.7.0/revision7，现有capability availability保持：projects/tasks/runs不可用、commandReceipts查询可用、recovery仍unsupported/notImplemented。权限模式空、deviceID=null、maxCommandBytes65536；Run GET保持12字段与五个phase样例，Project/Task executionReady=false，不新增HTTP恢复/取消流程。
- 工程验证：最终 make all 通过：Ruff、strict mypy（592 个源文件），5190 passed / 23 skipped；跳过不计通过。提交还须通过仓库实际 pre-commit/testmon 与 Dashboard 构建。真实模型、live PostgreSQL、Ubuntu/Linux/Windows 实机及完整 M0/M1 尚未验证。确定性Fixture覆盖合法派发后扫描、幂等/分页/归属/并发、双lane与原JSON保留、startup次序/mutex冲突和embedded跳过；原生metadata仅离线公开saver检查，skipped/deselected不计通过。
- 本地：完整备份后升级自有 8090 至 adapter0.7/revision7，schema23不变；原用户、stopped 托管专家、failed/providerUnavailable 项目、box身份和原 Project 收据字节保持。真实库0 Task/thread/session/Turn/Run/event，dispatch journal0条、usage/model calls0。第二个同home CLI显式指定host/port/SSL时返回 CONTROL_PLANE_BUSY，配置字节与锁inode不变，首个8090进程保持；原8088健康200。未知Task/Run404、非法正文/ID422、匿名/URLtoken401、body超限413，typed OpenAPI与Scalar五阶段示例可读，无HTTP claim/dispatch/recover/cancel或Task创建/执行入口。备份在 /Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-07-before-adapter-07-20261012T163009Z。原型不写ready/Task/Turn/Run/journal或真实模型配置；failed/providerUnavailable项目、stopped专家、原收据与身份保持。完整备份、运行数据库和凭据仅在忽略目录，旧8088独立实例另行核对。
- 剩余边界：扫描是安全收紧，不代表完整恢复或用户取消授权。真实模型/工具、原生历史/检查点恢复、进程停止证明、claimed安全重领、效果journal/审批/取消、事件/快照、备份防重放、设备配对、livePG与盒子实机及完整M0/M1仍待验收。


## 第八轮交付状态

- 基线：主目录 `feature/agentcomputer-foundation @ ab0f0391eaa5f88d236b646862832efa63f71b23`，继续同一用户 fork 功能分支；024 SQLite/PostgreSQL 配对迁移新增单一工具效果表，无客户端 Swift/UI、公开 HTTP 或生产运行开关。
- 内部事实：固定 effect/toolCall 身份、Run/Turn/消息/项目工作区、派发主体与双 epoch，prepared 意图及事件同事务；dispatching 先提交再调用受控 callback，executionAttempt 只能为1，同 Run 未收敛效果由部分唯一索引约束。terminal receipt 与事件同事务，原接受收据格式与字节不变。
- 结果与阻塞：prepared/dispatching 可收敛为 terminal 或 outcomeUnknown；dispatchStartedAt 的有无保留之前是否交接，不宣称 phase 完全不变。failed 可含部分效果，outcomeUnknown 缺可信耐久结果；输出 effects=unknown 即使报告 succeeded 也需对账。这些记录保持 Run reconciliationRequired 与 Task/project 双 active lane，不自动重放、递增 attempt、重建意图或释放许可。
- 授权与生命周期：派发前通过可信 caller bool callback 重检授权，撤销且确定未交接才能 notDispatched；本轮没有验收生产 catalog/文件 before-after。请求取消等待数据库操作结算，不能伪造用户取消或 stopped；精确重复调用读取原 receipt，不重新进入 handler。结果中的文件/成果 UUID 只是 adapter reported 关联，不是正式发布或已核实文件效果。
- 生产边界：默认 EngineEntryMiddleware.awrap_tool_call 始终拒绝，Fixture 内核不装配真实 Provider/工具或普通 Octop 旁路。原生检查点关联不证明工具效果，Swift 参数字符串与 Python 适配层 canonical 对象摘要互通、原生助手/历史身份映射仍未验收。
- 启动恢复：先扫描 Run，再扫描本box工具效果，两类分别固定高水位/100条分页；工具扫描覆盖 interrupted Run、停用 owner和旧epoch，不重放。内部report增加三项effects计数，默认0；无候选不读或创建控制身份。完整原接受图与effects校验，坏关联安全503不补建，公开字段不增加。
- 协议：adapter0.8.0/revision8；现有 capability availability 保持，projects/tasks/runs 不可用、commandReceipts 已有查询可用、recovery/文件/成果/审批/取消仍未实现。权限模式空、deviceID=null、maxCommandBytes65536；Run GET保持12字段，Project/Task executionReady=false，无新HTTP工具/恢复入口。
- 工程验证：最终 make all 通过：Ruff、strict mypy（592 个源文件），5175 passed / 23 skipped；跳过不计通过。提交还须通过仓库实际 pre-commit/testmon 与 Dashboard 构建。真实模型、live PostgreSQL、Ubuntu/Linux/Windows 实机及完整 M0/M1 尚未验证。隔离 Fixture 覆盖固定意图、精确重放、同 Run 竞争、派发前撤销、部分效果错误、取消/确认丢失、terminal与事件失败回滚、未知不重放及双 lane 保留；skipped/deselected不计通过。
- 本地：完整备份后升级自有8090至adapter0.8/revision8及schema24；原用户、stopped托管专家、failed/providerUnavailable项目、box身份和原Project收据字节保持。真实库0 Task/thread/session/Turn/Run/event、原派发journal0条、新tool effects0条、usage/model calls0。未知Task/Run404、非法正文/ID422、匿名/URLtoken401、body超限413；原typed OpenAPI和Scalar五阶段卡片可读，无HTTP工具/claim/dispatch/recover/cancel或Task创建/执行入口。同home第二CLI显式host/port/SSL返回CONTROL_PLANE_BUSY、config字节与锁inode保持，首8090进程及原8088健康200。备份：/Users/fangyuan/Desktop/code/Octop/.runtime/backups/iteration-08-before-adapter-08-20261013T021821Z。原型不写 ready/Task/Turn/Run/effect Fixture 或真实模型工具配置；failed/providerUnavailable项目、stopped专家、原收据与身份保持，备份、运行数据库和凭据仅保留忽略目录。
- 阶段边界：M1-07 部分通过，完整 M0/M1 未通过。生产 catalog/授权、实际文件 before/after、正式成果发布、工具隔离、原生历史/恢复、进程停止证明、持久取消/审批、公开事件/快照、恢复 fencing、设备配对、livePG与盒子实机继续逐项验收。
