# APP MCP 与连接器接入

更新日期：2026-10-29。接口核对基线：`release/1.0.0 @ 5c945899c8f2992cf2bef883dafc149ba82fba22`，并包含本轮连接器修复。本文描述已实现的服务端接口，不代表某个部署已升级或真实供应商已通过实连验收；本次不修改 APP Swift/UI。

REST 完整路径均包含 `/api`。运行实例的 `/api/openapi.json`、`/api/docs` 和当前服务端实现是最终契约；Task 实时协议另见[任务 WebSocket](project-task-websocket.md)及其 [JSON Schema](project-task-websocket.schema.json)。示例中的 ID、URL、令牌和时间均为占位值；标为“节选”的响应不能直接当成完整 DTO。

## 1. 接入对象与公共约定

| 对象 | 标识 | APP 用法 |
| --- | --- | --- |
| 内置连接器类型 | `kind`，如 `tencent-docs`、`notion` | 从目录发现授权方式和凭证字段；同一种类型可以建多个连接。 |
| 已保存连接 | `instance_id` | 详情、测试、编辑、删除、授权管理。 |
| 可供模型使用的 MCP | 连接列表的 `mcp_server_name`，资源目录的 `server_name` | 原样提交给项目或本轮任务，不提交凭证、URL 或命令。 |
| 自定义 MCP 配置 | `servers` 对象的键，如 `my_docs` | 用于自定义配置读写及 OAuth 的 `server_name`。 |

不要把 `instance_id`、`kind` 或显示名当成 MCP 引用。本人自定义 MCP 的引用通常就是配置键；他人共享的自定义 MCP 使用服务端生成的防冲突引用，如 `custom__parent_id__my_docs`，其列表 ID 又是 `custom:parent_id:my_docs`。APP 不拼装这些值，分别保存服务端返回的 ID 和引用。

```http
Authorization: Bearer <octop_access_token>
Accept-Language: zh
Content-Type: application/json
```

`Content-Type` 用于 JSON 请求。除供应商 OAuth 回调和内部 MCP 协议外，本文 REST 均需 Octop 登录令牌。语言按现有账号偏好／请求语言规则解析；时间显示使用 `GET /api/settings/timezone` 返回的服务端时区。连接器的 `created_at`、`updated_at`、OAuth `expires_at` 是 Unix **秒**；Task 时间使用其原有 ISO 8601 契约。

连接、项目与 Task 的资源引用应来自同一目标 Octop 主机和账号；APP 按服务地址／host 与用户隔离缓存，不能把本机返回的 server_name 当成远端授权。远端接线继续使用[统一会话交接](agentcomputer-unified-conversations-handoff.md)中的 Bridge transport。

成功响应直接返回对象或数组，没有统一 `data` 包装；删除成功为 `204`，无响应体。APP 响应 DTO 应容忍新增字段和可选字段缺失。管理页面按 `can_manage` 控制入口；共享可用不等于可管理，实例详情／编辑／测试／删除一般要求所有者或管理员，刷新 OAuth 和实例设备授权只允许所有者。

连接器 `default_open` 是账号／入口默认选择设置，**不会自动叠加到统一 Task**。统一 Task 的默认合并与逐轮覆盖规则见第 6 节。

## 2. 目录、连接列表与实例管理

### 2.1 获取连接器目录

`GET /api/connectors/catalog`，无请求参数，返回 `200` 数组。使用 `phase="available"` 判断可创建；不要把 `supports_quick_auth=true` 当成浏览器 OAuth 已配置，应同时读取 `auth_kind`、`oauth_mode`、`oauth_ready`。

响应节选：

```json
[
  {
    "kind": "notion",
    "name": "Notion",
    "description": "连接 Notion 工作空间",
    "auth_kind": "oauth2",
    "mcp_mode": "remote",
    "phase": "available",
    "category": "knowledge",
    "oauth_mode": "dynamic",
    "oauth_ready": true,
    "supports_quick_auth": true,
    "credential_fields": []
  }
]
```

目录还包含 `doc_url`、`icon`、`color`、`quick_auth_url`、`login_url`、`guide_url`、`manual_url`、`auth_hint`。`credential_fields[]` 包含 `key`、`label`、`field_type`、`required`、`placeholder`、`help`、`secret`；`field_type` 可为 `text/password/url/tags`。展示文案已本地化。供应商表单优先遵循返回元数据，具体字段以第 2.4 节和服务端校验为准。

`mcp_mode` 是服务端实现方式，可为 `remote`、`gateway`、`internal` 或 `stdio`；APP 均使用相同的连接管理和 Task 选择接口，不自行连接供应商 MCP。

### 2.2 列出已连接账号和自定义 MCP

`GET /api/connector-instances`，无必需查询参数或专家请求头，返回 `200` 数组，包含本人连接与有权使用的共享连接；自定义 MCP 按 server 展开。

```json
[
  {
    "instance_id": "conn_docs_01",
    "kind": "tencent-docs",
    "display_name": "工作文档",
    "description": "团队资料",
    "status": "active",
    "mcp_server_name": "tencent-docs__conn_docs_01",
    "has_credentials": true,
    "default_open": false,
    "shared": false,
    "owner_user_id": 1,
    "created_at": 1793059200,
    "updated_at": 1793059200,
    "owner_username": "demo",
    "owner_display_name": "示例用户",
    "can_manage": true
  }
]
```

`description` 在部分条目中可缺失或为 null。自定义条目 `kind="custom-mcp"`、`instance_id="custom:my_docs"`、`mcp_server_name="my_docs"`；其 `has_credentials=true` 不表示已经完成 OAuth，也不表示健康检测通过。

### 2.3 新建、读取、修改、删除

| 方法与路径 | 请求 | 成功响应 |
| --- | --- | --- |
| `POST /api/connector-instances` | 第 2.4 节字段 | `201` 连接对象 |
| `GET /api/connector-instances/{instance_id}` | 路径 ID | `200` 连接对象，另含 `config`、`credentials_preview` |
| `PATCH /api/connector-instances/{instance_id}` | 修改字段，至少一个有效字段 | `200` 连接对象 |
| `DELETE /api/connector-instances/{instance_id}` | 路径 ID，无 body | `204` |

创建和普通 PATCH 的响应是上节连接对象的基础字段，不包含列表补充的 `owner_username/owner_display_name/can_manage`，也不包含详情的 `config/credentials_preview`。创建成功后刷新列表。

连接创建没有公开的 request_id／Idempotency-Key 合约；响应不明时先刷新列表核对，不套用项目创建的幂等头自动重复创建。

创建示例：

```http
POST /api/connector-instances
```

```json
{
  "kind": "tencent-docs",
  "display_name": "工作文档",
  "description": "团队资料",
  "credentials": {"token": "<supplier_token>"},
  "shared": false,
  "default_open": false
}
```

详情的附加字段示例：

```json
{
  "config": {"description": "团队资料"},
  "credentials_preview": {"token_configured": true}
}
```

`credentials_preview` 随类型变化，可能有 `oauth_configured`、`api_key_configured`、`expires_at`、`app_id` 等；不返回普通连接的 token、密码或 App Secret。编辑保留原 `instance_id`：

```json
{
  "display_name": "工作资料",
  "description": "更新后的用途",
  "status": "active",
  "credentials": {"token": "<new_supplier_token>"},
  "shared": false,
  "default_open": false
}
```

PATCH 可省略任意未修改字段；`status` 仅接受 `active/disabled`；`kind` 不可改。`credentials` 按类型与已存凭据合并，一般不会用空字符串／null 擦除旧密钥；不要用空字段表达断开授权。名称、简介不能改为空白。断开使用 DELETE；部分供应商会先撤销授权，失败时可能拒绝删除，APP 应显示实际错误并刷新列表。

自定义 MCP 不能通过 POST 实例接口创建，也不能通过普通实例详情读取配置。其配置使用第 3 节接口；列表返回的自定义 `instance_id` 可用于 DELETE、`/test` 和状态类 PATCH。自定义的实例 PATCH 只使用 `status/default_open/shared`，编辑 URL、命令或显示名仍需全量保存配置。

### 2.4 请求字段与凭证

| 创建字段 | 类型／默认 | 约束 |
| --- | --- | --- |
| `kind` | string，必填 | 目录中可用的内置类型；不接受 `custom-mcp`。 |
| `display_name` | string，必填 | 去空白后非空；账号内与内置／自定义连接的显示名均不可重复。 |
| `description` | string，可选 | 最多 500 字符；省略/null 时取目录本地化简介，最终不得为空。 |
| `credentials` | object，默认 `{}` | 按类型校验，见下表。 |
| `shared` | boolean，默认 false | 共享连接供其他用户选择，不授予管理权限。 |
| `default_open` | boolean，默认 false | 账号入口默认设置，不改写项目／统一 Task 选择。 |

PATCH 支持上述除 `kind` 外的字段，以及 `status`；省略表示不改。当前常用凭证如下，参数属于执行 Octop 的主机，不能用 APP 本地路径代替服务器路径。

| 类型／连接器 | `credentials` |
| --- | --- |
| `personal_token` | `{"token":"..."}`，兼容 `access_token`；如腾讯文档、有道云笔记、语雀。 |
| `github` | `{"token":"<GitHub PAT>"}`，支持细粒度或经典 Personal Access Token；经官方远程 MCP 使用 Bearer 鉴权。创建、权限、探测和项目／任务示例见 [GitHub 连接器](connectors-github.md)。 |
| `oauth2` | 使用第 4 节授权结果的完整 `tokens` 对象作为 credentials；包含 `access_token`，按供应商还可能有 `refresh_token/expires_at/oauth_client_id/oauth_client_secret/openid`。 |
| 企查查 `qcc` | 浏览器 OAuth；另兼容 `{"api_key":"..."}`，详见[企查查说明](connectors-qcc.md)。 |
| 普通 `api_key` | `{"api_key":"..."}`。腾讯 IMA 还需 `client_id`，腾讯乐享还需 `company_from`（兼容 `client_id`）。 |
| `feishu-cli` | `app_id`、`app_secret` 必填；可选 `cli_config_key`、`default_as`，用户身份登录见第 5 节。 |
| `wecom-cli` | `bot_id`、`bot_secret` 必填。 |
| `qq-mail` | `email`、`password` 必填；可选 `mail_provider`（qq/gmail/netease）或自定义 `imap_host/imap_port/smtp_host/smtp_port`；识别到邮箱域时采用对应预设。 |
| `agently-cli` | `{}`；创建后走实例设备授权，隔离凭据目录由服务端管理。 |
| `dingtalk-cli` | `{}`，或成对的 `client_id/client_secret`；创建后走实例设备授权。 |
| `obsidian-cli` | `vault` 必填；可选 `binary_path`，均指 Octop 主机上的配置。 |
| `weknora` | `base_url` 必填；可选 `api_key/tenant_id/knowledge_base_ids`，后者接受数组或逗号字符串。 |
| `dify` | `mcp_url` 必填，须含 `/mcp/server/` 且以 `/mcp` 结尾。 |

### 2.5 测试连接与刷新

`POST /api/connectors/test-credentials` 接受 `{"kind":"tencent-docs","credentials":{"token":"..."}}`，测试尚未保存的凭证；**实际方法是 POST**。

`POST /api/connector-instances/{instance_id}/test` 无 body，测试已有连接；两者成功处理请求返回 `200`，测试失败也可能是 `200` 配合 `ok=false`，必须检查 body。

```json
{
  "ok": true,
  "tool_count": 1,
  "tools": [{"name": "search_documents", "description": "检索文档"}]
}
```

```json
{
  "ok": false,
  "error_type": "auth",
  "error": "需要重新授权",
  "status_code": 401,
  "oauth": {"available": true, "issuer": "https://auth.example.com", "resource": "https://mcp.example.com/mcp"}
}
```

`tools/tool_count/error_type/status_code/oauth` 均为可选；`error_type` 可能是 `auth/connection/validation`，不保证每种连接都提供。`tools` 只有名称和描述，不是可直接调用的完整工具 Schema，也不是 APP 调用工具的授权。

`POST /api/connector-instances/{instance_id}/refresh` 无 body，仅所有者可用，返回 `{"ok":true,"expires_at":1793062800}`（时间可为 null）。这是服务端的“按需确保凭证新鲜”，并非保证每次强制刷新；多数 OAuth 类型在刷新失败时仍可能返回旧凭据，`ok=true` 不能证明授权有效。它不适用于自定义 MCP 的合成实例 ID。自定义 OAuth 由服务端加载时自动刷新，失效后重新走授权。

## 3. 自定义 MCP 配置

### 3.1 读取与全量保存

`GET /api/connectors/custom-mcp` 返回当前用户自己的配置，默认 `{"servers":{}}`；不返回他人共享配置。`PUT /api/connectors/custom-mcp` 接受相同包装，返回 `200 {"servers":{...}}`，保存后安排连接重载。

```json
{
  "servers": {
    "my_docs": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer <supplier_token>"},
      "enabled": true,
      "display_name": "我的文档 MCP",
      "default_open": false,
      "shared": false
    },
    "local_tools": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["example-mcp-server"],
      "env": {"EXAMPLE_API_KEY": "<supplier_key>"},
      "enabled": true,
      "display_name": "主机工具"
    }
  }
}
```

上例说明请求结构，不表示服务端已安装示例程序。`stdio` 测试／执行会在 **Octop 主机** 启动进程；`command/args/env` 不在手机执行。HTTP MCP 的 `localhost`／局域网地址也是从 Octop 主机访问。

| 字段 | 约束 |
| --- | --- |
| `servers` | object，默认 `{}`；**替换整个本人配置集合**。漏掉的 server 被删除，`{}` 清空全部。无配置版本／CAS 字段，APP 保存前刷新并明确合并其他配置。 |
| 配置键 | 仅字母、数字、`_`、`-`；非空，不能与保留的内置 MCP 引用冲突。 |
| `transport` | 必填，`streamable_http/stdio`；兼容输入 `http`，返回规范化为 `streamable_http`。不接受 `sse`。 |
| `url` | HTTP 必填；公网必须 HTTPS，loopback／局域网允许 HTTP 或 HTTPS。 |
| `headers` | HTTP 可选，string → string 对象。 |
| `command` | stdio 必填，非空字符串。 |
| `args` | stdio 可选，推荐 string[]；省略视为空，兼容按行字符串。 |
| `env` | stdio 可选，string → string 对象。 |
| `enabled` | 默认 true。 |
| `display_name` | 可选，最多 64 字符；省略使用配置键，显示名在账号内不能重名。 |
| `default_open/shared` | 默认 false，含义同连接器入口默认／共享选择。 |
| `oauth` | 服务端管理的授权预览；APP 不写入 access_token 等 OAuth 内容，PUT 会忽略客户端 oauth 并按同名键保留已存授权。 |

GET/PUT 的响应与保存后的规范化配置同形，但 OAuth 只返回预览，例如：

```json
{
  "servers": {
    "my_docs": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/mcp",
      "enabled": true,
      "oauth": {"configured": true, "expires_at": 1793062800}
    }
  }
}
```

尚需授权时预览可为 `{"configured":false,"required":true}`，未启用 OAuth 时可无 `oauth`。当前实现仅隐藏 OAuth token，**自定义 headers/env 中手填的密钥仍可能原样返回**；这些响应和 OAuth 结果都按敏感数据处理，不写分析日志或跨账号缓存。更改配置键不会迁移旧键的 OAuth，APP 应作为删除旧项／新建新项处理并重新授权。

### 3.2 单项状态、删除与测试

`PATCH /api/connectors/custom-mcp/servers/{server_name}` 的路径使用本人配置键，并 URL 编码；body 至少包含一个非 null 的 `enabled/default_open/shared`：

```json
{"enabled": true, "default_open": false, "shared": false}
```

返回整个 `{"servers":{...}}` 集合。仅停用且未同时重设 `default_open` 时，服务端会清除该项默认开启标记。修改 URL、headers、命令、参数或显示名使用 PUT；删除单项可从最新集合移除后 PUT，也可对列表返回的 `instance_id` 调用 `DELETE /api/connector-instances/{instance_id}`。

`POST /api/connectors/custom-mcp/test` 二选一：

```json
{"name": "my_docs"}
```

```json
{"server": {"transport": "streamable_http", "url": "https://mcp.example.com/mcp"}}
```

`name` 使用已存本人配置及其 OAuth；`server` 测试临时配置且不保存，同时提供时优先 `server`，APP 应只提交其中一种。响应沿用第 2.5 节 ProbeResult；内联校验失败也可返回 `200 {"ok":false,"error_type":"validation","error_code":"CONNECTOR_MCP_HTTPS_REQUIRED","error":"..."}`。按名称探测还可能更新该配置的 `oauth.required` 标记。

## 4. 浏览器 OAuth 与手动授权

### 4.1 APP 的浏览器 OAuth 流程

1. 内置连接器先读取目录，确认 `oauth_ready`；自定义 MCP 先保存配置并测试，确认 `oauth.available=true`。
2. 调用统一 start，保存 `state_id`，在系统浏览器打开 `authorize_url`。Octop 服务端负责 state、PKCE、动态客户端注册和换 token。
3. 供应商回调 **Octop 服务端**的 `/api/connectors/oauth/callback`。APP 不自行调用该回调换 token。
4. APP 回到前台或用户完成浏览器授权后，用原 Octop 账号读取 pending；也可有限轮询。结果属于该账号，首次成功读取即被消费。
5. 内置连接器：将返回的完整 `tokens` 作为 `credentials` 创建连接，或 PATCH 当前待重新授权实例。自定义 MCP：回调已保存授权，`applied=true` 时直接刷新配置和资源目录，不再创建实例。
6. 测试连接，刷新项目／Task 资源，再让用户选择。

`POST /api/connectors/oauth/start`：

```json
{"target": {"type": "catalog", "kind": "notion"}, "redirect_after": "/connectors"}
```

自定义 MCP 改为：

```json
{"target": {"type": "custom_mcp", "server_name": "my_docs"}, "redirect_after": "/connectors"}
```

`target` 必填，`redirect_after` 可省略/null。成功为 `200`：

```json
{"authorize_url": "https://auth.example.com/authorize?state=example", "state_id": "oauth_state_01"}
```

`redirect_after` 仅支持站内 `/...` 路径，不支持 APP 自定义 scheme、Universal Link 绝对 URL 或外站回跳；无效值回退 `/connectors`。回调页面在网页 popup 场景用 `postMessage` 通知 opener，否则转到带 `oauth_state=<state_id>` 的站内页面。**这不构成原生 APP 的完成回调**，APP 应保留 state_id 并读 pending。浏览器不需要携带 Octop Bearer 给供应商。

`GET /api/connectors/oauth/pending/{state_id}`，无 body，内置连接器结果示例：

```json
{
  "kind": "notion",
  "tokens": {
    "access_token": "<supplier_access_token>",
    "refresh_token": "<supplier_refresh_token>",
    "expires_at": 1793062800,
    "oauth_client_id": "<registered_client_id>"
  },
  "server_name": null,
  "applied": null
}
```

自定义结果示例：

```json
{"kind":"custom-mcp","tokens":{"access_token":"<supplier_access_token>"},"server_name":"my_docs","applied":true}
```

两种响应都可能带敏感 token，APP 不记录 token。当前 pending **没有 `pending/failed/expired` 状态对象**：未就绪、已消费或不存在均可能 `404 NOT_FOUND`；不能把每个 404 都显示成授权失败，也不能无限等待。APP 自行设置等待时限并允许重新发起；浏览器回调失败返回 HTML 错误，不会产生 pending 成功结果。首次 `200` 立即停止轮询并保留结果至创建／更新连接完成；若 HTTP 响应丢失，读取结果可能已经被消费，需刷新自定义配置或重新发起内置授权，不能假定可以重读。

OAuth 重新授权不会根据 kind 自动匹配／覆盖已有内置实例。APP 必须保留正在编辑的 instance_id，成功后 PATCH 该实例；新连接才 POST，避免重复账号。

### 4.2 可用范围与部署要求

- 自定义 OAuth 仅用于公网 HTTPS 的 `streamable_http` MCP，需要能发现授权元数据并支持动态客户端注册（DCR）。本地／局域网 MCP 或明确不支持 DCR 的授权目标返回 `400 CONNECTOR_INVALID_CREDENTIALS`，`details.reason` 提供本地化原因；APP 改为引导填写 Token／headers 或服务自身支持的授权方式。
- 要求 HTTPS 回调的目标在非 loopback HTTP 部署上会返回 `CONNECTOR_OAUTH_HTTPS_REQUIRED`。回调地址来自服务端公开基址／反向代理配置；APP 不传 redirect_uri。完成授权的系统浏览器必须能访问该回调地址，且地址还需符合供应商注册／HTTPS 要求；不要求供应商服务器主动访问 Octop。
- 自定义 MCP 的 OAuth token 由服务端保存和刷新，项目／Task 只提交引用。对 `stdio` 发起此 OAuth 会被拒绝。
- 兼容旧接口 `POST /api/connectors/oauth/{kind}/start`，body 只有可选 `redirect_after`；新 APP 统一使用带 target 的 start。

### 4.3 授权说明与粘贴授权码

| 接口 | 参数 | `200` 响应 |
| --- | --- | --- |
| `GET /api/connectors/auth/{kind}/info` | 路径 kind | `{authorize_url,login_url,guide_url,manual_url,auth_hint}`，各值可 null。 |
| `GET /api/connectors/auth/{kind}/authorize-url` | 路径 kind | `{"authorize_url":null}` 或 URL；这是授权说明入口，不替代统一 OAuth start。 |
| `POST /api/connectors/auth/{kind}/exchange-code` | body：必填 `code`；可选 `bkn`、`knowledge_base_id` | `{"credentials":{...}}`，随后用于创建／更新连接。 |

例如交换请求 `{"code":"<pasted_code>"}`，响应可为 `{"credentials":{"cookie":"<supplier_cookie>"}}`，具体取决于连接器。exchange-code 仅支持 `auth_kind="auth_code"` 的类型；当前目录不能仅凭接口存在就展示此按钮，应按目录实际授权方式分流。Token/API Key 类型直接按第 2 节提交凭证。

## 5. CLI 连接器和设备授权

CLI 都安装和运行在 Octop 主机，APP 只调用 HTTP、打开授权 URL、展示用户码／状态。系统浏览器完成授权后不需要把命令行凭据拷到手机。

### 5.1 安装状态

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/connectors/{kind}/cli-status` | 路径 kind，无 body | `200` 安装状态。 |
| `POST /api/connectors/{kind}/install-cli` | 路径 kind，无 body | `200` 安装结果；失败也可能 `ok=false`。 |

安装响应节选：

```json
{"ok":true,"kind":"feishu-cli","installed":true,"binary":"lark-cli","version":"example","install_command":"npm install -g <package>","doc_url":"https://example.com/docs"}
```

其余可选字段有 `already_installed/npm_package/binary_path/guide_url/error`。状态查询返回这些安装信息，但不保证包含 `ok`；是否已安装读取 `installed`，安装结果才检查 `ok`。安装操作受 `require_permission("connectors")` 检查，不能仅根据路由 summary 中的 admin 字样自行推断权限；以服务端 `403` 为准。状态查询普通登录用户可用；支持 agently-cli、feishu-cli、wecom-cli、dingtalk-cli、obsidian-cli，不支持的 kind 会被拒绝。Obsidian 使用应用自带 CLI 注册，npm_package 可为空。

### 5.2 Agent Mail／钉钉设备授权

先创建 `agently-cli` 或 `dingtalk-cli` 实例，再调用以下仅所有者可用的接口：

| 方法与路径 | 请求 | 作用 |
| --- | --- | --- |
| `POST /api/connector-instances/{instance_id}/agently-auth/start` | 无 body | 启动设备授权。 |
| `GET /api/connector-instances/{instance_id}/agently-auth/status` | 无 body | 获取待授权／已授权状态。 |
| `POST /api/connector-instances/{instance_id}/agently-auth/logout` | 无 body | 取消待授权并清除／尝试撤销授权，保留实例。 |
| `POST /api/connector-instances/{instance_id}/agently-auth/refresh` | 无 body | 刷新授权并返回状态。 |

均返回 `200`：

```json
{"status":"pending","verification_url":"https://auth.example.com/device","user_code":"ABCD-EFGH","expires_at":1793062800,"error":null}
```

`status` 为 `idle/pending/authorized/expired/error`；其余字段可 null。start 初次可返回 pending 且 URL 仍为 null，继续读 status，取得 verification_url 后再打开并展示 user_code；达到 authorized、expired 或 error 后停止等待。该状态模型与第 4 节一次性 OAuth pending 不同。Agent Mail 的目录与行为详见[专门说明](connectors-agently-mail.md)。

### 5.3 飞书用户身份授权

已有 `feishu-cli` 实例优先使用实例接口，App Secret 留在服务端：

| 方法与路径 | JSON body | 响应 |
| --- | --- | --- |
| `POST /api/connector-instances/{instance_id}/feishu-user-auth/start` | 无 body | 设备授权开始对象。 |
| `POST /api/connector-instances/{instance_id}/feishu-user-auth/complete` | 必填 `device_code`；可选 `cli_config_key` | 完成对象。 |

开始对象示例：

```json
{"device_code":"<device_code>","verification_url":"https://auth.example.com/device","expires_in":600,"user_code":"ABCD-EFGH","hint":null,"cli_config_key":"instance_key"}
```

APP 打开 verification_url 后，用保存的 device_code 和原 cli_config_key 调用 complete；响应示例：

```json
{"ok":true,"identity":"user","default_as":"user","user_available":true,"bot_available":true,"search_docs_scope":true,"warning":null,"cli_config_key":"instance_key"}
```

这些可选状态字段随 CLI 返回，不把缺失当成 false；另可能返回结构不稳定的 `auth_status`，APP 不依赖其内部键。缺少文档搜索权限时可能成功但带 warning。complete 可能等待 CLI 约 120 秒，应使用适当请求超时；完成后刷新实例详情，可见 `credentials_preview.user_auth_valid/user_auth_needs_reauth/user_token_status` 等状态。

未保存实例时另有 `POST /api/connectors/feishu-cli/user-auth/start`（必填 app_id/app_secret，可选 cli_config_key/domains，domains 为字符串数组）及 `POST /api/connectors/feishu-cli/user-auth/complete`（必填 app_id/app_secret/device_code，**还必须回传 start 的 cli_config_key**；请求模型虽将它标为可选，领域校验要求非空），响应同形。保存前 complete 不创建连接，成功后仍需创建实例并带回 app_id/app_secret/cli_config_key/default_as:"user"。实例接口要求所有者；通用接口需登录，APP 新接线优先创建实例后授权。

### 5.4 本机 WeKnora 检测

`GET /api/connectors/weknora/detect-local` 无参数，返回 `{"found":false}`，或 `{"found":true,"base_url":"http://127.0.0.1:8080/api/v1","console_url":"http://127.0.0.1"}`。检测的是 Octop 主机固定 loopback 地址，不是 APP 所在设备；返回的 console_url 也不保证手机可以直接访问。不保存连接，仍需 POST 实例。

## 6. 在项目和任务中选择 MCP

### 6.1 获取候选资源

`GET /api/projects/resources` 和 `GET /api/project-tasks/resources` 均无必需查询参数，响应 `Cache-Control: no-store`。前者用于项目配置，后者另有模型、专家、执行引擎、审批等任务资源。两者 MCP 响应节选相同：

```json
{
  "mcp_servers": [
    {"server_name":"tencent-docs__conn_docs_01","name":"工作文档","state":"configured"},
    {"server_name":"my_docs","name":"我的文档 MCP","state":"configured"},
    {"server_name":"notion__conn_notion_01","name":"待授权 Notion","state":"authorization_required"},
    {"server_name":"old_tools","name":"已停用工具","state":"disabled"}
  ]
}
```

| state | APP 展示／处理 |
| --- | --- |
| `configured` | 存在配置／凭据标识，可选；不代表已探测或能连通。 |
| `authorization_required` | 引导本人在连接管理中补授权；共享连接无权管理时提示由所有者处理。 |
| `disabled` | 展示停用状态，避免当作可执行资源。 |

项目可保存仍在目录中的停用／待授权引用，但执行时可能因无法加载而失败；APP 应在发送前提示状态并提供连接管理入口。资源变更后刷新，不把资源列表长期缓存为授权依据。

自定义 MCP 的列表条目当前固定带 has_credentials=true，因此即使 OAuth 待授权，资源目录也可能仍返回 configured。本人自定义授权状态需结合 `GET /api/connectors/custom-mcp` 的 oauth.required/configured 及测试结果判断，不能只依赖资源 state；共享使用者没有他人的配置读取权。

### 6.2 项目默认 MCP

创建项目使用 `POST /api/projects`，必须提供 `Idempotency-Key`（8–128 位字母、数字、`_` 或 `-`）；body 的 name 必填，MCP 默认空数组：

```http
POST /api/projects
Idempotency-Key: app-project-000001
```

```json
{"name":"资料项目","mcp_servers":["tencent-docs__conn_docs_01","my_docs"]}
```

成功 `201` ProjectView，响应节选：

```json
{"project_id":"proj_example","name":"资料项目","revision":1,"mcp_servers":["tencent-docs__conn_docs_01","my_docs"]}
```

修改已有项目使用 `PATCH /api/projects/{project_id}`，传实际读到的 revision：

```json
{"expected_revision":1,"mcp_servers":["tencent-docs__conn_docs_01"]}
```

成功 `200` ProjectView，revision 加一。mcp_servers 最多 64 项，每项非空、最多 256 字符、不可重复；必须是当前用户可见引用。PATCH 省略不改、`[]` 清空、`null` 被拒绝。项目只保存引用，不保存连接密钥。

`PUT /api/projects/{project_id}` 是完整替换，需要 expected_revision 和全部当前项目字段（name/description/directory/client_directory/instructions/skills/mcp_servers/plugins/subagents/memory_enabled/team_enabled）；仅改 MCP 使用 PATCH，不能发送不完整 PUT。其他项目字段见[统一会话交接](agentcomputer-unified-conversations-handoff.md)。项目创建／修改不运行工具，也不创建或修改专家。

### 6.3 本轮 MCP 三态与继承

| `options.mcp_servers` | 实际选择 |
| --- | --- |
| 省略或 `null` | 合并入队时**当前所选专家默认 MCP + 当前项目默认 MCP**并去重。无专家只继承项目，无项目仍可继承专家，两者均无则为空。共享专家默认值仍受当前调用用户可见性校验。 |
| `[]` | 本轮禁用所有 MCP，不继承默认。 |
| `["server_name", ...]` | 完整替换本轮选择，不与专家、项目或上一轮合并；最多 64 项，不可重复。 |

`options` 本身只能省略或为 object，不能传 null。引用选择只影响本轮，不写回项目或专家；下一轮重新解析默认，也不会自动沿用上一轮显式数组。连接器 `default_open` 不参与统一 Task 的这次合并。

专家默认 MCP 来自已有专家配置，`POST /api/agents` 或 `PATCH /api/agents/{agent_id}` 的 mcp_servers 可管理该默认集合（修改受专家所有权限制）。APP 在项目或本轮选择 MCP 时直接保存引用，不需要改专家配置。

入队冻结的是引用选择、项目版本及本轮配置；并不复制／冻结供应商密钥。执行时仍使用服务端当前凭据、共享权限及启用状态，并按需刷新 OAuth。历史以 `turns[].options.mcp_servers` 展示实际选择，不能从 APP 当前选择器重建历史。

### 6.4 REST 创建、追问与重试

| 接口 | JSON body | 成功 |
| --- | --- | --- |
| `POST /api/project-tasks` | request_id、project_id（可 null）、prompt／attachment_ids、options 等 | `202 TaskDetail` |
| `POST /api/projects/{project_id}/tasks` | 同创建，但 body 不含 project_id | `202 TaskDetail` |
| `POST /api/project-tasks/{task_id}/messages` | request_id、prompt／attachment_ids、版本、options | `202 TaskDetail` |
| `POST /api/project-tasks/{task_id}/turns/{turn_id}/retry` | 新 request_id、版本、options；不传 prompt／附件 | `202 TaskDetail` |

request_id 必填，8–128 位字母、数字、`_` 或 `-`。创建／追问至少有非空 prompt 或附件；prompt 去首尾空白后最多 32768 字符。示例创建无专家、项目内 Task，并为首轮显式只选工作文档：

```json
{
  "request_id":"app-create-mcp-0001",
  "project_id":"proj_example",
  "selected_expert_id":null,
  "expected_configuration_revision":1,
  "expected_project_revision":2,
  "prompt":"检索项目相关文档并给出摘要",
  "options":{"mcp_servers":["tencent-docs__conn_docs_01"]}
}
```

追问时沿用 task_id，本轮禁用 MCP：

```json
{
  "request_id":"app-message-mcp-0002",
  "expected_configuration_revision":1,
  "expected_project_revision":2,
  "prompt":"仅根据已有结果继续总结",
  "options":{"mcp_servers":[]}
}
```

若希望改回继承，下一轮省略 mcp_servers 或提交 `{"options":{"mcp_servers":null}}`，同时保留该轮其他必需字段。无项目使用 `POST /api/project-tasks` 且 project_id 省略/null，同样能显式选择本人可见 MCP。

TaskDetail 响应节选：

```json
{
  "task_id":"task_example",
  "project_id":"proj_example",
  "configuration_revision":1,
  "turns":[{
    "turn_id":"turn_example",
    "status":"queued",
    "project_revision":2,
    "options":{"mcp_servers":["tencent-docs__conn_docs_01"]}
  }]
}
```

客户端应提交已读到的 `expected_configuration_revision` 和（有项目时）`expected_project_revision`。配置版本省略/null 按 1 校验，不自动采用最新；项目版本省略/null 不启用项目 CAS。冲突后刷新，让用户核对草稿，不能静默升级版本重发。

网络重传保留原 request_id、原 body、原版本及 options 的省略/null/数组形态；不要用响应中解析出的实际 options 替换原请求，否则可能 `TASK_REQUEST_CONFLICT`。用户明确重试最后一个 failed/cancelled 轮次时才使用 retry 和新的 request_id；它保留原 prompt／附件并采用重试时的默认或显式 MCP，可再次执行工具。

### 6.5 WebSocket 同样提交选择

APP 使用 `/api/project-tasks/ws`，原生客户端优先在升级请求中设置 `Authorization: Bearer <octop_access_token>`，等待 `{"type":"ready","protocol_version":3}` 后发送命令，不再发送 authenticate。无法设置请求头时，才在连接打开后 10 秒内发送 `{"type":"authenticate","token":"<octop_access_token>"}` 并等待 ready；令牌不放 URL。完整连接、订阅和事件顺序见[WS 协议](project-task-websocket.md)。以下是与 REST 同义的完整消息示例：

```json
{"type":"task.create","id":"cmd-mcp-create-0001","data":{"request_id":"app-create-mcp-0001","project_id":"proj_example","selected_expert_id":null,"expected_configuration_revision":1,"expected_project_revision":2,"prompt":"检索项目相关文档并给出摘要","options":{"mcp_servers":["tencent-docs__conn_docs_01"]}}}
```

```json
{"type":"task.message","id":"cmd-mcp-message-0002","task_id":"task_example","data":{"request_id":"app-message-mcp-0002","expected_configuration_revision":1,"expected_project_revision":2,"prompt":"仅根据已有结果继续总结","options":{"mcp_servers":[]}}}
```

```json
{"type":"task.retry","id":"cmd-mcp-retry-0003","task_id":"task_example","turn_id":"turn_failed","data":{"request_id":"app-retry-mcp-0003","expected_configuration_revision":1,"expected_project_revision":2,"options":{"mcp_servers":null}}}
```

回执为 `{"type":"result","id":"<command_id>","data":<TaskDetail>}`；这里的 `<TaskDetail>` 是结构说明，不是 JSON 字面量。WS 顶层 id 匹配命令，业务 request_id 保证幂等，两者分开保存；断线重传可用新命令 id，但保留原业务 request_id 与原 data。

读取实际 MCP：REST 和 WS result/snapshot 的 `data.turns[].options.mcp_servers`（REST 无 data 包装），以及 WS turn 更新的 `data.turn.options.mcp_servers`。工具进度沿用 Task 公开事件，不向 APP 暴露连接密钥。

## 7. 错误处理与 APP 验收

业务错误通常是以下 JSON。Pydantic 请求校验失败（如重复引用、字段类型不符或必填字段缺失）返回 `422`，使用 FastAPI 的 `detail[]` 结构，APP 应兼容。

```json
{"error":{"code":"CONNECTOR_NAME_TAKEN","message":"连接名称已存在","details":{}}}
```

| HTTP／错误码 | 处理 |
| --- | --- |
| 401／认证错误 | 恢复 Octop 登录；与探测结果 body 中供应商的 status_code=401 区分。 |
| 403 `FORBIDDEN` | 无管理权；共享连接可用性与管理权分开显示。 |
| 404 `CONNECTOR_NOT_FOUND` | 刷新连接和资源，处理已删除引用。 |
| 409 `CONNECTOR_NAME_TAKEN` | 提示修改账号内显示名。 |
| 400 `CONNECTOR_INVALID_CREDENTIALS` | 根据 details／message 修正凭证或完成授权。 |
| 400 `CONNECTOR_KIND_UNSUPPORTED` | 按当前目录隐藏不支持的操作。 |
| 400 `CONNECTOR_OAUTH_HTTPS_REQUIRED` | 修正服务端公开 HTTPS 回调配置。 |
| 502 `CONNECTOR_OAUTH_START_FAILED` | OAuth 启动失败；检查可选的 `details.reason` 与 `details.upstream_status`，修复服务器网络、代理或授权服务配置后重新 start；不能据此判定 PAT 无效或清空已保存凭据。 |
| 400 `CONNECTOR_MCP_HTTPS_REQUIRED/CONNECTOR_MCP_URL_INVALID` | 修正自定义 URL；测试接口也可能在 ok=false 的 body 中返回 error_code。 |
| 422 `PROJECT_RESOURCE_INVALID` | 未知或无权使用的 MCP 引用，刷新候选后重新选择。 |
| 422 `detail[]` | 重复引用、字段类型不符或缺少必填字段等请求校验错误；根据 `detail[].loc/msg/type` 修正请求。 |
| 409 `PROJECT_REVISION_CONFLICT/TASK_REVISION_CONFLICT` | 刷新项目／Task 并由用户核对待发送内容。 |
| 409 `TASK_REQUEST_CONFLICT` | 同 request_id 的请求内容变化，恢复原请求或明确形成新的发送意图。 |
| Task 执行阶段 `TASK_RESOURCE_UNAVAILABLE` | 已入队引用执行时不可加载，检查停用、凭据、共享权限与连接健康；按任务诊断／重试协议处理。 |

OAuth start 的 `details.reason` 可提供网络错误类型、地址校验原因、无效授权响应说明或上游状态；`details.upstream_status` 仅在上游 HTTP 错误时出现。这些字段可能缺失，不应作为固定错误枚举。例如：

```json
{"error":{"code":"CONNECTOR_OAUTH_START_FAILED","message":"无法启动连接器授权。请检查服务器网络、代理和连接器配置后重试。","details":{"reason":"HTTP 503","upstream_status":503}}}
```

HTTP 2xx 不等于探测成功或设备已授权；分别检查 `ok`、`status`、`applied`。管理变更后刷新连接、资源与当前项目，不覆盖旧轮次记录。

APP 联调按以下闭环验收：Token 连接创建→测试→修改→删除；自定义 HTTP／stdio 保存→测试→停用→恢复；内置 OAuth 新建和重授权；自定义 OAuth 自动应用；本人／共享资源隔离；项目 PATCH 与版本冲突；Task 首轮显式选择、追问继承、[] 禁用、断线原请求重放，以及历史实际选择展示。供应商和主机命令需在目标部署单独实测，文档示例不替代该验收。

## 8. 内部 MCP 与实现索引

`POST/GET/DELETE /api/internal/mcp/{kind}/{instance_id}?token=...` 是 Harness 使用的内部 Streamable HTTP MCP 网关，免登录 JWT 但需要实例内部 token；POST 支持 initialize、ping、tools/list、tools/call，GET 为会话 SSE，DELETE 结束会话。它不是 APP 连接管理 API，也没有向 APP 颁发内部 token 的公开接入流程。APP 使用本文 REST 管理连接并通过统一 Task 使用工具。

| 实现／文档 | 用途 |
| --- | --- |
| [连接器路由](../src/octop/api/routers/connectors.py) | 请求模型、OAuth、实例与配置管理。 |
| [目录和表单元数据](../src/octop/infra/connectors/catalog.json) | 内置 kind、授权方式、凭证字段。 |
| [自定义 MCP 规范](../src/octop/infra/connectors/custom_mcp.py) | transport、引用名、OAuth 预览与规范化。 |
| [连接服务](../src/octop/infra/connectors/service.py) | 存储、共享、按需刷新和可见配置。 |
| [项目模型](../src/octop/infra/projects/models.py)／[任务实现](../src/octop/infra/projects/tasks.py) | MCP 候选、项目字段与每轮合并。 |
| [网页调用封装](../dashboard/src/api/modules/connectors.ts) | 已有请求封装参考；类型若与服务端不一致，以服务端为准。 |
| [内置连接器目录](connectors.md)／[统一 Task APP 交接](agentcomputer-unified-conversations-handoff.md) | 供应商与会话全流程背景。 |
