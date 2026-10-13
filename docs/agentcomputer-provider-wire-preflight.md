# AgentComputer 模型协议离线预检（第十二轮）

这一轮为 M0-07 补充**无真实模型请求**的 Provider → octop-harness wire 证据。预检使用合成请求和受控响应，核对 Octop 实际发出的模型协议、鉴权头、工具调用与结果续轮、合成 SSE 文本终态和一次认证错误；不以纯内存 Fake model 代替 wire 路径。它不能证明目标 Qwen 27B 或原 RM-01 服务当前在线、接受这些字段、能够安全执行工具或已经满足盒子部署门禁。现有 `adapterVersion=0.11.0`、`capabilityRevision=11` 和 schema25 不因离线检查而放行。

## 已知差异与预检边界

AgentComputer 的历史 `box` 模型适配使用 Responses，原始 output item 重放、`store=false`，不发送 Authorization 或 `previous_response_id`，并发送 `chat_template_kwargs.enable_thinking=false`。这些是旧 RM-01 开发链路的证据；用户描述的 Qwen 27B 部署尚无已确认模型 ID、服务路由或协议。Octop 当前 `ProviderStore` 会跳过缺少 `base_url` 或 `api_key` 的 Provider，锁定的 `octop-harness 1.0.1` 配置也要求非空 Key；普通 OpenAI Provider 默认走 Chat Completions。离线预检要把这种差异具体化，不能通过未说明的占位 Key 把免鉴权路径记为已经兼容。

第六轮已有的 Fake Memory model 测试核对过 harness 入口和执行门禁，但没有编码 HTTP 请求。本轮测试在临时 SQLite 中创建 `local/qwen-27b-fixture` Provider，并确认空 Key Provider 被筛除；经真实 `ProviderStore` 构建 harness 配置，再由真实 `HarnessAgent.call` 完成合成工具续轮，`HarnessAgent.stream` 消费合成 SSE。它通过 `httpx.MockTransport` 截获模型请求，`https://wire.invalid/v1` 只是测试 URL，不访问外网；测试也不接入本机 8090 实验库或现有 8088 服务。`fixture-local-key` 仅为测试 Key，用来观察当前 SDK 发出的认证头，不能成为生产免鉴权兼容的结论。

| 事实 | 本轮离线证据 | 仍待验证 |
|---|---|---|
| Provider 装配 | 临时库中的 enabled OpenAI Provider 被识别为可用，`local/qwen-27b-fixture` 进入 harness 配置；同库空 Key Provider 不可用 | 目标服务的实际模型列表、版本和免 Key/内部认证方式 |
| 请求 wire | 两次工具往返请求、一次流式请求和一次合成 401 请求均为 `POST /v1/chat/completions`，携带 `model=qwen-27b-fixture` 与 `Authorization: Bearer fixture-local-key`；请求中没有 `store` 或 `previous_response_id` 字段 | Qwen 27B 对 Chat Completions、工具 schema 和这些 header 的接受情况；Responses 路径未测 |
| 工具续轮 | 合成模型发 `call-fixture-1`、参数 `{"value":"ping"}`；`synthetic_echo` 恰好收到一次 `ping`，次轮沿同一 call ID 回传 `pong`，得到最终中文文本 | 真实模型的工具选择、参数质量、续轮终态和真实工具隔离 |
| 流式与错误 | 合成 SSE 的两段文字和 `finish_reason=stop`、`[DONE]` 被 `agent.stream` 合成“流式完成”；次轮非流式响应省略 usage，结果未补造用量；一次合成 HTTP 401 抛出 `AuthenticationError` | 真实服务流式兼容、断线/超时、模型取消、工具和进程停止 |

## 验证与交付界线

定向测试文件是 `tests/integration/test_agentcomputer_model_preflight.py`；使用临时数据库、临时工作区和无外部副作用的 `synthetic_echo`。验证命令为 `UV_CACHE_DIR=/private/tmp/octop-uv-cache uv run --offline --no-sync pytest tests/integration/test_agentcomputer_model_preflight.py -q --disable-warnings`，当前结果为 **1 passed**。这只证明锁定依赖在合成 Chat Completions 服务前能完成上述工具往返、SSE 文本和认证错误；没有检验 Responses、无 Authorization 的路径、真实服务错误或取消。HTTP 内容与响应均为测试合成材料，没有读取真实凭据或用户资料。

完整工程门禁 `make all` 在允许本机回环测试的环境中通过：Ruff、格式检查、严格 mypy（598 个源文件）及非 live 测试 **5256 passed、23 skipped**；跳过项不计为通过。日志位于 `/private/tmp/octop-iteration-12-make-all-elevated.log`。首次受限沙盒运行因既有回环端口测试无法 bind 而失败，不能作为通过证据。

旧 App 的 box dialect 走 Responses；本轮 Octop 路径走 Chat Completions，两者不能因各自能处理合成工具调用就视为等价。目标模型的服务协议及鉴权需要在实际入口确认，再决定显式免 Key 适配或盒子内部认证代理；不能把测试 Key 写入生产配置来掩盖差异。本轮测试也没有装配 AgentComputer 托管 Run 的真实 worker、授权工具目录或历史/检查点恢复。

即使离线夹具通过，M0-07 的退出标准仍要求经确认的真实模型地址、版本、鉴权和请求预算，在目标环境完成 Octop 引擎的一次工具续轮；M0-02/03/05/06 的系统、网络、镜像和隔离证据也仍未取得。项目 ready、Task 对外创建、模型/工具生产执行、审批续跑及已交接 Run 的停止证明继续关闭。
