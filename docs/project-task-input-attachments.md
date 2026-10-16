# 任务通用附件接入说明

更新日期：2026 年 10 月 16 日。

任务支持文字、图片和通用文件混合输入。用户先上传文件取得附件 ID，再用首条消息创建任务，或在同一任务中继续追问。本文说明网页和其他客户端如何接入上传、发送、历史展示及下载，以及文件在多轮执行中的保存方式。

输入附件统一使用 `/api/project-tasks/attachments`，无需创建专家或预先创建任务。项目任务和无项目任务使用同一契约。任务输出附件仍由工具发布到 `TaskTurn.artifacts`，输入文件则保存在 `TaskTurn.attachments`。

## 支持范围和限额

| 输入类型 | 处理方式 | 模型要求 |
| --- | --- | --- |
| JPEG、PNG、WebP、GIF | 验证真实图片内容，在模型调用前临时展开为视觉输入 | 当前或历史包含这些图片时，须选择支持图片的模型 |
| 文档、PDF、表格、文本、代码、压缩包等其他文件 | 保存原始内容，并在任务执行前放入服务端工作区，供文件工具、脚本和 Skill 使用 | 不要求视觉模型 |
| SVG、BMP 等其他图片格式 | 按普通文件保存和下载 | 不作为直接视觉输入 |

每条消息最多提交 **9 个附件**，图片和其他文件合计计数，附件 ID 不能重复。每个文件上限为 **25 MiB**，同时遵守服务端配置的更低上传限额。网页支持文件选择、拖放和粘贴；上传未完成或失败时不能发送对应草稿。

上传文件不会自动执行、解压或解析所有格式。模型应使用适合该格式的工具读取真实内容，并遵循任务审批和全局安全策略。服务端不会因为收到普通文件而自动切换到视觉模型。

## 上传附件

上传需要当前账号鉴权，请求体为 `multipart/form-data`，文件字段名为 `file`。

```http
POST /api/project-tasks/attachments
Authorization: Bearer <access_token>
Content-Type: multipart/form-data; boundary=<boundary>
```

客户端应让 HTTP 库生成 multipart boundary，不要在上传时使用 JSON 请求体。成功返回 `201 Created`：

```json
{
  "attachment_id": "input_example",
  "name": "报告.pdf",
  "media_type": "application/pdf",
  "byte_count": 12345,
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "preview_url": "/api/project-tasks/attachments/input_example",
  "workspace_path": "inbound/input_example/报告.pdf",
  "created_at": "2026-10-16T08:00:00Z"
}
```

| 字段 | 用法 |
| --- | --- |
| `attachment_id` | 发送消息时提交的不透明 ID，原样保存 |
| `name` | 服务端清理路径分隔符等不安全字符后的文件名，用于展示和下载 |
| `media_type` | 服务端记录的类型；上述四类视觉图片按真实内容验证 |
| `byte_count` / `sha256` | 原始上传内容的长度和摘要 |
| `preview_url` | 需要鉴权的原始内容地址；图片用于预览，其他文件用于下载 |
| `workspace_path` | 普通文件在服务端工作区的稳定相对路径；视觉图片为 `null` |
| `created_at` | UTC 时间；用户界面使用 Octop 服务端配置的时区显示 |

附件上传后先属于当前账号，尚未发送的附件在 24 小时后失效。上传成功不代表文件已经关联任务，也不代表任务已经开始执行。

## 创建任务和继续追问

首条消息使用标准任务接口，指定 `project_id` 创建项目任务，省略或传 `null` 创建无项目任务：

```http
POST /api/project-tasks
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "request_id": "file-message-001",
  "project_id": null,
  "prompt": "请分析这份报告",
  "attachment_ids": ["input_example"]
}
```

成功返回 `202 Accepted` 和 `TaskDetail`。这表示消息已保存并排队，执行结果以任务轮次状态为准。也可使用 `POST /api/projects/{project_id}/tasks` 在指定项目中发送首条消息。

追问使用原任务 ID：

```http
POST /api/project-tasks/{task_id}/messages
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "request_id": "file-message-002",
  "prompt": "继续核对报告中的数字"
}
```

已有文件随任务保留，追问不必重复上传或提交旧附件 ID。用户可以再次引用本任务附件，也可以上传新文件并在追问中提交新 ID。任务仍在执行或等待交互时，新消息返回 `TASK_BUSY`。

`prompt` 可省略或为空，但文字和附件至少提供一种。只有附件的首条消息使用本地化附件任务标题。首次关联文件与消息保存处于同一事务内，消息被拒绝时不会占用附件。

WebSocket 的对应命令为 `task.create` 和 `task.message`，`data` 使用相同发送结构：

```json
{
  "type": "task.create",
  "id": "file-command-001",
  "data": {
    "request_id": "file-message-001",
    "prompt": "请分析这份报告",
    "attachment_ids": ["input_example"]
  }
}
```

WebSocket 只传 ID，不传文件字节。完整鉴权、订阅和状态恢复规则见[任务 WebSocket 接入指南](project-task-websocket.md)。

## 多轮执行中的文件

普通文件保存在任务的独立上传记录中。执行前，服务端通过任务工作区接口把缺失的文件放入 `workspace_path`，例如 `inbound/input_example/报告.pdf`。项目任务使用当轮项目的服务端目录，无项目任务使用自动分配的服务端工作区。

同一目录中的已有工作副本会保留，不因追问覆盖用户或工具的编辑。项目服务端目录变更后，缺失文件从原始上传内容恢复；旧目录中的编辑不会自动同步到新目录。历史下载始终返回上传原件，而非工作副本的当前内容。

每轮向模型提供本任务的普通文件清单，即使长对话压缩后丢失旧消息中的路径，模型仍可重新定位文件。图片通过 `task-image://` 引用持久化，模型可使用 `read_task_image` 重新查看历史视觉图片；该工具不读取普通文件。

`workspace_path` 是服务端工作区相对路径，不是主机绝对路径。`client_directory` 仍只保存位置，服务端不会探测、读取或同步用户电脑目录。客户端上传所选文件，只上传明确提交的文件内容。

## 历史展示和下载

`TaskTurn.attachments` 按提交顺序保存本轮输入元数据，未附文件的轮次返回空数组。REST 详情、历史分页及 WebSocket `result`、`snapshot`、`turn` 使用同一结构，均不内嵌文件字节。

客户端可把 JPEG、PNG、WebP、GIF 展示为图片缩略图，其他类型展示为文件卡片。通过返回的 `preview_url` 鉴权读取原始内容：

```http
GET /api/project-tasks/attachments/input_example
Authorization: Bearer <access_token>
```

视觉图片返回 `Content-Disposition: inline`，其他文件返回 `attachment`。响应包含对应的 `Content-Type`、`Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff` 和受限 CSP。

浏览器先用账号凭据获取 Blob，再生成本地预览或下载 URL；下载文件名使用 `name`。不要把访问令牌放进 URL。切换任务或卸载界面时取消未完成读取，使用结束后释放 Blob URL。

## 重试和附件归属

网络结果未知时，保留原 `request_id`、正文、`attachment_ids` 及顺序、执行选项和任务归属。显式重试复用原内容；WebSocket 命令 `id` 可以更换，但 `data.request_id` 保持不变。同一幂等标识换文件、调整顺序或改变内容，返回 `TASK_REQUEST_CONFLICT`。

手动重试失败或停止的最后一轮使用原轮附件，无需重新上传。断线恢复只重新订阅任务，不重发原消息。停止任务不会回滚已执行操作，已关联原始附件仍可下载或用于后续追问。

附件首次关联后只属于该任务，不能被另一个任务复用；其他账号也不能读取。删除任务会删除其上传记录，服务端工作目录中的工作副本仍按[任务删除规则](project-task-deletion.md)保留。

## 错误处理

| HTTP 状态和错误码 | 客户端处理 |
| --- | --- |
| `413 TASK_ATTACHMENT_LIMIT` | 减小单个文件，并检查管理员上传限额 |
| REST `422` 请求校验错误 | 修正消息结构，附件最多九个，ID 非空且不重复 |
| WebSocket `422 TASK_FRAME_INVALID` | 修正消息结构或附件 ID 列表 |
| `404 TASK_ATTACHMENT_UNAVAILABLE` | 附件不存在、已过期、归属其他账号或任务，重新选择可用附件 |
| `422 TASK_IMAGE_INVALID` | 修正损坏或伪造的 JPEG、PNG、WebP、GIF 图片 |
| `413 TASK_IMAGE_LIMIT` | 图片无法压缩到模型可接收大小，调整图片后重新上传 |
| `422 TASK_IMAGE_MODEL_REQUIRED` | 当前或历史包含视觉图片，选择支持图片的模型 |
| `409 TASK_REQUEST_CONFLICT` | 原幂等标识对应的消息内容不同，核对本地待发记录 |
| `409 TASK_BUSY` | 等待原轮结束或先停止，再继续发送 |

REST 消息结构校验失败时读取标准请求校验响应，业务错误和 WebSocket 错误按错误码处理，并展示本地化消息。上传失败时保留草稿供用户移除或重新添加，不自动重发任务消息。

接口字段以[API 文档](api.md)、[WebSocket JSON Schema](project-task-websocket.schema.json)及 `/api/docs` 为准。
