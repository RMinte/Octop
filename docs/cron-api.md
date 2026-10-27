# 定时任务接口

更新日期：2026-10-27。项目与专家可选、`skip_confirmation` 已部署到本机 `http://127.0.0.1:8088`，并核对在线 OpenAPI 和文档渲染。执行与审批边界通过隔离运行测试验证，本轮未在生产触发发送或发布；其他主机以实际接口契约为准。

在线调试：[API 文档](http://127.0.0.1:8088/api/docs)，选择 `cron` 分组；完整契约：[OpenAPI JSON](http://127.0.0.1:8088/api/openapi.json)。其他主机请替换域名和端口，并以该主机实际返回的契约为准。

## 基本约定

- 统一使用 `/api/cron`，客户端不再按当前专家拼接创建和列表 URL。
- **项目、专家均可选，独立选择。** 不关联项目时，新 Task 使用任务独立空间；不选专家时使用通用助手，不创建隐含专家。
- 选择项目时，使用项目的目录、行动指南及能力配置；项目必须属于当前用户且未归档。
- 全部定时任务管理接口需要 `Authorization: Bearer <access_token>`。Token 来自 `POST /api/auth/login`。
- 只允许管理本人的定时任务，管理员也不越过该归属范围。访问他人的单条定时任务返回 `404`。
- 请求正文使用 `Content-Type: application/json`；可传 `Accept-Language: zh` 获取中文错误消息。
- 调度时区取服务端 `default_timezone`，客户端可通过 `GET /api/settings/timezone` 获取；`GET /api/cron/settings` 是兼容别名。

## 接口一览

| 方法   | 路径                                | 成功响应                                       |
| ------ | ----------------------------------- | ---------------------------------------------- |
| GET    | `/api/cron`                         | `200 CronResponse[]`，当前用户全部定时任务     |
| GET    | `/api/cron?project_id=<project_id>` | `200 CronResponse[]`，指定项目下的本人定时任务 |
| POST   | `/api/cron`                         | `201 CronResponse`，创建定时任务               |
| GET    | `/api/cron/{cron_id}`               | `200 CronResponse`，读取详情和最近执行状态     |
| PATCH  | `/api/cron/{cron_id}`               | `200 CronResponse`，局部更新                   |
| DELETE | `/api/cron/{cron_id}`               | `204`，无响应正文                              |
| POST   | `/api/cron/{cron_id}/run-now`       | `204`，无响应正文，异步触发一次                |
| GET    | `/api/settings/timezone`            | `200 {"timezone":"Asia/Shanghai"}`，示例时区   |

列表直接返回数组，没有分页包裹。省略 `project_id` 返回全部本人定时任务，包含未关联项目的记录。当前没有“仅未关联项目”的服务端筛选参数；客户端从全部列表中筛选 `project_id === null`。不要发送 `project_id=null` 字符串代替空值。

## 创建

`POST /api/cron` 只有 `trigger` 和 `prompt` 必填。

| 字段                  | 类型          | 默认值      | 含义                                                                                                |
| --------------------- | ------------- | ----------- | --------------------------------------------------------------------------------------------------- |
| `trigger`             | string        | 必填        | 触发规则，格式见下表                                                                                |
| `prompt`              | string        | 必填        | 去除首尾空白后非空，最多 2000 字符                                                                  |
| `name`                | string / null | 自动生成    | 显示名称，最多 80 字符；省略、空白或 null 时从 prompt 生成                                          |
| `project_id`          | string / null | null        | 可选项目；省略或 null 表示定时任务不关联项目                                                        |
| `selected_expert_id`  | string / null | null        | 可选的可用专家 ID；省略或 null 使用通用助手                                                         |
| `task_type`           | string        | `"text"`    | `"text"` 原文提醒，不调用模型；`"agent"` 每次触发时执行模型任务                                     |
| `enabled`             | boolean       | true        | 是否启用自动触发                                                                                    |
| `skip_confirmation`   | boolean       | false       | 独立授权此定时器生成的模型执行轮次跳过 Octop 服务端工具确认，范围与限制见后文                        |
| `conversation_policy` | string        | `"per_job"` | Task 复用策略，见后文                                                                               |
| `target_task_id`      | string / null | null        | 仅 `task` 策略使用的目标 Task                                                                       |
| `model`               | string / null | null        | 可选模型引用；null 表示不指定模型覆盖，由 Task 执行链路解析                                         |
| `mcp_servers`         | string[]      | `[]`        | 显式 MCP server 选择，必须是当前用户有权使用的引用；非空时覆盖该轮默认 MCP 集合，空列表继承默认配置 |
| `session_key`         | string / null | 服务端生成  | 既有来源会话和投递绑定，普通客户端新建时可省略                                                      |
| `fresh_thread`        | boolean       | false       | 兼容字段，仅 `continue_target` 策略用它在每次触发前开启新 Task                                      |

需要模型执行时，务必明确传 `"task_type": "agent"`。获取专家选项使用 `GET /api/project-tasks/resources` 的 `experts`，项目选项使用 `GET /api/projects`。`task_type` 的客户端合法选项为 `text` 和 `agent`，不要传其他字符串。

### 触发规则

| 格式                    | 示例                              | 含义                                                     |
| ----------------------- | --------------------------------- | -------------------------------------------------------- |
| `cron:<五段表达式>`     | `cron:0 9 * * *`                  | 服务端时区每天 09:00                                     |
| `interval:<正整数秒>`   | `interval:3600`                   | 每隔 3600 秒                                             |
| `date:<ISO8601>`        | `date:2026-11-01T09:00:00+08:00`  | 指定时间执行一次；无时区时按服务端时区解释               |
| `agently:<instance_id>` | `agently:<connector_instance_id>` | 指定 Agent Mail 连接收到新邮件时触发，需要有效连接和授权 |

cron 表达式顺序为“分、时、日、月、星期”，星期数字沿用 Unix 约定：`0` 或 `7` 为周日，`1` 为周一。不要省略 `cron:` 等前缀。

### 无项目、无专家的提醒

```json
{
  "name": "每日提醒",
  "trigger": "cron:0 9 * * *",
  "prompt": "记得安排今天的工作",
  "task_type": "text",
  "project_id": null,
  "selected_expert_id": null
}
```

### 关联项目并使用通用助手执行

```json
{
  "name": "项目日报",
  "trigger": "cron:0 18 * * 1-5",
  "prompt": "总结项目今天的变化与待办事项",
  "task_type": "agent",
  "project_id": "<project_id>",
  "selected_expert_id": null,
  "conversation_policy": "per_job",
  "enabled": true
}
```

如需专家，只需将 `selected_expert_id` 换为可用专家 ID，不需要把项目绑定到专家。

### 显式跳过工具确认

创建请求、PATCH 和 `CronResponse` 均支持布尔字段 `skip_confirmation`，创建默认 `false`，已有定时器升级后同样为 `false`。`false` 沿用账号现有审批规则；`true` 是此定时器的独立显式授权，不修改账号全局 `approval_policy` 或扩大账号 `full_access` 模式的工具范围。

开启后，仅此 Cron 在 Octop 内置执行引擎生成的模型执行轮次，可对原安全策略允许 `allow_once` 的服务端工具审批保存单次批准，涵盖 `execute`、MCP 以及发送、发布工具。管理员 `require_human_tools`、禁止规则和既有拒绝仍生效；客户端电脑的目录/命令授权、ACP 原生权限不变，提问仍等待回答。暂停、取消和来源或资源撤权仍在派发前检查。`task_type: "text"` 不执行模型工具。

一次触发受理时，在 occurrence 与生成轮次中固定该选择；之后编辑定时器不会改写已受理触发或轮次。授权只属于该次 Cron 轮次，不流入同一 Task 的手动追问、fork 或 retry。每个实际放行的动作记录 `allow_once`，审计来源为 `cron_skip_confirmation`，不能由普通消息、工具文本或客户端自行声明为可信批准。CLI 本轮不新增此字段的命令行选项。

`POST /api/cron`：

```json
{
  "name": "自动项目日报",
  "trigger": "cron:0 18 * * 1-5",
  "prompt": "总结项目变化并按项目配置发送日报",
  "task_type": "agent",
  "project_id": "<project_id>",
  "selected_expert_id": null,
  "skip_confirmation": true
}
```

关闭后续触发的授权，`PATCH /api/cron/{cron_id}`：

```json
{
  "skip_confirmation": false
}
```

PATCH 省略该字段或明确传 `null` 都保留原值，只有 `false` 才关闭；已受理轮次保留原快照。完整审批边界见[Task 审批模式](project-task-approval-modes.md#cron-独立显式授权)。

## 对话与执行策略

| `conversation_policy` | 行为                                                                    |
| --------------------- | ----------------------------------------------------------------------- |
| `per_job`             | 默认。复用此定时器在同一项目范围下的 Task；无项目也支持复用             |
| `per_run`             | 每次触发创建一个新的 Task                                               |
| `task`                | 继续明确指定的本人 Task，必须传非空 `target_task_id`                    |
| `continue_target`     | 保留既有 `session_key` 来源路由，主要供旧入口兼容；不是自动寻找最近会话 |

- 只有 `task` 策略允许非空 `target_task_id`；其他策略必须省略该字段或传 null。
- `task` 策略使用目标 Task 自身的项目、专家配置；定时器的专家字段不覆盖目标身份。若同时传非空 `project_id`，必须与目标 Task 项目一致；省略项目也可以指向已有项目下的 Task。
- `per_job`、`continue_target` 更换专家时，复用 Task 会在空闲后为下一轮应用新选择；已有轮次的身份快照保留。
- 对 `per_job`、`continue_target` 策略，更换或清空项目后，后续触发使用对应项目范围的 Task，原 Task 历史保留；不会把旧 Task 移到另一个项目。切回已使用的范围时可能复用对应历史 Task。
- 新客户端使用 `per_run` 表达“每次新对话”，不要只设置 `fresh_thread: true` 而保留 `per_job`。
- 已受理的一次触发使用当时保存的输入；编辑定时器影响后续触发。

## 返回结构

创建、详情、修改都返回同一个 `CronResponse`。以下 ID 为示意值：

```json
{
  "id": "<cron_id>",
  "name": "每日提醒",
  "agent_id": null,
  "trigger": "cron:0 9 * * *",
  "prompt": "记得安排今天的工作",
  "session_key": "cron:<cron_id>",
  "model": null,
  "fresh_thread": false,
  "enabled": true,
  "skip_confirmation": false,
  "task_type": "text",
  "mcp_servers": [],
  "last_run_at": null,
  "last_status": null,
  "last_error": null,
  "conversation_policy": "per_job",
  "target_task_id": null,
  "project_id": null,
  "selected_expert_id": null
}
```

| 只读字段      | 含义                                                                           |
| ------------- | ------------------------------------------------------------------------------ |
| `id`          | 定时器公开 ID，用于 URL；不是 Task ID                                          |
| `agent_id`    | 旧生命周期兼容字段，新统一入口创建时为 null；专家选择读取 `selected_expert_id` |
| `last_run_at` | 最近执行状态记录的 Unix 时间戳，单位为**秒**；尚未执行时为 null                |
| `last_status` | null 表示尚无执行状态；当前可能为 `queued`、`ok`、`error`，客户端应兼容未知值  |
| `last_error`  | 最近错误信息，通常成功时为 null                                                |

`session_key` 按服务端返回值保存，不根据项目或专家 ID 自行拼接。当前响应没有 `next_run_at`、最近执行的 `task_id` 或 `turn_id`。

## 修改与清空字段

`PATCH /api/cron/{cron_id}` 使用创建字段的子集，省略字段表示保留。

| 字段                                                                                              | 明确传 null 的行为                                                    |
| ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `project_id`                                                                                      | 清除定时器项目关联                                                    |
| `selected_expert_id`                                                                              | 清除专家选择，后续适用轮次使用通用助手；`task` 策略仍由目标 Task 决定 |
| `target_task_id`                                                                                  | 清除目标；需同时保证最终策略不是 `task`                               |
| `model`                                                                                           | 清除显式模型覆盖                                                      |
| `mcp_servers`                                                                                     | 等同 `[]`，清除定时器上的显式 MCP 选择，继承项目/专家默认能力         |
| `name`                                                                                            | 根据最终 prompt 自动生成名称；空字符串也如此                          |
| `trigger`、`prompt`、`session_key`、`fresh_thread`、`enabled`、`skip_confirmation`、`task_type`、`conversation_policy` | 保留原值，不执行清空                                                  |

清空项目和专家：

```json
{
  "project_id": null,
  "selected_expert_id": null
}
```

将已有绑定 Task 的策略改为每次独立执行：

```json
{
  "conversation_policy": "per_run",
  "target_task_id": null
}
```

暂停自动触发：`PATCH /api/cron/{cron_id}`，正文 `{"enabled":false}`。恢复使用 `{"enabled":true}`。

## 立即执行、状态与删除

`POST /api/cron/{cron_id}/run-now` 无需正文，返回 `204` 表示已受理异步执行，**不表示执行已经完成**。请求受理时通常记录为 `queued`；稍后读取详情，最近状态更新为 `ok` 或 `error`。

已停用的定时器也可以手动执行一次，且不会因此启用自动触发。每次手动请求都是独立触发；接口没有客户端幂等键，结果未知时不要直接反复重发。最近状态是作业摘要，不是逐次执行账本。

执行结果保存在统一 Task 历史，并按来源投递。读取历史使用统一 `/api/project-tasks`；`run-now` 当前不返回本次执行收据或 Task ID，不能将定时器 ID 当成 Task ID。

`DELETE /api/cron/{cron_id}` 删除定时配置并停止后续自动调度，已有 Task 历史保留。删除定时器不等同于取消已受理轮次；活动轮次的取消使用统一 Task 控制接口。

## 请求示例

以下 `<access_token>`、`<cron_id>` 需要替换，示例不会自动执行：

```bash
curl 'http://127.0.0.1:8088/api/cron' \
  -H 'Authorization: Bearer <access_token>' \
  -H 'Content-Type: application/json' \
  --data '{"trigger":"cron:0 9 * * *","prompt":"每日计划提醒","task_type":"text","project_id":null,"selected_expert_id":null}'
```

```bash
curl -X POST 'http://127.0.0.1:8088/api/cron/<cron_id>/run-now' \
  -H 'Authorization: Bearer <access_token>'
```

```bash
curl 'http://127.0.0.1:8088/api/cron/<cron_id>' \
  -H 'Authorization: Bearer <access_token>'
```

## 错误响应

领域错误使用统一结构，客户端根据 `code` 判断，不依赖 `message` 的语言或原文：

```json
{
  "error": {
    "code": "TASK_REQUEST_CONFLICT",
    "message": "请求配置冲突",
    "details": {}
  }
}
```

| HTTP | 常见 code / 结构               | 场景                                                                     |
| ---- | ------------------------------ | ------------------------------------------------------------------------ |
| 400  | `CRON_TRIGGER_INVALID`         | 触发格式无效，或邮件触发源未就绪                                         |
| 400  | `CONNECTOR_NOT_BOUND`          | MCP 引用未绑定或当前用户不可用                                           |
| 401  | `AUTH_FAILED`、`TOKEN_EXPIRED` | 缺少有效登录凭据或 Token 过期                                            |
| 403  | `FORBIDDEN`                    | 执行授权不可用等禁止访问场景                                             |
| 404  | `NOT_FOUND`                    | 定时器不存在或属于他人；项目、目标 Task 不存在/无权/已归档，或专家不可用 |
| 409  | `TASK_REQUEST_CONFLICT`        | 策略与目标组合错误，或目标 Task 与明确选择的项目不一致                   |
| 422  | `{"detail":[...]}`             | 请求结构、类型或 `conversation_policy` 枚举校验失败                      |
| 500  | `INTERNAL_ERROR`               | 服务端未处理异常                                                         |

客户端提交前应校验 prompt 非空及长度、name 长度。当前这些内容约束未完整体现在 OpenAPI 中，且部分业务字符串校验失败仍可能返回 `500 INTERNAL_ERROR`，不能统一按 422 处理。

## 旧入口兼容

`/api/agents/{agent_id}/cron` 及其详情、修改、删除、立即执行接口继续保留兼容；创建、PATCH 和返回结构同样支持 `skip_confirmation`。新客户端使用统一入口。

通过旧入口创建时指定项目，或将旧定时器明确关联项目后，服务端会解除旧 `agent_id` 生命周期绑定并生成独立 `cron:{cron_id}` 会话键。`selected_expert_id` 作为可选配置保留，删除原专家不会连带删除该项目定时器。

APP 的其他 Task 接线参见[统一会话交接](agentcomputer-unified-conversations-handoff.md)。本文件描述服务端接口，不代表 APP Swift/UI 已完成接入。
