# Codex / Claude 数据导入

Octop 接收客户端明确选择并转换后的项目、可见会话历史和配置副本。客户端仍负责读取本机 Codex / Claude 文件及投影私有格式；服务端不扫描客户端目录，也不接收路径作为文件读取指令。本轮仅实现服务端，客户端尚未切换到这些接口。

所有接口都使用当前账号鉴权、`Cache-Control: no-store`，仅操作本人数据。一次请求导入一个项目、一条会话或一个配置条目，调用方可逐项选择、报告进度和重试；不提供整机覆盖恢复、旧本地会话自动迁移或 Bridge 转发。

| 接口 | 用途 |
|---|---|
| `POST /api/projects/import` | 创建或明确关联一个外部项目 |
| `POST /api/project-tasks/import` | 将一条外部会话保存为 Octop 任务历史 |
| `POST /api/projects/import/configurations` | 保存一个待启用配置条目 |
| `GET /api/projects/import/configurations` | 按 `source`、`category` 筛选条目，支持 `limit`、`offset` |
| `GET /api/projects/import/configurations/{configuration_id}` | 读取本人条目及文件副本 |

## 项目

```json
{
  "source": "codex",
  "external_project_id": "stable-source-project-id",
  "name": "示例项目",
  "client_directory": "/Users/example/code/demo"
}
```

`source` 为 `codex` 或 `claude`。返回 `{action, project}`，`action` 为 `imported`、`linked` 或 `unchanged`。按账号、来源及 `external_project_id` 去重；首次可附带 `project_id` 明确关联本人已有项目。项目重名不会自动关联，会为新项目名称添加确定后缀。

新项目的 `directory` 为 null，`client_directory` 仅保存客户端绝对路径，不授予文件访问权限。Skill、MCP、插件和行动指南不自动启用，不创建或绑定专家。重导返回原项目，不覆盖之后的编辑；同一来源试图改绑另一项目返回 `409 PROJECT_REQUEST_CONFLICT`。项目删除后对应映射随之删除，可以重新导入。

## 会话

```json
{
  "source": "claude",
  "external_session_id": "stable-source-session-id",
  "project_id": null,
  "title": "原来的会话",
  "source_updated_at": "2026-10-20T08:00:00Z",
  "source_revision": "optional-file-fingerprint",
  "turns": [{
    "external_turn_id": "stable-source-turn-id",
    "user_message": {"text": "原始问题", "timestamp": "2026-10-19T08:00:00Z"},
    "assistant_message": {"text": "原始回答", "timestamp": "2026-10-19T08:00:05Z"},
    "activities": [],
    "file_changes": [],
    "completed_at": "2026-10-19T08:00:05Z"
  }]
}
```

返回 `{action, task}`，`action` 为 `imported`、`updated` 或 `unchanged`。按账号、来源和 `external_session_id` 去重；不同来源的同名会话互不影响。`project_id` 必须是本人已有项目，可为 null。导入后不得通过重导改绑项目。

无需配置模型即可导入。任务与来源轮次保存为 `completed`，`model_ref` 为空，不排队执行、不生成标题、不产生用量或执行回执。摘要的 `import_source`、`external_session_id` 标识来源；原生任务为 null。每轮 `imported_history` 保存来源轮次 ID、来源时间、活动、文件差异及缺失附件信息。`started_at` 为空，不将外部历史时间伪装成 Octop 的执行时间。

`activities` 接收 `commentary` 或 `tool`，可包含 `name`、`detail`、`output`、`succeeded`、`timestamp`、`duration_ms`、`exit_code`；工具活动须有 `name`。同时投影为内容块供现有时间线展示，但没有 Octop `tool_call_id`、工具参数、审批或执行回执。`file_changes` 只保存 `{path, old_content, new_content}` 展示数据，不读取、应用或撤销文件。私有 reasoning 不属于此协议。

附件先通过 `POST /api/project-tasks/attachments` 上传字节，再在用户或助手消息的 `attachment_ids` 里提交本人附件 ID。用户附件见 `turn.attachments`，助手附件见 `turn.imported_history.assistant_attachments`。无法取得原文件时可提交 `missing_attachments: [{name, media_type}]`，仅保留缺失标记。接口不接受本机文件 URL。上传附件与导入事务一起绑定任务；失败不产生半条历史。

重导规则：

- 来源内容完全相同返回 `unchanged`，不会重复生成任务或轮次。仅标题、文件修订或来源更新时间变化仍属于 `unchanged`；可刷新未被手工编辑的来源标题，不改写历史时间。迟到的旧元数据重试不会回退已保存的新标题或来源修订。
- 未在 Octop 续聊时，可以更新来源回答、工具结果、文件差异或追加来源轮次，保持已有 Octop `turn_id`。旧用户正文、时间、附件引用及轮次身份必须一致，已有活动和文件条目不得删除或重排；来源时间更旧的内容更新也会冲突。
- 任务存在活动轮次时返回 `409 TASK_BUSY`。已在 Octop 追问后，来源内容变化返回 `409 TASK_REQUEST_CONFLICT`，保留现有历史和执行上下文；相同来源历史仍可重放。归档任务或项目不接收新来源内容。
- 导入后的追问走现有消息接口，在同一个任务内使用当时的项目、模型、Skill 和 MCP 配置，原问题与回答进入上下文。

一次会话最多 1000 轮、原始 JSON 请求体 16 MiB，单段文字最多 1 MiB。所有时间都须带时区。较大的来源会话由客户端拆为不同的外部会话标识；不能把后半段作为同一个来源会话覆盖上传。返回详情使用现有默认历史分页，超过一页时通过任务详情接口的 `next_page_cursor` 读取更早轮次。

## 配置逐项导入

每次提交一个独立条目，而非整个来源的总开关：

| `category` | 条目粒度 | 文件副本示例 |
|---|---|---|
| `skills` | 每个 Skill 一个条目 | `SKILL.md` 及所需脚本、参考资料 |
| `mcpServers` | 每条 MCP 服务配置一个条目 | 该服务的配置片段 |
| `plugins` | 每个插件一个条目 | 插件清单和所选配置 |
| `instructions` | 每份行动指南一个条目 | `AGENTS.md`、`CLAUDE.md` |
| `settings` | 每项可独立选择的设置一个条目 | 模型偏好等配置片段 |

客户端负责从聚合配置文件中提取选中的条目，避免把整份配置反复复制到多个条目。服务端不执行来源配置，也不会安装包、连接 MCP、迁入来源授权或修改当前项目。

```json
{
  "source": "codex",
  "category": "skills",
  "name": "my-skill",
  "source_path": "/Users/example/.codex/skills/my-skill",
  "files": [{"relative_path": "SKILL.md", "content_base64": "IyBNeSBTa2lsbAo="}]
}
```

返回 `{action, configuration}`。条目有独立 `configuration_id`、来源、类别、名称、`source_path`、摘要、字节数和文件数，状态固定为 `pendingActivation`。列表不返回文件字节，详情返回原始 Base64 副本；本人可读，其他账号返回 404。

同一账号和来源下，类别、名称与文件内容相同的条目去重；文件变化保存为另一份副本。`source_path` 仅为来源说明，不参与内容去重。每条最多 1000 个文件、解码总计 20 MiB、单文件 2 MiB；原始 JSON 请求体上限 32 MiB。文件路径为以 `/` 分隔的相对路径，拒绝绝对路径、点目录、重复路径、反斜杠与非法 Base64。

本轮不提供配置激活接口。后续启用 Skill、MCP 或插件需走各自安装与连接流程，再显式选择项目引用；待启用副本不会被任务运行时加载。

## 数据版本

SQLite 与 PostgreSQL 使用 schema 50：新增来源身份映射及配置副本表，任务新增来源字段。升级保留现有项目、任务和会话，不自动迁移客户端数据。确定性测试覆盖迁移重复执行、v49 数据保留、鉴权、去重、冲突、追问及配置不生效；PostgreSQL DDL 保持同构，未进行 PostgreSQL 实例实测。
