# AgentComputer 技能包私有与分享：客户端交接

更新日期：2026-10-26。适配目标是已部署包含 `ddf3f566569086551a6a70d0e148eae9a8cd9e3c` 技能包实现的 Octop 后端；本文从 `codex/integrate-main-capabilities @ cc5dfdfa80dc9011c789051c2d0220e6dbe01936` 核对。本文交给客户端实施，不表示 AgentComputer Swift/UI 已完成适配。通用技能与项目接口见[技能接口说明](agentcomputer-skills.md)。

## 当前差异和最低改造

当前 AgentComputer 的 `Services/Box/OctopSkillUpload.swift` 中，`PackageCreation` 只有 `name`，`Package` 只读取 `id`、`name`、`can_write`；`OctopWorkspaceClient.createSkillPackage` 因而仅发送名称。`Core/Strings.swift` 的技能入口仍使用 “Octop shared library” 和上传到共享库的文案。新服务端收到此请求会创建私有包，上传成功不等于已经分享。

客户端最低需要完成以下改动：

1. 将默认文案改为当前 Octop 的“技能库”，新建默认“仅自己可用”。用户明确选择分享后才发送 `shared: true`，说明其含义是向同一 Octop 实例的其他账号开放查看和使用。服务端不恢复默认全局共享。
2. 创建请求显式携带 `shared: false`；增加由用户触发的分享/取消分享操作，调用包元数据的 `PATCH`，无需重新上传文件。
3. 扩充包 DTO，分别保存可见性、所有权展示和操作权限；兼容缺失字段，不将未知状态显示成私有。
4. 写操作后重新读取包详情及项目资源；迁移旧缓存为“待刷新”，按当前主机和账号重新取数。不要批量修改旧包的分享状态。

## 连接与接口

直接请求当前连接的 Octop host，复用 `POST /api/auth/login` 获得的用户 token：

```http
Authorization: Bearer <access_token>
Accept-Language: zh
Content-Type: application/json
```

收到 `X-Octop-Access-Token` 响应头时沿用既有 token 续期逻辑。下列技能包接口全部要求账号有 `skill_packages` 模块权限，管理员自动通过；项目资源接口只要求登录。`package_id` 是不透明字符串，`slug` 和查询参数分别按 URL 路径段、查询值编码。

| 方法 | 完整 URL | 结果 |
| --- | --- | --- |
| GET | `/api/skill-packages` | 200，当前账号可见包的数组 |
| GET | `/api/skill-packages?writable_only=true` | 200，仅当前账号可修改的包，供上传目标选择 |
| POST | `/api/skill-packages` | 200，创建包；默认 `shared=false` |
| GET | `/api/skill-packages/{package_id}` | 200，包元数据、`skills` 和当前账号操作权限 |
| PATCH | `/api/skill-packages/{package_id}` | 200，修改指定元数据；仅创建者或管理员可操作 |
| POST | `/api/skill-packages/{package_id}/skills` | 200，创建包内技能；`overwrite` 默认 false |
| PUT | `/api/skill-packages/{package_id}/skills/{slug}` | 200，更新技能，文件语义见下文 |
| GET | `/api/projects/resources` | 200，当前账号项目可选资源；读取其中 `skills` |
| POST | `/api/skill-packages/from-skillhub` | 201，从合集新建包，同样默认 `shared=false` |
| DELETE | `/api/skill-packages/{package_id}` | 204，删除包；仅创建者或管理员可操作 |

这些接口没有 `/api/agentcomputer/v1` 版本，也没有技能包 Bridge 转发入口。分享只作用于当前 Octop host 内的账号，不表示跨主机发布或公开到 SkillHub。

## DTO 和权限语义

列表、详情实际返回以下相关字段。新增字段使用可选解码并忽略不认识的额外字段；不要将读取 DTO 整体重新编码成更新请求。

| 字段 | 实际含义与客户端用法 |
| --- | --- |
| `id`、`name` | 包标识和名称，保留服务端原值 |
| `created_by` | 创建者用户 ID 的字符串；不是用户名 |
| `creator_username`、`creator_display_name` | 展示信息，可能为 null，不用于鉴权 |
| `shared` | 布尔值。false 为创建者和管理员可见，true 为该实例其他账号可见；不授予修改权 |
| `can_write` | 当前账号能否修改包元数据及包内技能；列表和详情返回，管理/分享/上传按钮以此为准 |
| `allow_download` | 包的 ZIP 导出策略，创建默认 true；不等于当前账号的有效导出权限 |
| `can_download` | 当前账号能否调用包 ZIP 导出；列表和详情返回 |
| `copy_policy`、`can_copy` | 专家工作区复制策略和当前账号复制权限，与项目技能引用分开处理 |
| `is_owner` | **当前技能包响应没有此字段。** 不作为必填 wire 字段，也不能用 `can_write` 代替所有权 |

如需显示“我创建的”，可在已取得当前 host 的 `GET /api/auth/me` 数字 `id` 后，将它转为字符串与 `created_by` 比较，得到客户端派生的 `is_owner`；缺少身份或创建者时保持未知。管理员对别人的包也有 `can_write=true`，并不因此成为创建者。界面权限不能只依据本地所有权判断。

创建和 `PATCH` 响应包含包元数据及技能列表，但**没有** `can_write`、`can_copy`、`can_download`。收到成功后用 `GET /api/skill-packages/{package_id}` 获取当前有效权限，不把缺失能力值覆盖为 false。`GET /api/projects/resources` 的技能项也不提供这些权限字段。

| 当前账号与包的关系 | 可见 | 可修改 | ZIP 导出 |
| --- | --- | --- | --- |
| 创建者，私有或分享 | 是 | 是 | 是，即使 `allow_download=false` |
| 普通其他账号，分享包 | 是 | 否 | 取 `can_download`，由 `allow_download` 决定 |
| 普通其他账号，私有包 | 列表和资源中不出现；读取返回 404 | 否 | 404 |
| 管理员，任意包 | 是 | 是 | 是，即使 `allow_download=false` |

普通其他账号即使可见分享包，也不能修改、删除或向其中上传。`shared` 不能代替 `can_write` 或 `can_download`。`copy_policy=deny` 会禁止普通其他账号复制；`lock` 复制时给工作区副本加锁定标记；创建者、管理员的复制权限以服务端 `can_copy` 为准。

`allow_download` 当前只限制包的 ZIP 导出。可见包的技能正文、目录、单文件读取/下载接口仍按可见性授权，不能把“禁止 ZIP 导出”描述成“禁止读取或取走内容”。需要保护内容时应保持包私有。

## 创建、分享和取消分享

确认当前实例支持私有包后，默认创建请求为：

```http
POST /api/skill-packages
Authorization: Bearer <access_token>
Content-Type: application/json

{"name":"我的审阅技能","shared":false}
```

成功是 200，响应的 `shared` 应为 false。可选创建字段还有 `description`、`icon_name`、`icon_url`、`copy_policy`（默认 `snapshot`）、`allow_download`（默认 true）。用户在创建前明确选择分享时，才把同一请求中的 `shared` 改为 true。

对已有包，先取最新详情并确认 `can_write=true`。用户点击分享后发送：

```http
PATCH /api/skill-packages/<package_id>
Authorization: Bearer <access_token>
Content-Type: application/json

{"shared":true}
```

取消分享发送 `{"shared":false}`；修改 ZIP 导出策略单独发送 `{"allow_download":false}`，二者互不替代。成功均为 200；重新 GET 确认状态后再更新界面。不要因网络超时直接宣称已分享或已私有，先重新读取确认。

`PATCH` 还接受 `name`、`description`、`icon_name`、`icon_url`、`copy_policy`。仅发送用户本次明确修改的字段，未修改字段省略。此接口没有 `expected_revision`，不要套用项目接口的修订号，也不要用旧缓存的整份包 DTO 覆盖当前设置。名称冲突返回 `409 SKILL_PACKAGE_NAME_TAKEN`。

## 旧服务和旧缓存

本说明的私有默认值属于上述目标后端，不能直接套到任意旧实例。旧服务的请求模型可能忽略未知 `shared` 字段，且旧默认行为为全局可见；仅成功发送 `shared:false` **不能证明已私有**。

- 先确认当前 host 已部署目标技能包实现，必要时核对该实例 `/api/openapi.json` 的创建和更新请求定义及实际返回。Task WebSocket 协议版本 3、产品版本名称或客户端版本都不是这项能力的证明。
- `shared`、`allow_download`、`can_download` 缺失时，DTO 保留“未知/服务端未提供”，不解码为默认 false，不显示“仅自己可见”或“禁止下载”。`can_write` 等能力缺失时也不能由 `shared` 推导。
- 空列表不能证明支持私有包。没有可靠的部署/能力证明时，提示确认后端部署或升级，暂停承诺私有的创建与上传；不要用真实技能内容先上传再探测，也不新增臆造的能力字段。
- 旧缓存首次加载后按当前 host 和用户 ID 刷新列表、详情和资源。未知状态不自动 PATCH，不批量将旧包设为分享或私有；已有状态以当前服务端返回为准。

缓存、所选包、请求结果及能力确认都以当前连接主机和账号隔离；切换 host、登出/换号后失效，拒绝上一连接代次的迟到响应。不同主机相同 `package_id` 不能合并。模块权限变化后也要重新读取身份和包权限。

## 上传与编辑文件

现有完整文件上传可沿用。创建包后取其 `id`，发送：

```http
POST /api/skill-packages/<package_id>/skills
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "name":"example-review",
  "files":[
    {"path":"SKILL.md","content_base64":"IyBSZXZpZXcK"},
    {"path":"references/checklist.txt","content_base64":"Y2hlY2sK"}
  ],
  "overwrite":false
}
```

`files` 是该技能根目录下的**完整文件集合**，必须有根 `SKILL.md`；Base64 编码文件原始字节，路径为安全相对路径。非空 `files` 优先于 `content`。`name` 是技能目录 slug，不能有路径分隔符或以点开头。重名且未选择覆盖时返回 `409 SKILL_ALREADY_EXISTS`；由用户决定覆盖，不自动重试为 `overwrite:true`。

已有技能的两种更新语义不同：

| 请求 | 文件结果 |
| --- | --- |
| `PUT .../skills/{slug}`，仅 `{"content":"新的 SKILL.md 原文"}` | 只改写 `SKILL.md`，保留原脚本、模板和其他目录 |
| `PUT .../skills/{slug}`，非空 `files` | 替换整个技能目录，本次未提供的原文件会被移除 |
| `POST .../skills`，`overwrite:true` | 也替换整个技能目录；仅传 `content` 会只留下 `SKILL.md`，不具备上述 PUT 的保留语义 |

不要把空 `files` 当成清空目录命令，也不要将 Base64 放入 `content`。完整覆盖必须持有完整集合；技能详情的 `raw` 只有 `SKILL.md`，不足以重建配套文件。

## 刷新、挂载与既有副本

创建、上传、更新、分享、取消分享和删除后，刷新当前 host/user 的包列表、已打开包详情与 `GET /api/projects/resources`。上传目标列表继续用 `writable_only=true`；项目技能选择器用资源接口，不能因缺少 `skill_packages` 管理权限而隐藏其已有项目选择能力。

私有包可由创建者用于自己的项目；分享后其他账号的资源列表才会包含其中有效技能。取消分享后，其他账号重新获取资源时不再看到这些技能；遇到已选择但不再可用的引用，应提示刷新/重新选择，不能继续凭缓存授权或自动把包分享回来。项目保存只保存引用，本轮已受理的配置和既有历史不由客户端改写；下一轮配置继续由服务端受理和校验。

专家挂载使用包引用，服务端按**专家所属账号**重新检查可见性；管理员能看到的包不意味着可挂到任意普通账号的专家。分享状态改变时服务端会刷新运行中的相关专家挂载，配置中已有 ID 不等于仍有执行权限。此操作不删除已复制到工作区的独立快照，也不撤回已经读取或导出的内容；客户端不要承诺取消分享能清除这些副本。既有复制不自动同步源包，`copy_policy` 与锁定限制仍按服务端处理。

## 可选文件查看和导出

最低分享适配不要求新增导出 UI。如接入，以下均携带相同 Bearer token：

| GET URL | 200 响应 |
| --- | --- |
| `/api/skill-packages/{package_id}/skills/{slug}/tree?path=<相对路径>` | 目录项 JSON 数组，省略 path 表示技能根目录 |
| `/api/skill-packages/{package_id}/skills/{slug}/file?path=<相对文件路径>` | 文本 JSON：`path`、`content` |
| `/api/skill-packages/{package_id}/skills/{slug}/download?path=<相对文件路径>` | 单文件原始二进制，`application/octet-stream`，需包可见 |
| `/api/skill-packages/{package_id}/export` | 整包 ZIP 二进制，`application/zip`，需 `can_download=true` |

二进制响应不能当 JSON 解码。使用 `Content-Disposition` 提供的建议文件名，经客户端保存面板/沙盒保存到用户电脑；不要调用 `/api/downloads/local`，该接口不是客户端保存入口。ZIP 中每个技能是包含 `SKILL.md` 的顶层目录，空包或无有效技能返回 400。普通其他账号在分享包关闭 ZIP 导出后调用 export 返回 `403 SKILL_PACKAGE_DOWNLOAD_DENIED`。

## 错误与验收

| 状态 | 处理 |
| --- | --- |
| 401 | 重新认证，清理当前账号的授权状态 |
| 403 | 缺模块权限，或没有修改/复制/ZIP 导出权限；刷新详情，保留服务端错误码 |
| 404 | 包或技能不存在，或私有包对当前账号不可见；按“资源不可用”处理并刷新，不探测或泄露其他账号私有包是否存在 |
| 409 | 包名/技能重名；要求用户选择名称或明确覆盖 |
| 400、422 | 名称、文件路径、Base64 或请求字段无效；提示修正输入 |

404 隐藏规则适用于包读取及需可见性的操作；不能假定所有修改请求都返回 404，已知但非本人包的修改可能返回 `403 FORBIDDEN`。领域错误通常为 `{"error":{"code":"…","message":"…"}}`，同时兼容已有 `detail` 字符串/数组格式。

客户端用受控测试账号 A、B 和管理员验收，不改运行中的用户数据：

- [ ] 已确认目标服务能力；默认创建显式发送 `shared:false`，详情确认 false，界面显示私有。B 的包列表/项目资源不出现该包，B 读取详情为 404。
- [ ] A 明确选择分享后才发送 true；B 刷新后可见，`can_write=false`，不能上传、改元数据或删除。取消分享后 B 刷新失去访问。
- [ ] A 和管理员可管理包；管理员操作别人的包时不显示“我创建的”。`allow_download=false` 时 A/管理员仍可 ZIP 导出，B 得到下载拒绝。
- [ ] DTO 接受未知附加字段，缺少新字段显示未知；创建/PATCH 缺少 can_* 不会将权限写成 false；旧服务/空列表未证明私有能力时不会自动上传并宣称私有。
- [ ] 分享设置只更新本次字段；未选择分享、重试、缓存升级和切账号不会批量修改旧包。迟到响应不会串到其他 host/user。
- [ ] 完整 files 上传含配套文件；PUT content 只改说明且保留配套文件，PUT files/POST overwrite 的完整替换有明确用户意图。
- [ ] 操作后包详情、可写列表与项目资源刷新；不再可见的旧选择提示处理；取消分享不声称已删除既有工作区副本或历史。

契约依据：[路由及请求模型](../src/octop/api/routers/skill_packages.py)、[包可见性和操作策略](../src/octop/infra/skills/skill_package_store.py)、[包响应字段](../src/octop/infra/db/repos/skill_packages.py)、[项目资源过滤](../src/octop/infra/projects/service.py)、[专家挂载校验](../src/octop/infra/agents/manager.py)。
