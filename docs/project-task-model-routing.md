# 任务模型路由接入

服务端在现有任务创建、追问和重试请求中接受 `options.routing`，由盒子估计本轮工作难度，再固定本轮实际执行模型。客户端调用这些接口；网页「模型」页的可用模型池提供盒子与远端两组独立星标，Swift 客户端协议保持不变。

Task 仍是多轮会话。每次新消息单独判难，同一轮中的执行、暂停后继续及恢复使用已保存的模型。第一句问候走盒子，不会把之后的复杂重构也固定在盒子。

## 认证与能力发现

REST 使用现有 Octop 登录令牌 `Authorization: Bearer <Octop access_token>`，JSON 请求同时使用 `Content-Type: application/json`。此令牌不是盒子或远端模型服务商的 API Key。WebSocket 使用现有 `/api/project-tasks/ws`，连接后先发送 `authenticate`；认证与命令协议不变。

先读取 `GET /api/project-tasks/resources`。`models`、`default_model_ref` 继续提供可显式选择的主模型；新增 `routing` 声明路由能力和当前配置，例如：

```json
{
  "default_model_ref": "remote/chat-model",
  "routing": {
    "modes": ["box", "hybrid"],
    "levels": [0, 20, 40, 60, 80, 100],
    "default_level": 60,
    "box_model_ref": "Box/RM-01 VLM",
    "remote_model_ref": "remote/chat-model",
    "box_available": true,
    "remote_available": true
  }
}
```

以上模型引用是示意，须使用服务端实际返回值。`provider_kind=box` 可用于客户端分组；显式选择仍提交完整 `model_ref`。资源响应不返回服务地址、密钥或连接头，ASR/TTS 不进入主模型列表。语音来源仍由独立语音配置控制。

## 部署策略配置

模型池中，盒子星标通过 `PATCH /api/preferences` 的 `preferred_box_model` 保存，远端星标沿用 `preferred_model`。两者属于当前用户，互不覆盖；旧的盒子 `preferred_model` 仍可作为盒子偏好读取。任务受理和资源接口按当前用户解析，修改星标从下一轮生效。

每侧按「可用用户偏好 → 同类候选的旗舰优先排序」解析，不再要求用户重复绑定。无有效星标时，旧部署绑定和全局默认不覆盖任务自动选择。`GET /api/project-tasks/routing/settings` 返回当前用户解析后的模型引用。`PUT` 同一路径需要 `providers` 管理权限，接受完整配置并返回保存值：

```json
{
  "box_model_ref": "Box/RM-01 VLM",
  "remote_model_ref": "remote/chat-model",
  "default_level": 60
}
```

`PUT` 配置仍保存到服务端部署范围，保留旧接口读写兼容；当前用户的任务自动路由仅使用其中的默认档位，模型引用由星标和自动排序解析。盒子引用必须指向可用的 `box` 主模型。默认档位大于 0 时，远端引用必须指向可用的非 `box` 主模型；默认档位为 0 时允许 `remote_model_ref=null`，只配置盒子即可使用。

### 无有效星标时的自动排序

仅在已经配置、启用且满足图片等能力要求的候选内排序，不添加模型、不自动申请权限。`ProviderStore` 的自动选择与执行失败后的候选队列使用同一顺序；盒子和远端仍各自筛选，不因回退越过来源约束。运行时继续检查健康状态和上下文容量，显式指定模型不受该排序覆盖。

识别 GLM、GPT、Claude、Gemini 的明确产品命名：优先旗舰档位，再比较同系列数字版本（如 5.10 新于 5.9）。Turbo/Sonnet 和 Flash/Mini 等档位随后。未知名字保留原顺序，排在已识别系列后；不把上下文长度、参数量字符串或任意数字当作综合实力。不同系列之间没有统一版本或发布日期，处于相同档位时沿用候选首次出现的系列顺序，不声称跨厂商质量排名或全网最新。未识别的新命名需更新规则或设置星标。

例如已配置的 GLM-5.2、GLM-5.1、GLM-5、GLM-5-Turbo、GLM-5V-Turbo、GLM-4.7-Flash，在没有星标时按上述顺序尝试；图片请求先过滤图片能力。星标 GLM-4.7-Flash 仍优先处理其支持的请求，失效后才进入自动队列。

档位参考厂商产品定位：[智谱模型概览](https://docs.bigmodel.cn/cn/guide/start/model-overview)、[OpenAI 模型目录](https://developers.openai.com/api/docs/models)、[Claude 模型概览](https://platform.claude.com/docs/en/models/overview)、[Gemini 模型目录](https://ai.google.dev/gemini-api/docs/models)。这是确定性的选择策略，并非基准测试或逐次联网发现；语音与搜索配置独立，不参与主对话模型排序。

`default_level` 只为显式开启的 `hybrid` 请求补齐省略或为 null 的 `level`，不会接管现有全局默认模型，也不会使省略 `routing` 的旧客户端自动进入混合模式。修改配置只影响之后受理的轮次。

## 每轮选择与优先级

| 请求中的选择 | 本轮行为 |
| --- | --- |
| 非空 `options.model_ref` | 使用该完整模型引用，优先于 `routing`，不调用判难模型。 |
| 无显式模型，`routing.mode=box` | 使用配置的盒子模型，不判难；`level` 只能省略、null 或 0。 |
| 无显式模型，`routing.mode=hybrid` | 使用本轮 `level`，省略或 null 时采用部署默认档位。 |
| `routing` 省略或为 null | 保持原有默认模型选择，不判难、不继承上一轮的路由选择。 |

只接受 `box` 和 `hybrid` 两种模式，不接受 `remote` 模式；需要固定远端时使用 `options.model_ref`。档位必须是整数 `0/20/40/60/80/100`，布尔值、字符串、浮点数、其他数值和未知字段均被拒绝。

混合档位控制难度阈值，数字越高，越多较低难度的工作可以走远端：

| 档位 | 选择远端的难度条件 |
| --- | --- |
| 0 | 始终盒子，不调用判难模型，不要求远端配置。 |
| 20 | 难度 ≥ 80。 |
| 40 | 难度 ≥ 60。 |
| 60 | 难度 ≥ 40。 |
| 80 | 难度 ≥ 20。 |
| 100 | 难度 ≥ 10；0–9 的基础工作仍使用盒子。 |

这些数字不是任务占比、调用比例、随机概率或费用比例。难度由盒子根据本轮消息、有效历史摘要、当前项目行动指南及附件元数据估计，不按消息长度或工具数量机械打分。判难不读取附件内容，不执行工具，也不调用远端判难。

本轮或历史包含图片时，最终主模型仍须具备图片能力，包括只保存在有效工具上下文中、没有输入附件元数据的图片。`hybrid` 大于 0 档允许在配置的盒子不能处理图片而远端可以时选择远端，原因记录为 `capability_image`；若盒子支持图片、可用远端候选仅支持文本，则保留盒子并记录 `capability_image_box`。这两类能力已确定的选择跳过判难，`difficulty=null`。`box` 或 0 档保持盒子约束，不自动启用远端。

## REST 请求

创建无项目任务：

```http
POST /api/project-tasks
Authorization: Bearer <Octop access_token>
Content-Type: application/json
```

```json
{
  "request_id": "routing-create-001",
  "prompt": "请分析并修复这组模块之间的依赖问题。",
  "options": {
    "routing": {"mode": "hybrid", "level": 60}
  }
}
```

关联项目时，在同一请求增加 `project_id`，或使用已有 `POST /api/projects/{project_id}/tasks`。追问使用同一任务 ID：

```http
POST /api/project-tasks/task_example/messages
Authorization: Bearer <Octop access_token>
Content-Type: application/json
```

```json
{
  "request_id": "routing-followup-002",
  "prompt": "继续检查边界条件，并补充验证。",
  "options": {
    "routing": {"mode": "hybrid", "level": 80}
  }
}
```

最后一个失败或取消轮次可手动重试，使用原消息和附件，但根据这次请求的选项重新选择模型：

```http
POST /api/project-tasks/task_example/turns/turn_example/retry
Authorization: Bearer <Octop access_token>
Content-Type: application/json
```

```json
{
  "request_id": "routing-retry-003",
  "options": {
    "routing": {"mode": "hybrid", "level": 100}
  }
}
```

这三个接口成功均返回 `202 Accepted` 和现有 `TaskDetail`。请求受理可能包含一次有时限的盒子判难；判难期间不持有数据库写事务。服务端受理前会重新检查项目、历史与策略等输入是否仍一致；变化冲突返回 `409 TASK_REVISION_CONFLICT`，不提交沿用过时判难结果的新轮次。

## WebSocket 请求

完成认证后，沿用现有命令，把选项放入 `data.options`：

```json
{
  "type": "task.create",
  "id": "command-create-001",
  "data": {
    "request_id": "routing-create-001",
    "prompt": "请分析并修复模块依赖问题。",
    "options": {"routing": {"mode": "hybrid", "level": 60}}
  }
}
```

```json
{
  "type": "task.message",
  "id": "command-followup-002",
  "task_id": "task_example",
  "data": {
    "request_id": "routing-followup-002",
    "prompt": "继续检查边界条件。",
    "options": {"routing": {"mode": "hybrid", "level": 80}}
  }
}
```

```json
{
  "type": "task.retry",
  "id": "command-retry-003",
  "task_id": "task_example",
  "turn_id": "turn_example",
  "data": {
    "request_id": "routing-retry-003",
    "options": {"routing": {"mode": "box"}}
  }
}
```

需要固定模型时，将同一位置的选项改为 `{"model_ref":"remote/chat-model"}`。命令响应继续使用现有 `result/error`，任务状态与正文继续通过现有订阅更新。

## 结果、幂等与恢复

`turns[].model_ref` 是实际执行模型。路由轮次另返回已固定的 `turns[].routing`，例如：

```json
{
  "model_ref": "remote/chat-model",
  "routing": {
    "mode": "hybrid",
    "level": 100,
    "target": "remote",
    "model_ref": "remote/chat-model",
    "difficulty": 65,
    "reason_code": "execution_dependencies",
    "policy_version": "difficulty-v1",
    "input_digest": "<input SHA-256>",
    "classifier_input_tokens": 120,
    "classifier_output_tokens": 18,
    "classifier_duration_ms": 420,
    "classifier_input_truncated": false
  }
}
```

`target` 为 `box/remote/explicit`；显式模型同时携带路由请求时记录 `explicit` 和 `explicit_model`，仍跳过判难。未开启路由的轮次 `routing=null`，历史记录不补造判难结果。`difficulty=null` 表示该轮未取得有效难度，并不等于难度为 0。`reason_code` 是稳定短码，不包含模型原始输出、隐藏思考、请求正文或内部异常；`input_digest` 用于对齐输入快照，不是可下载的上下文内容。

每轮独立选择。希望追问保持相同路由策略或显式模型，客户端必须在每轮再次携带相同选项；服务端不从上一轮推断。客户端应按结果的 `model_ref/routing` 展示历史，不能用输入框当前选择反推历史。标题、压缩和审核继续遵循已有的模型继承规则，不单独创建另一套混合策略。

网络结果未知时，重传同一 `request_id`、消息、附件及原始 `options`。已受理请求返回原轮次，不重新判难；之后修改部署默认档位也不会改变原结果。不能把响应中解析后的模型或补齐档位替换进原请求；同一标识更改选项返回 `409 TASK_REQUEST_CONFLICT`。两个同时到达且尚未受理的相同请求可能各自进行判难，但事务内仍只受理一个轮次。

判难超时、无效输出或调用异常时，保守使用盒子并保存 `classifier_timeout/classifier_invalid/classifier_unavailable/classifier_error` 原因。调用方真正取消提交时，取消向上传播，不继续入队。盒子模型本身不可用，或已决定使用的远端模型不可用时，返回 `409 TASK_MODEL_UNAVAILABLE`，不静默改用另一模型。图片能力不满足时返回 `422 TASK_IMAGE_MODEL_REQUIRED`。其他任务权限、忙碌、归档、重试及附件约束保持原有语义，无权访问的任务或项目在判难前即被拒绝。

## 观察与用量边界

路由次数应从已受理轮次的 `routing.target` 统计，以轮次为单位，按需区分基础盒子、远端、显式覆盖及判难失败。一个 Task 可以同时包含盒子轮次与远端轮次，不能把整个 Task 只归为一个来源；不要用模型调用次数或 Token 数作为这些档位的分母。

现有 `/api/usage/summary` 的 `by_model/by_project/by_task` 可查看已记录主任务及旁路累计用量，仍遵循原有统计覆盖边界。新增 `classifier_*` 只作为本轮判难审计，尚未并入统一用量或用户 Token 配额，不能视为完整账单；主轮次的 `model_ref` 也不能代表判难调用的模型。服务端未新增路由聚合或价格接口，不承诺精确远端占比、成本、节省或预算。

验证依据为 [路由集成测试](../tests/integration/test_project_task_routing.py)，基础任务协议见 [API 文档](api.md)与[任务 WebSocket 接入指南](project-task-websocket.md)。

## 图片与偏好

自动路由先筛选支持图片的可用模型，再应用难度策略。每侧优先使用该侧偏好模型；偏好不支持图片时，选择同侧启用的图片模型。盒子模式及 0 档绝不使用远端；混合模式两侧都有图片模型时按难度选择，只有一侧支持图片时直接使用该侧，不再判难。均不支持时返回 `TASK_IMAGE_MODEL_REQUIRED`。显式 `model_ref` 仍是固定选择，不兼容图片时直接提示错误。历史消息与工具上下文中的图片同样参与约束。

切换只写入本轮实际模型快照，不修改星标偏好；纯文本新轮次继续优先使用偏好。
