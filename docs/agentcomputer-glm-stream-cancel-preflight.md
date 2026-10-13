# AgentComputer 本地 GLM 流式与取消边界预检（第十六轮）

第十五轮已经用现有 GLM-5 完成真实 Provider → harness 的合成工具续轮。本轮继续验证同一条本地模型路径的**文本流式输出**，并用确定性离线夹具检查 harness 的 thread 取消能否截断尚未完成的模型流。两项证据的对象不同：真实 GLM 请求只验证自然结束的文本流；取消夹具只验证本地消费端行为。它们都不开放 AgentComputer 生产托管 Run。

## 范围与输入

- Octop 开工基线是主目录 `feature/agentcomputer-foundation @ d97c03ca33f85c8cc105c48a318f907861ad57ec`。本轮沿用第十五轮的只读 Provider 读取方式，不改 8088 配置或 8090 实验实例的业务状态。
- `scripts/agentcomputer_glm_live_preflight.py` 的默认 `tool` 模式保持原有工具续轮探针；显式 `--mode stream` 才运行本轮流式探针。它从既有 SQLite 以 `mode=ro` 读取启用的 `Zhipu Coding Plan (BigModel)` Provider，在自动清理的临时工作区构造 `HarnessAgent`，不向 8090 导入 Key。
- 流式模式禁用工具、检查点、会话日志、记忆、团队、Web 搜索和模型重试。提示词只要求输出固定标记 `OCTOP_STREAM_OK`，不包含用户项目资料。最多一次模型 HTTP 请求，`max_tokens=256`；HTTP 请求超时 20 秒，整次流消费超时 45 秒，重试 0 次。请求钩子检查 `stream=true` 与 token 上限，响应钩子检查 `text/event-stream`。
- 探针只输出脱敏的路径、计数和布尔值，不输出 Key、Authorization、完整提示词或模型回复。请求与 token 上限是此次预检的规模约束，不能当作生产用量或费用限额。

持有该 Provider 的本机可选择性复跑；命令会实际调用 GLM 并产生服务用量：

```sh
uv run python scripts/agentcomputer_glm_live_preflight.py \
  --provider-db "$HOME/.octop/octop.db" \
  --provider 'Zhipu Coding Plan (BigModel)' \
  --model glm-5 \
  --mode stream
```

## 2026-10-13 验证结果

| 核对项 | 结果 |
|---|---|
| 真实模型入口 | `glm-5`；智谱 Coding Plan 的 `/api/coding/paas/v4/chat/completions`；一次请求，`max_tokens=256` |
| 传输与事件 | 请求 `stream=true`，响应 `text/event-stream`；收到 5 个非空 token 事件、5 个状态快照事件 |
| 完成条件 | 流迭代自然结束，最后的 AI 消息含 `OCTOP_STREAM_OK`；探针 `success=true`、退出码 0，耗时约 7.22 秒 |
| 用量 | 收到 1 个 usage 事件；探针未提取或核对累计 token 用量，故聚合用量记为 `unknown` |
| 秘密和实验状态 | 探针日志未包含所用 Key；8088 原 Provider 和 8090 原项目状态未被探针写入，8090 未新增 Provider、Task、Run、暂停、取消或事件 |

离线取消夹具见 `tests/integration/test_agentcomputer_model_preflight.py`。它用 `httpx.MockTransport` 模拟 Chat Completions SSE：先送出一个尚未结束的 `synthetic_echo` 工具调用片段，然后阻塞后续模型字节。消费端在这一时刻调用 `agent.cancel(thread_id)`。测试核对消费任务结束、HTTP 流关闭、仅发出一次模型请求、工具调用次数为零。包含原有 Provider wire 夹具的定向运行结果为 `2 passed`：

```sh
uv run pytest tests/integration/test_agentcomputer_model_preflight.py -q -n 0 --disable-warnings
```

该夹具证明 harness 能在所测阻塞点按 thread 取消本地流消费，并避免执行尚未完成的工具调用；它没有证明远端 GLM 服务停止生成或停止计费。真实 GLM 流式探针以固定 ASCII 标记验收，尚未核对中文流式文本、真实流中取消、错误流和断线重连。

## 后续门禁

M0-07 现在已有离线 Provider wire、真实 GLM 工具续轮、真实 GLM 文本流和离线 harness 取消边界的**分项证据**。目标 Ubuntu 盒子的 Qwen 服务地址、鉴权、协议、流式与取消仍需分别实测。M1 已交接 Run 的精确停止还缺 Run → 原生 thread/执行实例的绑定、停止请求与停止证明、工具效果和恢复状态的收敛；本轮的 `agent.cancel(thread_id)` 夹具不能代替生产 `POST /runs/{runID}/cancel` 的完成语义。执行隔离、盒子资源、生产配对和客户端端到端任务仍未验收，项目 ready 与托管执行能力继续关闭。
