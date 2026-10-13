# AgentComputer 中文流、传输故障与可信 Run 本地取消预检（第十八轮）

第十六轮分别验证了真实 GLM-5 文本流自然结束和普通 harness 的离线 thread 取消；第十七轮让持久 Run 取消请求能通知同服务器内精确匹配的活动 task。本轮继续补三项相互独立的证据：真实 GLM 中文流自然结束、离线模型流中断，以及隔离可信 Run 经认证取消到 harness 流关闭的完整本地链路。三项均不证明远端模型或工具子进程停止。

## 范围与输入

- 开工基线是主工作目录 `feature/agentcomputer-foundation @ a7b06af30fe27ead07ab9c21e0874afdc6f5f877`，工作树干净。本轮不改 schema、adapterVersion、capabilityRevision，也不装配生产 worker 或打开 `runs.available`。
- `scripts/agentcomputer_glm_live_preflight.py` 增加显式 `--mode stream-zh`。它沿用只读 Provider 读取、临时工作区、禁用工具/记忆/检查点/模型重试及脱敏输出，要求固定中文标记，并核对流式 token 中确实有汉字。原 `tool` 与 `stream` 模式保持原语义。
- 中文探针最多一次真实模型请求，`max_tokens=256`、HTTP 超时 20 秒、整体等待 45 秒、SDK 重试 0 次。它只输出路径、计数和布尔结果，不输出 Key、Authorization 或完整模型文本。下面命令会调用现有 GLM 并产生服务用量：

```sh
uv run python scripts/agentcomputer_glm_live_preflight.py \
  --provider-db "$HOME/.octop/octop.db" \
  --provider 'Zhipu Coding Plan (BigModel)' \
  --model glm-5 \
  --mode stream-zh
```

## 本地结果

2026-10-13 用既有 `Zhipu Coding Plan (BigModel)` Provider 和 `glm-5` 执行一次中文探针，退出码 0、`success=true`。实际路径仍是 `/api/coding/paas/v4/chat/completions`；一次请求、`stream=true`、响应为 `text/event-stream`、请求上限 256 token。收到 2 个非空 token 事件和 5 个状态快照，流自然结束；最终 AI 消息含固定中文标记，token 中检测到汉字。收到 1 个 usage 事件，但探针未核对累计用量，故仍记为 `unknown`；耗时约 5.08 秒。探针没有工具，未向 8090 导入 Provider 或 Key。

`tests/integration/test_agentcomputer_model_preflight.py` 新增参数化离线故障夹具。`httpx.MockTransport` 先返回未完成的 `synthetic_echo` 工具调用 SSE delta，再分别抛出 `httpx.ReadTimeout` 和 `httpx.ReadError`。两个场景均只发送一次模型 POST；异常向流消费端传播，HTTP 流关闭，工具没有执行，且没有模型完成 `state_update` 或文本 token。模型调用之前正常产生的输入 `state_snapshot` 不被误认为最终答复。此测试不访问真实 GLM，也不模拟服务端重试或重连。

`tests/integration/test_agentcomputer_trusted_run_cancel.py` 以隔离 Fixture 暂时提供 Run 领取条件；同一 `OctopServer` 的 `dispatch_run` 可信回调构造真实 `HarnessAgent`，通过 `invoke_engine_entry(..., active_runs=...)` 消费阻塞的合成工具调用 SSE。认证 `POST /runs/{runID}/cancel` 先提交持久取消事实，再精确取消本地活动 task。夹具观察到流关闭、工具未执行，已交接 Run 进入 `interrupted/reconciliationRequired`，Task/Project 双活动占用和原 `cancelRequested` 收据保留。Fixture 结束后生产 `runs.available` 仍为 false。它没有调用真实 GLM，也没有证明模型服务、工具子进程或计费已停止。

定向故障测试文件为 4 passed；可信 Run 夹具与现有取消 HTTP 测试合跑为 11 passed。仓库默认 `make all` 通过 Ruff、格式检查、严格 mypy 和全量非 live 测试，结果为 5297 passed、23 skipped。上述 skipped 不计入通过数；真实 GLM 中文探针的成功结果独立于离线测试。

## 门禁与后续

这三项证据只把开发机上的协议、故障传播和本地取消链路推进一步。真实 GLM 的超时/断线/取消仍未执行；HTTP 请求在数据库提交后、中断于发送信号前时也可能只留下持久取消事实。生产代码尚无调用 `dispatch_run`/`invoke_engine_entry` 的 worker，已交接 Run 缺停止核验、工具效果对账、持久终态 CAS 与双活动占用释放。生产项目 ready、Task/Run 执行及取消能力继续关闭。目标盒子系统、隔离、资源及其模型协议仍需实机验收，不能由现有 GLM 成功推定。
