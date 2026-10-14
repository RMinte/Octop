# AgentComputer 盒子集成进度

更新于 2026-10-14。这里按阶段退出标准计数，不按代码提交或迭代轮数推算百分比。`●` 表示整个阶段已验收，`◐` 表示只有部分可复核证据，`○` 表示阶段尚未开始。

**整体进度：`[◐ ◐ ◐ ○ ○ ○]`，0/6 阶段完整验收；M0、M1、M2 进行中。**

| 阶段 | 状态 | 已有可复核结果 | 尚缺的退出条件 |
|---|---|---|---|
| M0 环境与协议 | ◐ | 本机 GLM-5 工具续轮、文本流，以及离线故障和取消夹具 | 目标 Ubuntu 盒子的资源、模型协议、执行隔离与容量实测 |
| M1 盒子服务端 | ◐ | 稳定身份、持久领域、项目准备、内部派发与对账、精确取消；AgentComputer HTTP 操作关闭等待；受托 Turn 的原生 v2 历史与无工具自然完成证明；本机固定 GLM 模型的公开项目/Task/Turn、单进程 worker 和验证过的 Run 结果，8090 实例真实运行成功 | Ubuntu 盒子部署、真实工具授权、已交接停止证明、完整恢复与控制库/历史库跨库一致性 |
| M2 原生客户端 | ◐ | AgentComputer“设置 → 连接”已可查看能力；独立“盒子项目”入口已实现项目创建、Task 提交、Run 轮询与结果显示，命令丢失响应可按原 ID 回查；客户端测试和构建正在复验 | 客户端实际界面点验、设备配对、远端项目与本机项目绑定、事件/审批/取消/正式成果与重连验收 |
| M3 配置与恢复 | ○ | 规划已形成 | 指令/技能版本、资源、Outbox、多设备和异常对账验收 |
| M4 调度与面板 | ○ | 规划已形成 | Schedule、Git/终端、受认证预览与资料库验收 |
| M5 迁移与发布 | ○ | 规划已形成 | 显式导入、备份恢复、TLS/配对、升级回滚和盒子实机发布验收 |

本轮[原生历史绑定](agentcomputer-native-history.md)的受托引擎定向回归为 65 passed；最终 `make all` 的 Ruff、严格 mypy（600 个源文件）和非 live 全套通过，5333 passed / 23 skipped。跳过项不计为通过。首轮受限沙箱无法绑定测试所需的 `127.0.0.1` 端口；在允许本地回环监听的环境下完整复验通过。服务关闭测试只证明本地流关闭与保守对账，不构成远端模型或工具子进程停止证明。

后续[启动只读对账](agentcomputer-native-history.md#启动时的只读历史对账)已把 ACK 后的归档缺失、Turn 缺失、归属不符、六种历史状态和初始消息落盘窗口纳入内部恢复报告及无身份信息的汇总警告日志。它只做诊断：即使历史为 `complete`，Run 仍保守中断并保留双活动占用；不会因为这项证据开放执行。定向恢复测试 37 passed；最终完整 `make all` 的 Ruff、严格 mypy（600 个源文件）和非 live 全套通过，5342 passed / 23 skipped。

**当前用户可见的切片**位于 AgentComputer“设置 → 连接”及独立“盒子项目”入口：macOS 本机可输入 `http://127.0.0.1:8090`，使用独立实验实例的 `local-admin` 账号创建 GLM 项目与 Task，并查看 Run 状态和回答。实验密码仅保存在本机忽略文件 `.runtime/agentcomputer/local-credentials.json`，不与 8088 的 `fangyuan` 账号共用；iOS 真机需要可访问的 HTTPS 盒子地址。旧项目的 `modelRef=local/qwen` 仍是 `failed/providerUnavailable`，应创建固定 GLM 模型的新项目。这里使用用户会话，尚无设备配对；Octop 8088 的现有聊天页不会因服务端 API 增加而自动出现项目入口。

第二十六轮[自然完成内核](agentcomputer-run-completion.md)已把无工具真实 Harness 流的严格历史/持久检查点证明、完成收据、`run.finished` 和双占用释放串起来；取消造成的正常 EOF、空回答或缺证据仍保守对账。当轮 schema 升至 26，公开 HTTP 和能力开关尚未放行，8090 实例也尚未升级；本页下方记录了后续本机文本 Task 升级与实测。

第二十六轮完整 `make all` 通过，5396 passed / 23 skipped；严格 mypy 覆盖 602 个源文件。数据库与备份的旧“最新 schema=25”断言已同步到 26，旧 v25 故障注入条件仍针对 v25 本身。

**2026-10-14 本机 Task 试用验收：**以 `feature/agentcomputer-foundation @ 74ce4d82` 为基线，8090 独立控制目录先备份后升级到 adapter 0.15.0，配置已存在的 GLM-5 Provider 与固定 `textModelRef`。认证能力返回 `projects/tasks/runs.available=true`；新项目 `ready/executionReady=true`，Task 创建成功，Run 从 `queued` 到 `succeeded`，严格结果接口返回“2+2 等于 4。”。`/api/openapi.json` 中新路由的摘要、说明与类型化 2xx 响应已核对。本轮最终 `make all` 通过，5416 passed / 23 skipped；运行中的 8088 用户实例未重启。这个证据只覆盖开发机本地、无工具、单 Run 的真实模型路径；客户端实际界面点验仍待完成。

**下一步按依赖推进：**完成 AgentComputer 的完整构建和实际界面点验，继而验证重启/断线后原命令回查与保守对账；然后逐项验收 Ubuntu 盒子部署、真实工具授权与效果、已交接停止证明，最后才扩大生产范围。完整阶段退出标准没有因本机试用而降低。

当前实施分支是 `feature/agentcomputer-foundation`，交付目标为 [RMinte/Octop](https://github.com/RMinte/Octop)。每轮在主[集成记录](agentcomputer-integration.md)中注明基线、改动、验证与剩余门禁。
