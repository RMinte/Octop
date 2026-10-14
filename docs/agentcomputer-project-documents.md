# AgentComputer 项目文档与受控读取

当前本机 Task 可通过一次 `read_project_document` 工具调用读取所属项目的文本资料。这个切片用于把 Project 资料接入模型回答；它只支持读取，不允许模型写文件、运行终端命令或访问任意工作区路径。原纯文本 Task 不需要工具调用，仍按原完成证据验证。

## 使用流程

1. 在已验证的盒子 Project 中导入一个 `.md` 或 `.txt` 文档。文件名只允许 1–128 个字母、数字、点、下划线或连字符，不能以点开头，也不能包含目录或 `documents/` 前缀。UTF-8 内容最多 16,384 字节。
2. 客户端可调用 `PUT /api/agentcomputer/v1/projects/{projectID}/documents/{filename}`，JSON 正文为 `{"content":"..."}`。成功返回 201；同名文件返回 409，接口不会覆盖原文件。`GET /projects/{projectID}/documents` 返回排序后的 `items`，`GET /projects/{projectID}/documents/{filename}` 返回 `filename` 与 `content`。三条路由均使用完整的 `/api/agentcomputer/v1` 前缀和当前 Octop 用户 Bearer 会话，且只访问本人项目。
3. 在该项目的 Task 中发问，并明确指定要读取的文件名，例如“请读取 `brief.md`，再回答其中的项目代号”。模型可发出一次 `read_project_document({"path":"brief.md"})`；工具参数不能写路径前缀或跨目录。客户端等待 Run 成功后通过原 `/runs/{runID}/result` 读取最终回答。

导入只接受已有且已验证的受管项目工作区，不启动模型。资料不存在、超过限额或无法读取时，工具会返回有界的 `notFound`、`tooLarge` 或 `unreadable` 状态，不把其他路径内容交给模型。HTTP 读取超限文件返回 413；非法文件名或正文返回 422。项目资料和 Task 仍属于盒子上的 Octop 用户会话，目前没有设备配对或跨账号共享。

## Run 完成证据

受托工具入口只接受当前 Run 的一条 `read_project_document` 调用，先将固定参数和项目工作区摘要记入效果日志，再以一次 CAS 标记派发并读取文件。工具结果以有界规范 JSON 写入 `terminal` 收据；旧的内建文件、网络和终端工具继续关闭。

只有同一 Turn 的 v2 历史呈现完整 `Human → AI(tool_call) → Tool(success) → AI(final)` 四条消息，调用 ID、名称、参数摘要、结果摘要与效果日志一致，且持久检查点已到根终态时，控制库才用单事务保存 v2 完成证据、写 `run.finished`、将 Run 标为 `succeeded` 并释放 Task/Project 占用。无工具 Turn 仍使用原 v1 完成证据。完成收据的外层和数据库行格式未变，因此这轮没有 schema 迁移。

如果工具已执行但模型没有可验证的最终答复，Run 保守转为 `interrupted/reconciliationRequired` 并保留双活动占用；不会自动重放读取或伪造成功。运行中的工具 Turn 不发布未经最终证据验证的部分正文。

## 本机验证与边界

2026-10-14 在独立的 `127.0.0.1:8090` GLM-5 实验实例上，先备份工作目录并检查控制库、历史库和检查点库 `quick_check=ok`，随后导入 `brief-iteration40.md`、创建新 Task `e6762c81-8365-47d5-bf8f-e10729ce7720`，提交读取请求。Run `c5ee8c26-6bce-4b84-8fa6-8b9c27948d53` 完成，结果是文件中的验证码“海星-73142”；控制库记录一条 `terminal/succeeded` 工具效果、v2 完成证据和无对账标记，历史库有四条本 Turn 消息。旧无工具 Run 的结果接口仍返回 200。实验数据备份位于忽略目录 `.runtime/backups/iteration-40-project-documents-20261014T200226`；8088 用户实例未修改。

安全复核发现受管虚拟工作区的相对读取会优先回退到盒子根目录的同名文件。现在读取和导入存在性检查均使用反查到同一受管文件的绝对虚拟 key；单测、集成测试以及重启后 8090 的认证 HTTP 实测均确认：根目录同名文件不会覆盖项目文档内容，根目录独有文件不会让项目导入误报冲突。

这一证据只覆盖本机回环、GLM 模型和受控只读文档。项目写入工具、审批续跑、产物发布、真实第二设备、Ubuntu 盒子隔离与故障恢复仍需分别验收。模型是否选择调用工具由模型决定；提示它读取文件不等于服务端保证一定调用。
