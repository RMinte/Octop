# AgentComputer 与 Octop 集成

本项目把 Octop 作为 AgentComputer 的盒子执行引擎。首轮已建立认证能力查询和稳定服务身份；第二轮继续建立持久数据层、内部原子接收与已有收据查询，详细规则见 [第二轮数据层说明](agentcomputer-core.md)。

第三轮实现项目意图、停止的默认专家和受限工作区准备，详见 [项目准备说明](agentcomputer-projects.md)。当前完整项目执行能力仍未开放。

第四轮建立内部 Task/thread/session 原子映射和本人只读 Task 查询，详见 [Task 映射说明](agentcomputer-tasks.md)。真实任务创建和执行仍等待后续门禁。

当前只能在开发机本地部署。用户提供的目标设备描述是“可能为 Ubuntu、约 16 GB 内存的普通电脑，并有可部署 Qwen 27B 的独立算力”；系统、CPU 架构、容器条件、网络与实际可用资源尚未核实。它们是待确认输入，不是已验收的部署规格。

## 仓库与版本依据

| 项目 | 位置或来源 | 依据 |
|---|---|---|
| 交付 fork | [RMinte/Octop](https://github.com/RMinte/Octop) | 用户指定的后续交付仓库 |
| 上游 | [TencentCloud/Octop](https://github.com/TencentCloud/Octop) | 保留上游来源，后续升级先验证兼容性 |
| Octop 主目录基线 | `main` | `0c5a46ab5f82e5ad9d1a56fa09b00e542f042fda` |
| AgentComputer 主目录基线 | `feature/0.0.2/all` | `81e7acb653865f5c31342e844f2e90a36116caa9` |
| 本轮实施分支与提交 | `feature/agentcomputer-foundation`，从上述本地主目录 HEAD 创建；交付提交见 Git 历史 | 提交前重新核对主目录最新 HEAD |

Git remote 的实际名称、推送分支、提交和 PR 由交付记录填写；仓库网址不等于已经推送。工作开始核对主目录 status、worktree、branch 和 HEAD，保留已有文档及代码改动。提交运行仓库 hooks，完成默认 `make all` 门禁后再交付；不跳过红测试。

本文是 fork 内的集成入口文档；API 与运行说明见 [API](api.md)、[配置](configuration.md)、[CLI](cli.md)。AgentComputer 客户端的详细规划仍保存在其 `SPEC/029-octop-box-integration.md` 及步骤附录，本轮不把客户端代码移入 Octop。

## 阶段安排

| 阶段 | 交付内容 | 当前边界 |
|---|---|---|
| M0 环境和协议验证 | 部署输入、模型协议、隔离与容量、最小身份及能力入口 | 本轮推进可本地完成的基础；真实盒子仍待验 |
| M1 领域和命令接收 | 项目、Task/thread、Run、固定输入、命令幂等、最小派发和恢复 | 021 数据层与内部接收基础推进中；项目准备、线程创建与派发尚未开放 |
| M2 原生闭环 | 客户端远程会话、事件、审批、取消、正式成果及重连 | 不以实时连接成功代替可靠执行 |
| M3 配置与恢复 | 指令/技能版本、资源、Outbox、多设备与异常对账 | 保留配置和执行快照，不盲重放 |
| M4 调度与面板 | Schedule occurrence、Git/终端、受认证预览及资料库 | 工具隔离和项目写入规则先通过 |
| M5 迁移与发布 | 显式导入、完整备份恢复、TLS/配对、容量、升级和回滚 | 生产能力逐项实机验收后放行 |

阶段不代表固定工期。M0 本地检查通过不能宣称模型工具续轮、Linux Shell 沙箱、真实盒子恢复或生产配对已经完成。

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

后续先做离线载荷与工具续轮 Fixture，再选择明确免凭据适配或盒子内部认证入口。真实模型探针应单独约定地址、请求预算和合成数据；本轮不调用模型、不把模型服务连通写成盒子已配对。

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

## 下一轮 M1 领域准备

本轮工程验收完成后，下一轮先冻结公共 ID、owner 归属、项目准备状态、Task/thread 映射、Run/Turn、命令幂等和 fixed input 的契约及失败 Fixture。

随后按 Octop 配对 SQL 迁移和领域服务规范建立最小数据层；托管专家的 Dashboard、IM、Cron 和直接工作区写入口必须遵守同一 Run 登记与项目 writer lock，不能先开放原始写入口再补一致性。

M1 的本地 Fixture 可以继续迭代；真正派发模型与工具前仍需 M0 模型续轮及执行隔离门槛通过。生产配对、资料迁移、Schedule 切换和正式发布保留各自放行条件。

## 本轮交付状态

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
