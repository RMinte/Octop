# 任务 WebSocket 连接诊断

更新日期：2026 年 10 月 22 日。适用于协议版本 3 的 `/api/project-tasks/ws`，不改变现有协议帧。

## 连接 ID

原生客户端可在每次握手发送 `X-AgentComputer-Connection-ID`，使用本次连接的新 UUID（36 字符、带短横线，大小写均可）。服务端在握手响应中原样回显合法值，并独立生成 `X-Octop-Connection-ID`。

缺失、重复或非法的客户端 ID 不会原样写日志或回显；日志用 `client_connection_id_status` 区分。代理若传入单个 `X-Request-ID`，仅 UUID、32 位十六进制或 ULID 格式会记入 `request_id`；其他值丢弃。

## 日志位置与关联

服务端日志位于 `$OCTOP_HOME/logs/octop.log`（默认 `~/.octop/logs/octop.log`），继续按现有大小、日期和保留期限轮转。

搜索 `project_task_ws ` 后的 JSON，使用 `client_connection_id` 与客户端按次连接的记录关联，使用 `server_connection_id` 区分服务端实际接到的每次连接。当前客户端若未收集 `X-Octop-Connection-ID` 响应头，仍可用它已发送的 `X-AgentComputer-Connection-ID` 完成关联。

每条记录包含 `schema_version`、UTC `timestamp`、连接内递增 `seq`、距接入的 `elapsed_ms` 和固定路由路径。

| 事件 | 含义 |
| --- | --- |
| `connection_received` / `connection_accepted` | 路由收到连接 / ASGI 已接受握手提交 |
| `authentication_started` / `authentication_succeeded` / `authentication_failed` | 首次认证过程及后续认证失败 |
| `ready` | ready 帧已提交，连接可以处理命令 |
| `session_renewed` | 新凭据已通过 session 帧提交，不记录凭据值 |
| `error` | 遇到错误，记录阶段、错误码、HTTP 状态及作用范围 |
| `close_requested` / `close_sent` | 准备关闭 / ASGI 已接受关闭帧提交 |
| `transport_error` / `unexpected_error` | 传输异常 / 意外异常，仅保留安全元数据 |
| `connection_closed` | 本次路由处理结束，包含关闭状态与累计计数 |

后续认证检查只累计次数，并记录失败或续期，避免逐条业务内容落盘。命令类型仅取通过协议校验的枚举。

## 关闭状态与计数

`close_sent` 只能证明 ASGI 已接受关闭帧提交，不能证明对端收到。最终 `connection_closed` 含关闭码、来源、阶段和计数：

- `close_source=server` 表示服务端成功提交关闭；`peer` 表示收到断开事件，不能据此判定用户主动退出。
- 已知发送 `OSError` 被 Starlette 包装成 1006 时仍识别为 `transport`，不会被后续普通断开覆盖；取消且尚无已知断开来源时记为 `cancelled`。
- `command_count` 只计通过协议校验的命令；`subscription_count` 是结束时仍保留的订阅数。
- `auth_check_count` 包含首次认证；`error_count` 是遇到的错误数，不保证错误帧送达。

## 隐私与定位边界

连接日志不记录 Authorization、token、原始请求头、URL 查询参数、命令 ID、任务 ID、游标、会话正文、工具参数/结果、错误消息或关闭原因。

意外异常仅记录异常类型和代码文件名/函数/行号，不保留异常文本、源码行或局部变量。Uvicorn 的协议 DEBUG/TRACE 帧及请求头日志受抑制，访问日志去掉查询参数，框架异常日志移除原始堆栈文本。

如果握手在代理或更早的层被拒绝，此路由可能完全没有 `connection_received`。需要代理透传上述客户端连接 ID，并结合代理自身的请求 ID、上游状态和时间核对；仅凭服务端没有记录，不能确定 403 的来源。
