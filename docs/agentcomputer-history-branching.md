# AgentComputer：继承历史再次分支接线说明

更新日期：2026-10-27（Asia/Shanghai）。本次提交基线：`codex/task-execution-engines @ 4cf4e893d1cdaf411cb5a3ec5206ff9ea80dc0eb`。下述 2026-10-26 部署与验证记录的历史源码基线为 `release/1.0.0 @ 36500aa2cbe3102e3d766dae4496121536d4011d`，加当时尚未提交的历史分支修改。本文只说明此次分支行为修订；完整协议见[统一会话接线文档](agentcomputer-unified-conversations-handoff.md#44-分支编辑重新生成)。客户端 Swift/UI 本轮未修改。

历史部署记录（2026-10-26）：当日 21:55（Asia/Shanghai）按原参数平滑重启本机 `127.0.0.1:8088`，当时 PID 为 `53795`，运行上述历史基线及当时未提交的修改。该次健康、在线文档和网页均返回 `200`；在线 OpenAPI 已明确允许继承历史锚点，网页响应与当时构建一致。此记录不代表当前进程或运行版本，本次文档整理未重新部署服务。

该次重启核验于 2026-10-26 21:56 完成：当时数据库版本仍为 `57`，18 个项目、194 个 Task、816 个 Turn，以及其他项目和 Resident 记录的逐表摘要与停服备份一致，配置和环境文件未变。运维记录为 `.runtime/8088/history-branching-latest.json`；备份为 `.runtime/backups/history-branching-20261026T215456+0800`。该次未在真实数据中创建验收任务或调用模型，APP 接线与真机操作仍需按文末清单验收。

## 1. 客户端行为变化

`status == "completed"` 的轮次均可作为 `fork`、`edit`、`regenerate` 的锚点，包括 `history_only == true` 的继承历史。客户端应移除“历史副本不能再次分支”的判断和提示，保留已完成历史的三个操作入口。

请求使用**当前显示的 Task ID 和该 Task 内的 Turn ID**。例如，任务 B 从任务 A 继承了一条回答，在 B 中再次分支时，应提交 B 的 `task_id` 和 B 中该副本的 `turn_id`，不能提交 `source_task_id/source_turn_id`，也无需跳回顶部“分支自”对应的任务 A。来源链接仍可用于查看原记录。

来源 Task 必须空闲，即服务端 `active_turn_id == null`；执行中、排队中或等待审批等仍有活动轮次的 Task 不可分支。锚点必须已完成。省略 `turn_id` 时选择当前 Task 的最后一轮；最后一轮未完成时不会自动寻找更早的已完成轮次。客户端有待确认发送或本地发送队列时也应先解决发送状态，避免对过期视图操作。

## 2. 请求与响应

```http
POST /api/project-tasks/{当前_task_id}/forks
Content-Type: application/json
Authorization: Bearer <当前账号令牌>
```

三个示例中的 ID 均来自当前 Task 详情；每次新的用户意图生成新的 `request_id`。

**从该回答分支**：复制截至锚点、包含锚点的公开历史，不立即执行新轮次。

```json
{
  "request_id": "app-fork-20261026-0001",
  "operation": "fork",
  "turn_id": "turn_inherited_current"
}
```

**编辑为分支**：复制锚点之前的历史，将新文字作为新分支的下一轮用户消息；原回答保持不变。

```json
{
  "request_id": "app-edit-20261026-0001",
  "operation": "edit",
  "turn_id": "turn_inherited_current",
  "prompt": "请按新的条件重新分析。"
}
```

**重新生成**：复制锚点之前的历史，沿用锚点的用户消息及可用输入附件，在新分支执行新轮次。请求中**不要编码 `prompt` 字段**，包括客户端编辑框里显示的原文。

```json
{
  "request_id": "app-regen-20261026-0001",
  "operation": "regenerate",
  "turn_id": "turn_inherited_current"
}
```

补充字段及语义：

| 字段 | 规则 |
| --- | --- |
| `request_id` | 8–128 个字符，仅字母、数字、`_`、`-`；网络重传复用原值和原始请求。 |
| `operation` | `fork`、`edit`、`regenerate`，省略默认为 `fork`。 |
| `prompt` | `edit` 必填；`regenerate` 不传；`fork` 可选，传入新文字时分支后立即受理新轮次。 |
| `project_id` | 省略继承当前来源 Task 的项目；显式 `null` 为无项目。 |
| `selected_expert_id` | 省略继承当前来源 Task 的所选专家；显式 `null` 为通用助手。Swift 编码需保留省略、`null`、有值三态。 |

此接口不接受 `options` 或 `expected_revision`。成功统一返回 **HTTP `202`、`TaskDetail`**，普通无 `prompt` 的 `fork` 也返回 `202`，此时只是创建空闲分支，没有新执行。客户端导航到响应的 `task_id`，直接使用服务端返回的 `title`，按返回状态展示空闲或活动轮次；不能将 `202` 一律显示为“正在生成”。

## 3. 历史与执行边界

- 原 Task、原轮次及回答不被覆盖。新分支有自己的 Task ID、轮次 ID 和来源关联；已完成的继承回答可以继续再次分支。
- `history_only` 仍表示公开历史副本：不携带可执行检查点、审批授权、暂停恢复状态或工具执行状态。不能据此调用旧执行的 `retry/resume`，历史工具卡片也不能恢复为可交互执行。
- `edit/regenerate` 或带新文字的 `fork` 是新执行，工具可能再次执行，并按当前规则重新审批。后续执行采用目标分支当前的项目、专家、目录与能力配置，每轮固定实际配置快照，不恢复锚点当时的执行授权。
- 可复制的输入附件和已发布成果先校验字节长度及摘要，再保存为新 Task 所属的独立 ID 和内容，历史链接随之替换。客户端应使用响应中的新附件及成果引用。旧式 `legacy_attachments` 仍按原鉴权来源和可用性展示，不表示已经复制成工作目录文件。
- 工作目录文件不会因分支自动复制；目标 Task 的执行目录仍由目标项目配置决定，未配置时使用 Task 独立空间。

## 4. 幂等与错误处理

同一账号下，相同 `request_id`、相同来源 Task 和相同原始请求返回同一分支，不重复创建或重复受理新轮次。修改锚点、文字、操作或目标配置时生成新 `request_id`；重试不要在省略字段与显式 `null` 之间切换。幂等写入成功后再次读取到的 Task 状态可能已经推进，应以返回详情为准。

| HTTP / 错误码 | 客户端处理 |
| --- | --- |
| `409 TASK_BUSY` | 刷新当前 Task，待其空闲后由用户再次操作。 |
| `409 TASK_RETRY_UNAVAILABLE` | 锚点缺失、未完成或无法形成可执行输入；刷新详情，不因 `history_only` 单独隐藏按钮。 |
| `409 TASK_REQUEST_CONFLICT` | 同一 `request_id` 对应了不同请求；保留草稿，修正请求身份，不自动换 ID 重复执行旧意图。 |
| `404 TASK_ATTACHMENT_UNAVAILABLE` / `TASK_ARTIFACT_UNAVAILABLE` | 所需附件或成果缺失、校验失败；明确展示错误，不静默丢弃后继续执行。 |
| `404 NOT_FOUND` / `409 PROJECT_ARCHIVED` | 来源或目标不可用，或目标项目已归档；刷新资源选择。 |
| `422` | 请求不符合模型，例如 `regenerate` 携带非空 `prompt`、`edit` 缺少 `prompt` 或传入额外字段；修正编码。 |

失败响应沿用统一错误信封与账号鉴权。网络超时重传原请求；不要自动把请求改到最初来源 Task。

## 5. 最小验收

1. A 的已完成回答分支为 B；B 中继承回答显示三种操作，无需回到 A。
2. 在 B 的继承回答上分别调用 `fork/edit/regenerate`，请求使用 B 的 Task/Turn ID，成功后导航至各自新 Task，A、B 原记录保持不变。
3. 无 `prompt` 的 `fork` 返回 `202` 且保持空闲；`edit/regenerate` 受理新轮次，`regenerate` 的实际请求没有 `prompt` 字段。
4. 空闲约束生效；未完成轮次不可作为锚点。历史副本继续禁止恢复旧执行，历史工具不取得交互权限。
5. 原请求重传不重复创建；同 ID 改请求返回冲突。项目/专家的省略和显式 `null` 按各自语义生效，新轮次采用目标当前配置。
6. 复制后的附件、成果可通过新 Task 引用访问；缺失附件明确报错，工作目录文件没有被当作历史附件自动复制。

历史源码验证（2026-10-26，对应上述历史基线及当时修改）：`make all` 通过（6749 passed、31 skipped）；网页测试 92 passed；前端构建通过。当时全仓 ESLint 另有两处既有错误，修改文件检查通过。这些数字仅对应当时源码，不作为当前提交的验证结果。

2026-10-27 提交前复核：当日已有完整 `make all RUN='uv run --no-sync' PYTEST_JOBS=6` 通过记录（6946 passed、31 skipped），详见[执行引擎验证记录](task-execution-engines.md#codex-一键连接2026-10-27后续改造)。本次 5 个代码与测试文件逐一比对上次提交前保存的完整工作区备份，内容一致，沿用该次全量验证；本次另行复验后端分支及统一会话测试 42 passed、网页任务会话测试 99 passed，TypeScript 和相关 Python 文件的 Ruff 检查通过。APP 接线与真机验收仍需按上述清单进行。
