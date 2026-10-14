# AgentComputer 盒子集成进度

更新于 2026-10-14。这里按阶段退出标准计数，不按代码提交或迭代轮数推算百分比。`●` 表示整个阶段已验收，`◐` 表示只有部分可复核证据，`○` 表示阶段尚未开始。

**整体进度：`[◐ ◐ ◐ ○ ○ ○]`，0/6 阶段完整验收；M0、M1、M2 进行中。**

| 阶段 | 状态 | 已有可复核结果 | 尚缺的退出条件 |
|---|---|---|---|
| M0 环境与协议 | ◐ | 本机 GLM-5 工具续轮、文本流，以及离线故障和取消夹具 | 目标 Ubuntu 盒子的资源、模型协议、执行隔离与容量实测 |
| M1 盒子服务端 | ◐ | 稳定身份、持久领域、项目准备、内部派发与对账、精确取消；AgentComputer HTTP 操作关闭等待（15 项审计通过）；内部派发和引擎入口强制登记，真实 Harness 流的服务关闭并发回归；受托 Turn 的原生 v2 历史固定身份、流归档和启动只读对账；按登录用户隔离的只读项目列表和 8090 本机联调 | 生产项目 `ready`、公开 Task/Run 创建、托管 worker、Run 自然完成收据、真实工具、已交接停止证明和完整恢复；控制库与历史库仍无原子提交 |
| M2 原生客户端 | ◐ | AgentComputer“设置 → 连接”可用 Octop 用户会话查看盒子能力和远端项目准备状态；macOS 项目侧栏和 iOS 任务目录已有独立只读“盒子项目”入口、身份绑定的项目详情与状态解释；macOS 实际界面已点验项目列表与 `providerUnavailable` 详情；盒子专项单测 8/8、完整 `make verify`、macOS/iOS 模拟器构建通过 | 设备配对、远端项目与本机项目绑定、Task/Run、消息/事件/审批/取消/成果与重连验收；iOS 实际界面点验 |
| M3 配置与恢复 | ○ | 规划已形成 | 指令/技能版本、资源、Outbox、多设备和异常对账验收 |
| M4 调度与面板 | ○ | 规划已形成 | Schedule、Git/终端、受认证预览与资料库验收 |
| M5 迁移与发布 | ○ | 规划已形成 | 显式导入、备份恢复、TLS/配对、升级回滚和盒子实机发布验收 |

本轮[原生历史绑定](agentcomputer-native-history.md)的受托引擎定向回归为 65 passed；最终 `make all` 的 Ruff、严格 mypy（600 个源文件）和非 live 全套通过，5333 passed / 23 skipped。跳过项不计为通过。首轮受限沙箱无法绑定测试所需的 `127.0.0.1` 端口；在允许本地回环监听的环境下完整复验通过。服务关闭测试只证明本地流关闭与保守对账，不构成远端模型或工具子进程停止证明。

后续[启动只读对账](agentcomputer-native-history.md#启动时的只读历史对账)已把 ACK 后的归档缺失、Turn 缺失、归属不符、六种历史状态和初始消息落盘窗口纳入内部恢复报告及无身份信息的汇总警告日志。它只做诊断：即使历史为 `complete`，Run 仍保守中断并保留双活动占用；不会因为这项证据开放执行。定向恢复测试 37 passed；最终完整 `make all` 的 Ruff、严格 mypy（600 个源文件）和非 live 全套通过，5342 passed / 23 skipped。

**当前用户可见的切片**位于 AgentComputer“设置 → 连接”及独立“盒子项目”入口：macOS 本机可输入 `http://127.0.0.1:8090`，使用这个独立实验实例的 `local-admin` 账号查看盒子身份、能力、远端项目列表与只读详情。实验密码仅保存在本机忽略文件 `.runtime/agentcomputer/local-credentials.json`，不与 8088 的 `fangyuan` 账号共用；iOS 真机需要可访问的 HTTPS 盒子地址。8090 当前 Provider 数为 0，8088 的 GLM 配置未进入这个隔离实例；旧项目的 `modelRef=local/qwen` 因此保持 `failed/providerUnavailable`、`executionReady=false`。这里使用用户会话，尚无设备配对或远端任务执行。Octop 8088 的现有聊天页不会因服务端 API 增加而自动出现项目入口；AgentComputer 原有的本机项目与盒子项目分开展示，不能作为盒子端到端执行验收。

**下一步按依赖推进：**先给无工具、无暂停的可信 Run 建立独立自然完成收据与控制库终态事务，核对固定历史 Turn 的 `complete`、取消竞争、原派发/效果绑定和双占用释放；再建立每 Run 独立的受限托管 worker，显式选择 Provider/模型并接入服务器关闭等待；之后逐项验收真实工具授权与效果、已交接停止证明，才开放生产项目 `ready` 和公开 Task/Run。项目列表可读不代表项目 `ready`；当前 `projects.available=false`，Task/Run 创建与执行门禁仍关闭，不能把本地夹具称为远端任务已可用。

当前实施分支是 `feature/agentcomputer-foundation`，交付目标为 [RMinte/Octop](https://github.com/RMinte/Octop)。每轮在主[集成记录](agentcomputer-integration.md)中注明基线、改动、验证与剩余门禁。
