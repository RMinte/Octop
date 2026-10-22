# AgentComputer 任务文件变更接入指南

更新日期：2026 年 10 月 22 日。服务端实现基线：Octop `df033f9ab91178dbe5e7bd898b0f06bbeb6d430e`，任务 WebSocket 协议仍为 `3`。

客户端通过每轮 `TaskTurn.file_changes` 恢复“已编辑 N 个文件”、增删行数、文件列表和只读差异审核。服务端已保存差异，客户端直接展示收到的记录，不需要读取客户端目录或请求当前文件内容。接入前，服务端运行进程须已加载上述提交或包含它的后续版本。

## 数据入口

沿用现有任务详情和订阅，不新增命令、订阅、公开文件事件或审核接口。

| 入口 | 文件变更字段路径 |
| --- | --- |
| `GET /api/project-tasks/{task_id}`，包括历史分页 | `turns[].file_changes` |
| WebSocket `snapshot` | `data.turns[].file_changes` |
| WebSocket `turn` | `data.turn.file_changes` |
| 返回 `TaskDetail` 的 WebSocket `result` | `data.turns[].file_changes` |
| 现有 SSE 轮次事件 | `data.turn.file_changes`，即 `TaskUpdate.turn.file_changes` |

登录鉴权、任务归属、游标和断线恢复沿用[任务 WebSocket 指南](project-task-websocket.md)。完整字段约束见[JSON Schema](project-task-websocket.schema.json)。

可直接用于解码联调的完整虚构帧见 [文件变更 turn 示例](examples/project-task-file-changes-turn.json)。其中 ID、时间和路径均为示例值，不代表真实任务或可访问文件。

## 字段与示例

`TaskTurn.file_changes` 是该轮已保存的成功文件工具记录的完整数组，没有记录时为 `[]`。同一路径允许多条记录，数组元素没有独立的文件 ID 或变更 ID。

| 字段 | JSON 类型 | 含义 |
| --- | --- | --- |
| `path` | `string` | 服务端工作区相对展示路径，例如 `src/app.ts`；不是客户端文件 URL。 |
| `additions` | 非负整数或 `null` | 本次工具执行前后文本差异的新增行数；`null` 表示未知。 |
| `deletions` | 非负整数或 `null` | 本次工具执行前后文本差异的删除行数；`null` 表示未知。 |
| `diff` | `string` 或 `null` | 保存的 unified diff 文本；`null` 表示无法取得完整文本差异。 |

以下是轮次字段摘录，不是完整 WebSocket 帧：

```json
{
  "turn_id": "turn_example",
  "content_revision": 7,
  "file_changes": [
    {
      "path": "src/app.ts",
      "additions": 1,
      "deletions": 1,
      "diff": "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-const title = 'old';\n+const title = 'new';\n"
    },
    {
      "path": "src/app.ts",
      "additions": 1,
      "deletions": 1,
      "diff": "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-const title = 'new';\n+const title = 'final';\n"
    },
    {
      "path": "assets/logo.png",
      "additions": null,
      "deletions": null,
      "diff": null
    }
  ]
}
```

本例应显示两个文件；`src/app.ts` 为 `+2 / −2`，并按数组顺序展示两次差异。这是两次操作的累计值，不是轮次开始到结束的净差异。另一文件统计未知，因此卡片总计也应保留未知。

`content_blocks[].file_changes` 也保存同一批工具记录。客户端卡片使用顶层 `TaskTurn.file_changes` 即可，不要再把内容块中的记录加一次。`file_changes` 不是新的内容块类型。

## Swift 解码

新增独立的只读 DTO；不需要构造带本机执行、撤销或权限信息的写入提案。

```swift
struct TaskFileChangeDTO: Decodable, Equatable, Sendable {
    let path: String
    let additions: Int?
    let deletions: Int?
    let diff: String?
}
```

在现有 `TaskTurn` DTO 中加入 `[TaskFileChangeDTO]`，映射 JSON 键 `file_changes`；内容块 DTO 中对应字段使用相同类型。根据当前服务端合同，无记录返回空数组。三个可空字段必须保留 `nil`，不能默认填 `0` 或空字符串。

## 快照合并与缓存

`file_changes` 没有独立版本，与 `content_blocks` 共用本轮的 `content_revision`。客户端按原始字符串 `task_id / turn_id` 定位轮次，并将两个数组作为同一组内容保存和替换。

```text
如果收到的 content_revision >= 本地 content_revision：
    用收到的 content_blocks 替换本地 content_blocks
    用收到的 file_changes 替换本地 file_changes
    保存收到的 content_revision
否则：
    保留本地这组内容
```

这段规则只处理内容字段；摘要、状态、计划和审批继续遵循原协议各自的版本与合并规则。不能因为 `list_revision` 未变、回复文字未增长，或本地轮次已是终态，就忽略较新的文件变更内容。

每次收到的数组都是完整快照，不能追加到旧数组。也不能通过 `path` 或差异正文去重原始记录：同一文件的多次合法修改需要保留。重连重复收到同一快照时，替换后文件数和行数应保持不变。

缓存必须同时保存 `content_revision`、`content_blocks` 和 `file_changes`，重新打开任务时恢复这组数据。历史分页的轮次仍按 `turn_id` 合并，不用历史页推进实时游标。追问和手动重试产生新轮次，各轮变更分开；暂停后恢复同一轮继续使用原 `turn_id`。

## 卡片与审核展示

按当前轮次内的精确 `path` 分组，文件数为分组数量。组内累加每次操作的增删行数，并保留各条差异的数组顺序；界面行身份可以使用 `turn_id + path`，不要将客户端路径解析结果作为身份。

默认显示前三个文件，支持“再显示 N 个文件”和收起。点击“审核”打开只读差异视图，点击文件名直接选择该文件；差异中的 `+`、`-`、`@@` 和文件头可以分别着色，但正文作为普通文本展示。

| 数据情况 | 展示规则 |
| --- | --- |
| `file_changes: []` | 不显示文件变更卡片。 |
| 行数已知 | 显示实际增删行数，零保持为零。 |
| 某次操作的某项行数为 `null` | 该文件和卡片总计的对应项均为未知，例如 `+?`；不把已知部分显示成完整总计。 |
| 某条 `diff: null` | 提示缺少完整快照；同文件其他已知差异仍可查看。 |
| `diff: ""` 且行数为 `0 / 0` | 仍保留文件记录；成功创建或删除空文件可以产生这种结果，展示空差异提示即可。 |
| 整轮失败、取消、暂停或中断，但有记录 | 保留已经成功的文件修改，不要求整轮 `completed` 才显示。 |

文件卡片独立于“步骤面板”和“动作链路”展示开关，放在该轮正式回复之后、输出附件区域之前。`artifacts` 是正式发布的交付附件，与这里的工作文件变更分别展示和计数。

审核直接读取保存的 `diff`，无需新增网络请求，也不打开 `path` 指向的本机文件。当前没有撤销、写回、批准执行或重新执行接口；不能把“审核”接到工具审批操作。

## 当前覆盖范围

当前只记录主任务的服务端 `write_file`、`edit_file` 和 `delete` 文件工具成功执行后的观测差异。失败调用、未产生实际变化的调用及内部工作文件不计入。无法取得完整文本快照时保留路径，但统计和差异未知。

| 待接入来源 | 当前行为 |
| --- | --- |
| 命令 `execute` | 命令产生的文件修改不进入文件变更卡片。 |
| MCP 工具 | 外部 MCP 工具的文件副作用不进入卡片。 |
| 客户端 `client__` 工具 | 客户端文件修改尚无此类收据；服务端不探测或读取客户端工作目录。 |
| 子代理与团队成员 | 嵌套执行的文件修改尚未汇入主任务卡片。 |

旧轮次没有快照时不补录。`imported_history.file_changes` 属于外部来源历史，不是本次真实执行收据，不自动并入本卡片。空数组仅表示没有可展示的收据，不能据此断言任务没有修改文件。

## 客户端联调检查

| 场景 | 预期结果 |
| --- | --- |
| 一个文件连续修改两次 | 一行文件、累计行数、两份有序差异。 |
| 同一完整快照重复推送、重连或重新打开 | 不追加记录，不重复累计。 |
| `content_revision` 增长，回复和 `list_revision` 不变 | 卡片仍更新；两个内容数组采用同一版本。 |
| 较旧快照迟到，或终态后收到较新内容 | 拒绝旧内容；允许较新内容刷新，状态另按原规则合并。 |
| 四个以上文件 | 默认三行，可展开、收起、切换审核文件。 |
| 已知与未知统计混合 | 对应总计保持未知，已知差异仍可查看。 |
| 空文件新建或删除 | 保留 `0 / 0` 文件记录，允许空差异。 |
| 部分写入成功，后续工具失败或用户停止 | 已成功修改留在原轮次；失败调用不增加记录。 |
| 新轮追问、重试、同轮恢复 | 新轮不继承旧轮记录；同轮恢复不重复累计。 |
| 关闭两个过程展示开关 | 文件卡片仍可显示和审核。 |

服务端真实文件工具和历史持久化用例见 [test_project_task_file_changes.py](../tests/integration/test_project_task_file_changes.py)，空文件、失败及未知快照用例见 [test_file_changes.py](../tests/unit/projects/test_file_changes.py)。原生客户端的接线验收应覆盖上表，服务端验证不代替 APP 联调。
