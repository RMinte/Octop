# AgentComputer 本地 GLM 真实协议预检（第十五轮）

用户决定先复用当前可用的 GLM 做本地联调，盒子部署时再单独验收 Qwen。此轮只验证 **Octop ProviderStore → octop-harness → GLM Chat Completions → 合成工具续轮**；不开放 AgentComputer 托管执行能力，也不把本地 GLM 当作目标盒子模型。

## 输入与隔离

- Octop 开工基线：`feature/agentcomputer-foundation`，`a225e52b91928d9a837633fb46c250cc21964c43`；主工作树干净。AgentComputer 主目录是 `feature/0.0.2/all`、`67d203baceb4de1a77c6ddb4843af33642e69333`，已有未提交文档，不由本探针改动。
- 既有 8088 Octop 实例的只读 SQLite 中有启用的 `Zhipu Coding Plan (BigModel)` Provider 和 `glm-5`。实际请求发往智谱 Coding Plan 的 `/api/coding/paas/v4/chat/completions`，使用已有 Bearer 凭据；探针只在进程内读取凭据，不向 8090 导入 Provider，不在代码、文档或输出中记录 Key。
- `scripts/agentcomputer_glm_live_preflight.py` 从指定 SQLite 以 `mode=ro` 读取单个 Provider，经 `ProviderStore` 装配到临时 `HarnessAgent`。工作区是自动清理的临时目录；关闭检查点、会话日志、记忆、团队、Web 搜索和模型重试。工具 `synthetic_echo` 只按 `ping` 返回 `pong`，不接触用户项目资料。
- 为给这次探针注入 HTTP 超时、零重试和请求计数，脚本在独立进程内临时包装 harness 的模型构造函数；这不是生产引擎适配或托管 Run 的实现。
- 每次最多 3 个模型 HTTP 请求，每次输出上限 512 token；SDK 请求超时 20 秒、整轮等待 50 秒、重试 0 次、LangGraph 递归上限 20。探针仅输出脱敏统计，失败时返回非零退出码。请求数和输出上限限制探针规模，但不是生产用量或费用限额。

经明确选择后可在持有该本地配置的机器上重跑：

```sh
uv run python scripts/agentcomputer_glm_live_preflight.py \
  --provider-db "$HOME/.octop/octop.db" \
  --provider 'Zhipu Coding Plan (BigModel)' \
  --model glm-5
```

此命令会请求真实模型并产生服务用量。不要提交数据库、凭据、完整请求/响应或调试日志。

## 2026-10-13 实测

首次探针把 LangGraph 递归上限设为 8：模型已调用 `synthetic_echo(ping)`，第二个请求也携带匹配的工具结果 ID，但在取得最终答复前触发 `GraphRecursionError`。这次不能计为完整工具续轮通过。只将探针的递归上限调为 20 后复验，其余请求数、token、超时、模型与临时工作区限制保持不变。

| 核对项 | 复验结果 |
|---|---|
| 模型入口 | `glm-5`；两次 `POST /api/coding/paas/v4/chat/completions`；每次 `max_tokens=512` |
| 工具调用 | `synthetic_echo` 恰好收到一次 `ping`，返回 `pong` |
| 结果续轮 | 第二次模型请求的 assistant call ID 与 tool result ID 一致 |
| 终态 | 最终消息包含 `pong`；无异常；约 11.12 秒 |
| 可见用量 | 最终消息报告 `input_tokens=5940`、`output_tokens=17`；这是最终消息的 usage，不代表两次请求的总计费量 |
| 秘密与状态 | 探针日志未包含所用 Key；8088 Provider 仍为 1 条；8090 仍为 0 Provider、1 项目意图、0 Task/Run/暂停/取消/事件 |

仓库内保存的脚本又按上面的命令复跑一次：退出码 0，`success=true`，同为 2 次请求、512 token 上限、一次工具调用及匹配续轮，最终答复包含 `pong`，约 9.36 秒；最终消息 usage 同为 5940 输入/17 输出 token。复跑日志也未包含 Key。

这证实当前 GLM 凭据、Chat Completions 路径、工具调用 ID 回放和最终答复在本机 Octop harness 路径上可用。服务端具体构建版本未取得；真实流式续轮、错误与取消、受控工具授权、托管 Run/检查点、Ubuntu 盒子网络与隔离尚未验证，也未请求或验收盒子 Qwen。M0-07 可记为“本地 GLM 工具续轮子门禁通过”，目标环境的 M0-07 总门禁仍阻塞；项目 ready、Task 对外创建、真实模型/工具生产执行及审批续跑继续关闭。
