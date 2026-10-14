# AgentComputer 本机纯文本 Task 试用

这一切片让 AgentComputer 的“盒子项目”页面创建 Task、提交文本 Turn、查看 Run 状态和最终回答。它只面向本机回环地址上的单进程 Octop，使用 Octop 用户 Bearer 会话。默认关闭；启用时固定一个已配置的模型，并要求 SQLite 控制库和 v2 历史。工具、MCP、技能、子代理、文件操作、审批续跑和远端设备配对均未开放。

## 本机启用

1. 在运行这份代码的 Octop 实例中，通过管理界面配置一个可用的聊天 Provider。项目 `modelRef` 必须是该实例的 `Provider名/模型ID`；8088 和隔离的 8090 实例各有自己的控制库，不能把前者有 GLM 误认为后者也有。验证模型列表请使用不含密钥的 `/api/providers/resolved`，不要打印 `/api/providers` 的完整响应。
2. 停止要升级的本机实例并完整备份其 `OCTOP_HOME`，包括 SQLite WAL 文件。仅修改该实例的 `config.json`：

   ```json
   {
     "bind_host": "127.0.0.1",
     "history_v2_enabled": true,
     "agentcomputer_text_enabled": true,
     "agentcomputer_text_model_ref": "Provider名/模型ID"
   }
   ```

   把这些字段合并进现有配置，不要覆盖数据库、TLS 和其他设置。GLM 可以继续作为模型；这里无需等待 Qwen 服务地址。启用后使用一个 worker 且不开 reload，例如 `octop run --host 127.0.0.1 --workers 1`。非回环监听或多 worker 会在启动前被拒绝。
3. 使用该实例的 Octop 账号在 AgentComputer“设置 → 连接”登录。能力查询应显示 `projects/tasks/runs.available=true` 和 `supportedPermissionModes=["askForApproval"]`；若仍为 false，检查当前实例的 Provider、模型引用、v2 历史、SQLite 和监听地址。
4. 在 AgentComputer“盒子项目”里创建使用同一 `modelRef` 的新项目。项目详情显示 `ready` 且 `executionReady=true` 后创建 Task，输入一条短文本并提交。客户端先持久保存命令 ID、幂等键和原始请求；丢失响应时先回查原收据，避免重复创建。Run 成功后只展示服务端从固定 v2 历史及完成收据共同验证的文本结果。

已有的 `failed/providerUnavailable` 项目不会因为改了服务端模型就自动变成新模型项目；请新建与当前固定模型一致的项目。项目、Task 与 Turn 都按登录用户隔离。一次 Run 未完成时，该项目和 Task 的活动 lane 仍被占用；已交接的中断会保守保留 `reconciliationRequired`，不能盲目重投。

## 当前边界

- 能力开关只证明本机配置、Provider 目录和持久层满足接入条件，不证明目标 Ubuntu 盒子的部署、容量或远端模型停止。
- 新 Run 使用单进程后台 worker，最多同时进入一个模型循环。重启时只重新领取从未派发的 queued Run；有派发事实的未终结 Run 进入保守对账，不重复执行。Provider 暂不可用时会定期重查未派发队列。
- 当前项目只持久绑定 `modelRef`。同名 Provider 的地址或密钥被替换后，可用性检查仍可能通过；正式盒子部署前需要补 Provider 指纹绑定、网络与资源隔离以及凭据轮换验收。
- 关闭服务器会等待已交接 Run 的本地结算和不确定性围栏。若远端模型流或底层 I/O 永不返回，关闭可能继续等待；不能通过超时释放单实例锁来假装远端执行已经停止。
- 2026-10-14 已先备份并升级独立的 8090 实例，固定使用已有的 GLM-5 配置。一次真实 HTTP 验收完成：新项目 `ready/executionReady=true`，Task 与 Turn 接收成功，Run 由 `queued` 到 `succeeded`，结果接口返回“2+2 等于 4。”；8088 用户实例未重启。这只证明开发机本地无工具文本任务可运行，尚未验收 Ubuntu 盒子、网络断线恢复和 AgentComputer 实际界面操作。

API 详情见 `/api/docs` 的 AgentComputer 分组。实现和更广阶段边界见[集成入口](agentcomputer-integration.md)与[阶段进度](agentcomputer-progress.md)。
