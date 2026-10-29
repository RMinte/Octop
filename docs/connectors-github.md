# GitHub MCP 连接器

GitHub 内置连接器使用 GitHub 官方远程 MCP Server，通过 **Personal Access Token（PAT）** 访问账户允许的仓库、Issue、Pull Request 等资源。工具的实际可用范围取决于 GitHub 返回的工具和 PAT 权限。

| 配置 | 值 |
| --- | --- |
| 连接器 kind | `github` |
| 鉴权 | `personal_token`，填写 PAT |
| MCP 模式 | `remote` |
| 传输 | Streamable HTTP |
| 官方地址 | `https://api.githubcopilot.com/mcp/` |
| 请求头 | `Authorization: Bearer <PAT>` |

Octop 主机直接连接官方地址，无需安装 GitHub CLI、Node.js 或 Docker。当前入口面向 GitHub 官方托管服务；不提供 GitHub Enterprise Server 地址配置，也不执行 OAuth、动态客户端注册（DCR）或 OAuth 回调。

## 在控制台连接

1. 在 GitHub 的 [Personal access tokens 设置](https://github.com/settings/tokens)创建适合当前工作的 PAT，按需要选择账户、仓库和权限。GitHub 组织策略、授权审批及 Token 有效期仍然生效。
2. 在 Octop「连接器 → 内置连接器 → GitHub」中填写连接器名称、说明和 Token。只粘贴 PAT 本身，无需添加 `Bearer `。
3. 点击「探测」。Octop 使用该 PAT 初始化官方 MCP 会话并列出工具；探测不会创建仓库、Issue 或 Pull Request。探测成功不代表 PAT 拥有每个仓库的全部读写权限。
4. 点击「保存」。一个 Octop 账户可以保存多个 GitHub 实例，例如工作账户和个人账户，各自持有独立的 PAT。

PAT 加密保存在 Octop 数据库中。列表、详情和项目资源接口不返回 PAT 明文，详情仅返回 `credentials_preview.token_configured`。更新 Token 时重新填写；删除连接器清除 Octop 的保存记录，不会撤销 GitHub 上的 PAT。需要撤销时在 GitHub 设置中处理。

开启「共享」后，其他用户可使用该实例的授权身份。共享使用者不能读取其凭证或修改、删除该实例；有管理权限的管理员适用已有连接器管理规则。

## API 接入

使用当前 Octop 登录态的 Bearer Token 调用 `/api`。以下请求中的 PAT 均为占位值；生产环境应通过安全输入提交，避免写入日志。

创建实例：

```http
POST /api/connector-instances
Authorization: Bearer <Octop 登录 Token>
Content-Type: application/json
```

```json
{
  "kind": "github",
  "display_name": "工作 GitHub",
  "description": "用于当前项目的仓库和 Issue",
  "credentials": { "token": "<GitHub PAT>" },
  "shared": false,
  "default_open": false
}
```

成功返回 `201`，包含 `instance_id` 和 `mcp_server_name`。保存前探测使用 `POST /api/connectors/test-credentials`，请求体为 `{"kind":"github","credentials":{"token":"<GitHub PAT>"}}`；保存后探测使用 `POST /api/connector-instances/{instance_id}/test`，不传请求体。

探测成功示例（工具以远端实际返回为准）：

```json
{
  "ok": true,
  "tool_count": 1,
  "tools": [{ "name": "get_me", "description": "Get the authenticated user" }]
}
```

探测失败可能仍返回 HTTP `200`，APP 必须检查 `ok`，不能只检查 HTTP 状态。远端 `401/403` 表示鉴权被拒绝；网络或远端服务故障与权限问题应分别展示。PAT 过期或被撤销后，通过 `PATCH /api/connector-instances/{instance_id}` 提交新的 `credentials.token`，Octop 不会自动刷新 PAT。

## 在项目和任务中选择

1. 从 `GET /api/projects/resources` 或 `GET /api/project-tasks/resources` 的 `mcp_servers[]` 中选择 GitHub 实例。保存凭证后的条目通常为：

   ```json
   {
     "server_name": "github__<instance_id>",
     "name": "工作 GitHub",
     "state": "configured"
   }
   ```

2. 项目默认选择保存为 `mcp_servers: ["github__<instance_id>"]`。使用接口返回的 `server_name` / `mcp_server_name`，不要传 `kind`、显示名称或单独的 `instance_id`。
3. 创建任务或追问时，在消息的 `options.mcp_servers` 中传本轮选择。省略或 `null` 合并当前所选专家默认 MCP 和项目配置并去重；显式数组完整替换本轮集合，`[]` 表示本轮停用 MCP。本轮覆盖不会修改项目或专家默认值，也不自动沿用上一轮选择。

```json
{
  "request_id": "github-review-001",
  "project_id": "<project_id>",
  "prompt": "查看当前仓库的待处理 Issue，并整理优先级",
  "options": { "mcp_servers": ["github__<instance_id>"] }
}
```

将以上请求发送到 `POST /api/project-tasks`；追问使用 `POST /api/project-tasks/{task_id}/messages`。连接器的 `default_open` 不是项目默认选择的替代品，应按项目与本轮选项明确绑定。使用允许 MCP 执行的任务模式；需要修改远端资源时，PAT 和 GitHub 账户仍须具备相应权限。

## 验证范围与官方参考

自动化测试使用合成 PAT 和模拟远端探测，覆盖官方 URL、Bearer 请求头、Streamable HTTP 路径、空 Token 拒绝、凭证脱敏、实例 CRUD 与账户隔离，以及项目/任务中的资源引用和本轮停用。没有使用真实 GitHub 账户验收远端工具调用。

- [GitHub MCP Server 官方说明](https://github.com/github/github-mcp-server/blob/main/README.md)
- [GitHub 远程 MCP Server 接入与权限说明](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)
- [Octop 内置连接器目录](connectors.md)
