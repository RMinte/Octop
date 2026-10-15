# AgentComputer 技能接口接入说明

AgentComputer 可以直接调用 Octop 已有的技能接口，复用网页登录所用的用户 Bearer token。项目选择技能使用全局 SkillHub 目录和项目资源接口，管理共享技能库使用技能包接口。以下路径均为完整 HTTP 路径，直接请求当前连接的 Octop 服务。

## 认证和权限

通过 `POST /api/auth/login` 登录，请求体为 `{"username":"账号","password":"密码"}`，取响应中的 `access_token`。启用了强验证码时还需 `captcha_token`，详见 [认证说明](api.md#authentication)。后续请求携带：

```http
Authorization: Bearer <access_token>
Accept-Language: zh
Content-Type: application/json
```

收到响应头 `X-Octop-Access-Token` 时更新保存的 token，以支持滑动续期。

| 接口范围 | 权限要求 |
| --- | --- |
| `/api/skills/hub/*` | 任意已登录账号，无需专家或 `skill_packages` 权限 |
| `/api/projects/resources`、项目创建和编辑 | 任意已登录账号；项目仅本人可访问 |
| `/api/skill-packages` 及其全部子路径 | 账号具有 `skill_packages` 权限；管理员自动通过 |
| 修改、删除技能包及其中技能 | 在模块权限之外，还要求是该包创建者或管理员 |

AgentComputer 应直连目标 Octop；这些全局技能接口和项目接口没有 Bridge 转发入口。也没有 `/api/agentcomputer/v1` 技能专用协议。

## 为项目选择技能

项目中的 `skills` 支持多个已安装技能和 SkillHub 引用，最多 64 项。添加或移除技能通过项目配置接口完成，不需要创建或绑定专家。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/skills/hub/search?q=关键词&limit=50` | 搜索 SkillHub；`limit` 默认 50，限制在 1–100 |
| GET | `/api/skills/hub/rankings?type=all` | 获取 SkillHub 榜单 |
| GET | `/api/projects/resources` | 获取项目可用的已安装技能、MCP 引用和服务端目录范围 |
| POST | `/api/projects` | 新建项目，同时保存技能引用；返回 201 |
| GET | `/api/projects/{project_id}` | 读取当前技能选择和项目版本 |
| PATCH | `/api/projects/{project_id}` | 更新技能选择，必须携带 `expected_revision`；返回 200 |

### 搜索和榜单响应

搜索直接返回数组，常用字段为 `slug`、`name`、`description`、`version`；可能附带上游的图标、评分等字段。示例中的名称和标识仅用于演示，实际保存搜索返回的值。

```json
[
  {
    "slug": "example-search",
    "name": "示例搜索技能",
    "description": "检索和整理资料",
    "version": "1.0.0"
  }
]
```

榜单 `type` 支持 `all`、`recommended`、`trending`、`hot`、`newest`、`featured`、`paid`。`all` 返回 `{"rankings": {...}}`，其中各键对应一个榜单的上游 JSON 对象；单一 `type` 直接返回该榜单对象。部分榜单失败时仍返回成功分组，并附带 `errors` 对象，按失败分组提供重试即可。全部失败返回 502，全部超时返回 504。榜单项字段随上游载荷返回，不应将搜索数组与榜单对象使用同一解码结构。

### 已安装技能响应

`GET /api/projects/resources` 返回以下结构；项目选择器只需读取 `skills`。该列表会排除已标记损坏的技能。

```json
{
  "skills": [
    {
      "package_id": "示例技能包标识",
      "slug": "example-review",
      "name": "示例审阅技能",
      "description": "检查内容并提出修改建议"
    }
  ],
  "mcp_servers": [],
  "directory_roots": []
}
```

`package_id` 和 `slug` 必须使用资源列表中的原值。只做项目技能选择时，无需先请求受 `skill_packages` 权限限制的技能包管理接口。

### 保存和修改选择

新建项目必须携带 `Idempotency-Key`，值为 8–128 位字母、数字、下划线或短横线。同一次创建的网络重试复用相同标识和内容。

```http
POST /api/projects
Authorization: Bearer <access_token>
Idempotency-Key: 793080af-5865-4c34-bbba-3e2e65379519
Content-Type: application/json

{
  "name": "资料整理",
  "skills": [
    {"package_id": "从资源列表取得的标识", "slug": "example-review"},
    {
      "source": "skillhub",
      "slug": "example-search",
      "name": "示例搜索技能",
      "description": "检索和整理资料"
    }
  ]
}
```

编辑时先读取项目最新 `revision`，再发送完整的技能选择数组：

```http
PATCH /api/projects/<project_id>
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "expected_revision": 1,
  "skills": [
    {"source": "skillhub", "slug": "example-search", "name": "示例搜索技能"}
  ]
}
```

`skills` 省略表示保持原选择，`[]` 表示全部移除，不接受 `null`。SkillHub 的 `name` 必填，`description` 可省略。避免重复引用，并避免不同来源使用相同 `slug`，运行时技能名不能冲突。版本冲突返回 `409 PROJECT_REVISION_CONFLICT`，应重新读取详情后处理冲突。更多项目字段见 [项目和任务接口](api.md)。

保存项目只保存引用，不下载或执行 SkillHub 技能。发送任务消息时才加载本轮项目配置：已安装技能从 Octop 共享技能库读取，SkillHub 引用由 Octop 下载后加载到任务工作区。修改后的选择在下一轮消息生效。技能包管理的安装操作则立即写入 Octop 服务端共享技能库，不会写入用户电脑的客户端目录。

## 管理共享技能包

以下全部接口要求 `skill_packages` 权限。`package_id` 使用不透明字符串，`slug` 作为 URL 路径段编码。

| 方法 | 路径 | 请求体或用途 | 成功状态 |
| --- | --- | --- | --- |
| GET | `/api/skill-packages` | 所有共享技能包；可加 `writable_only=true` 只列可修改的包 | 200 |
| POST | `/api/skill-packages` | `{name, description?, icon_name?, icon_url?}` | 200 |
| GET | `/api/skill-packages/{package_id}` | 包元数据、技能列表、当前用户操作权限 | 200 |
| PATCH | `/api/skill-packages/{package_id}` | `{name?, description?, icon_name?, icon_url?, copy_policy?}` | 200 |
| DELETE | `/api/skill-packages/{package_id}` | 删除技能包 | 204 |
| GET | `/api/skill-packages/{package_id}/skills` | 包内技能摘要数组 | 200 |
| GET | `/api/skill-packages/{package_id}/skills/{slug}` | 技能摘要及 `frontmatter`、`body`、`raw` | 200 |
| POST | `/api/skill-packages/{package_id}/skills` | `{name, content?, files?, overwrite?}` | 200 |
| PUT | `/api/skill-packages/{package_id}/skills/{slug}` | `{content?, files?}`，替换已有技能文件集合 | 200 |
| DELETE | `/api/skill-packages/{package_id}/skills/{slug}` | 删除包内技能 | 204 |
| POST | `/api/skill-packages/{package_id}/skills/hub/install` | `{skill_name, display_name?, icon_url?, label?, summary?, overwrite?}` | 201 |
| POST | `/api/skill-packages/{package_id}/skills/import` | `{bundle_url, version?, overwrite?}` | 201 |
| POST | `/api/skill-packages/from-skillhub` | `{slug, name?, description?, icon_name?, icon_url?}`，从 SkillHub 技能合集新建包 | 201 |
| GET | `/api/skill-packages/hub/search?q=关键词&limit=50` | 技能包管理入口的 SkillHub 搜索 | 200 |
| GET | `/api/skill-packages/hub/rankings?type=all` | 技能包管理入口的 SkillHub 榜单 | 200 |

包列表返回 `id`、`name`、`description`、`created_by`、`creator_username`、`creator_display_name`、`skill_count`、`icon_name`、`icon_url`、`copy_policy`、`created_at`、`updated_at`、`can_write`、`can_copy`。详情额外包含 `skills`。根据 `can_write` 控制管理按钮；`can_copy` 表示向专家工作区复制的权限，不表示项目技能引用的权限。

创建和修改响应不保证包含 `can_write`、`can_copy`；需要刷新这些权限时重新读取列表或详情。`copy_policy` 可为 `snapshot`、`lock`、`deny`，作用于向专家工作区复制的行为。

技能摘要包含 `slug`、`name`、`description`、`path`、`kind: "package"`、`package_id`，可能附带 `emoji`、`icon_url` 等展示字段。详情的 `raw` 是完整 `SKILL.md`，`frontmatter` 是解析后的元数据，`body` 是去掉 frontmatter 的正文。损坏项可能返回 `corrupt: true`，客户端应禁用该项。

### 新建技能和上传配套文件

只有一个说明文件时使用 `content`，例如：

```json
{
  "name": "example-review",
  "content": "---\nname: example-review\ndescription: 审阅内容\n---\n\n检查输入内容并提出修改建议。",
  "overwrite": false
}
```

包含脚本、模板等配套文件时改用 `files`：每项为 `{"path":"SKILL.md","content_base64":"文件原始字节的 Base64"}`，路径相对于该技能根目录，必须包含 `SKILL.md`。`files` 非空时优先于 `content`；不要把 Base64 字符串直接放进 `content`。替换操作会移除原有但本次未提供的文件，因此编辑前应保留完整文件集合；技能详情接口只返回说明文件，不提供完整配套文件下载。

新建技能时 `name` 是技能目录的 `slug`，不能包含路径分隔符或以点开头；与 SkillHub 的展示名称区分。`overwrite` 默认 `false`，重名返回 409。

### 安装和导入

从 SkillHub 安装单个技能到已有包时，请求中的字段叫 `skill_name`，取搜索结果的 `slug`：

```json
{
  "skill_name": "example-search",
  "display_name": "示例搜索技能",
  "overwrite": false
}
```

可选 `label`、`summary` 均为 `{"zh":"中文","en":"English"}`。`icon_url` 使用服务端认可的 SkillHub 图标 URL。URL 导入默认支持 `https://skills.sh/`、`https://clawhub.ai/`、`https://skillsmp.com/`、`https://github.com/`，管理员也可配置额外来源。

## 已有专家自身的技能

AgentComputer 如果需要操作已有专家的工作区技能，可以继续调用下列通用接口。读取时检查专家访问权限，修改时要求拥有者或管理员；工作区内容操作通常要求专家已启动。独立项目的技能选择使用前述项目接口。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET、POST | `/api/agents/{agent_id}/skills` | 列表或新建技能 |
| GET、PUT、DELETE | `/api/agents/{agent_id}/skills/{name}` | 读取、替换或删除技能 |
| POST | `/api/agents/{agent_id}/skills/{name}/enable` | 启用，成功返回 204 |
| POST | `/api/agents/{agent_id}/skills/{name}/disable` | 禁用，成功返回 204 |
| POST | `/api/agents/{agent_id}/skills/import` | 从 URL 导入 |
| GET | `/api/agents/{agent_id}/skills/hub/search` | 搜索 SkillHub |
| GET | `/api/agents/{agent_id}/skills/hub/rankings` | 获取 SkillHub 榜单 |
| POST | `/api/agents/{agent_id}/skills/hub/install` | 安装 SkillHub 技能 |
| GET、PUT | `/api/agents/{agent_id}/skill-packages` | 读取或替换专家挂载的技能包 |
| POST | `/api/agents/{agent_id}/skill-packages/{package_id}/copy` | 复制包内技能到专家工作区，另需 `skill_packages` 权限 |
| POST | `/api/agents/{agent_id}/skills/{name}/push-to-package` | 将工作区技能复制到可写包，另需 `skill_packages` 权限 |
| POST | `/api/agents/{agent_id}/skills/copy` | 从本人有权访问的另一专家复制工作区技能 |

复制创建快照，后续不自动同步。完整请求字段可在启用 API 文档的 Octop 实例查看 `/api/docs` 的 `skills`、`skill-packages` 和 `projects` 分组，OpenAPI 为 `/api/openapi.json`。

## 错误处理

| 状态 | 常见原因 | 客户端处理 |
| --- | --- | --- |
| 401 | 未登录、token 无效或过期 | 重新登录 |
| 403 | 缺少模块权限，或不是技能包创建者 | 提示权限不足 |
| 404 | 技能包或技能不存在，或项目不属于当前用户 | 刷新资源或返回列表 |
| 409 | 包名或技能重名、项目版本或幂等请求冲突 | 按错误码处理重名或刷新最新项目 |
| 400、422 | 请求字段、引用、Base64 或文件路径不合法 | 提示修正输入 |
| 502、504 | SkillHub 上游失败或超时 | 展示重试入口 |

领域错误通常为 `{"error":{"code":"错误码","message":"本地化提示",...}}`；部分 SkillHub HTTP 错误使用 `{"detail":"提示"}`，请求校验也可能返回 `detail` 数组。客户端应兼容这些现有错误结构。
