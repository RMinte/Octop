# 模型调用流水

服务端独立保存新采集的文本与视觉 LLM 逻辑调用，并提供只读接口。流水不接入现有 `/api/usage/summary`、用户 Token 配额、费用、预算或网页；不能把两套数据相加。已有统计记录不伪造为逐调用流水，也不回填历史。

## 计量单位与覆盖

一条记录对应一次被观察到的 LangChain 模型调用，不是一条用户消息、整个 Task 或一次底层 HTTP 请求。一次业务轮次可能包含多次主执行、自动摘要、标题、审核、进展说明或 peer 调用。SDK 在同一次逻辑调用内部重试 HTTP 时，仍只产生一条流水；`physical_attempts_available=false`，不宣称物理请求次数完整。

接入点包括任务主执行、自动摘要、显式上下文压缩、同步与异步 peer、任务标题、自动审核、进展说明，以及普通聊天、cron、常驻会话、主动关怀、提示词润色、专家说明生成和知识库视觉 OCR 的模型调用。模型必须由已接入的 Octop 工厂构造，并在带有实例与身份的作用域内执行。ASR/TTS、embedding、连通性探测与外部插件自行创建的独立模型不属于本轮业务 LLM 流水覆盖。

主执行与摘要可能实际使用不同模型。每条流水保存工厂中真正调用的提供商和模型，不用轮次的主 `model_ref` 替换辅助调用模型。没有发生实际模型调用的宿主模板输出、无须压缩的操作或幂等回执不创建流水。

## 登录与接口

使用现有 Octop 登录令牌 `Authorization: Bearer <Octop access_token>`，不是模型提供商的 Key：

| 接口 | 权限与范围 |
| --- | --- |
| `GET /api/usage/calls` | 当前登录用户自己的记录。 |
| `GET /api/admin/usage/calls` | 仅管理员；默认全部记录，可用 `user_id` 限定用户。 |

普通用户不能通过筛选参数读取他人流水。无法确认用户身份的系统调用，其 `user_id=null`，只出现在管理员全局查询中。接口不查询模型服务商账单。

例如查询某任务的盒子主模型调用记录：

```http
GET /api/usage/calls?task_id=task_example&purpose=main&source=box&limit=50
Authorization: Bearer <Octop access_token>
```

可组合的精确筛选参数为 `provider/model_ref/source/purpose/status/agent_id/thread_id/project_id/task_id/turn_id/submission_id`。管理员接口另支持 `user_id`。未传参数表示不限定这一维度，不按展示名、URL 或正文模糊匹配。

`source` 仅接受 `box/third_party`；`status` 接受 `running/succeeded/failed/cancelled/interrupted`。`limit` 为 1–100，默认 100；`before_id` 为上一页返回的正整数游标。无效枚举、过长字段和越界分页参数返回 422；未登录返回 401，普通用户访问管理员接口返回 403。

## 响应

```json
{
  "measurement_unit": "logical_model_invocation",
  "recording_scope": "new_calls_only",
  "physical_attempts_available": false,
  "included_in_legacy_summary": false,
  "timezone": "Asia/Shanghai",
  "generated_at": 1792627200456,
  "next_before_id": null,
  "items": [
    {
      "call_id": "36ad590f-2aaf-4eaf-9e6c-aab760a9f098",
      "parent_call_id": "",
      "submission_id": "create-request-001",
      "user_id": 7,
      "agent_id": "",
      "thread_id": "",
      "project_id": "project_example",
      "task_id": "task_example",
      "turn_id": "turn_example",
      "provider": "Box",
      "model_ref": "Box/RM-01 VLM",
      "source": "box",
      "purpose": "main",
      "started_at": 1792627200000,
      "finished_at": 1792627200400,
      "status": "succeeded",
      "usage": {
        "input_tokens": 120,
        "output_tokens": 18,
        "total_tokens": 138,
        "uncached_input_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null,
        "presence": {
          "input_tokens": true,
          "output_tokens": true,
          "total_tokens": true,
          "uncached_input_tokens": false,
          "cache_read_tokens": false,
          "cache_write_tokens": false,
          "reasoning_tokens": false
        }
      },
      "metadata": {
        "version": 1,
        "trace_id": null,
        "run_id": "36ad590f-2aaf-4eaf-9e6c-aab760a9f098",
        "parent_run_id": null,
        "request_id": "create-request-001",
        "provider_request_id": null,
        "usage_source": "usage_metadata"
      }
    }
  ]
}
```

`started_at/finished_at/generated_at` 均为 Unix 毫秒；与旧汇总接口的秒时间戳区分。`timezone` 用于用户展示，不改变时间戳。`model_ref` 是完整实际模型引用；`provider` 是配置的提供商标识。归属尚不存在或不适用时，资源 ID 返回空字符串，不创建占位任务。

`parent_call_id` 仅在能够确认模型父调用时使用，当前可为空。`metadata.parent_run_id` 是 LangChain 图运行的父链标识，不保证指向另一条模型流水。客户端不应把每个图节点都算作一次模型调用。

## Token 可用性

`usage` 逐字段保留未知值为 null。服务商明确报告 0 时保存 0；`presence` 对应字段为 true 才表示适配器明确报告过该值。缺少缓存或思考细分时不能显示“未使用缓存”或“零思考”，也不能以回复字符数推算 Token。

OpenAI 流式响应有时只提供标准化的 canonical usage，其默认 0 未必来自服务商原始 usage。缺少原始报告证明时，这类 0 保守保存为 null；可信正数仍可保存。`total_tokens` 可以由可信输入、输出推导，此时数值存在但 `presence.total_tokens=false`，用于区分派生值与明确报告。

缓存读写是输入细分，思考通常是输出细分，不把这些字段无条件再加一次总量。保留提供商适配器能够证明的值，不假设所有服务商都有相同细分或计费规则。`metadata.usage_source` 标明解析来源，可能为 `usage_metadata/response_metadata/llm_output/mixed/missing/derived`；它不保证所有实际消耗均已报告。

## 来源与用途

`source` 表示产品接入来源：显式 `BoxProviderConfig` 或 Provider `kind=box` 记为 `box`；其他所有 Provider 均记为 `third_party`，包括用户自配、自托管、Ollama 和兼容接口。不读取 `metadata.execution_source`，也不按 URL 或模型名判断。两类来源均不承诺物理部署位置；本地/云端分类能力 `execution_location` 仍为 `false`，费用、节省和金额预算仍未支持。首版没有新增来源配置页面。

常见 `purpose` 为 `main`、`summarization`、`compaction`、`title`、`approval_review`、`progress_narration`、`chat`、`resident`、`peer`、`subagent`、`cron`、`proactive`、`prompt_polish`、`expert_manifest`、`knowledge_ocr`。实际取值可能随新增业务接入扩展，客户端应允许展示未知用途。自动摘要由实际调用的摘要元数据识别；显式压缩保留 `compaction` 用途。

## 生命周期、取消与恢复

调用开始时保存 `running` 和唯一 `call_id`；收到实际结果后更新同一条记录为 `succeeded`。模型异常或取消分别记为 `failed/cancelled`，未报告 Token 继续为 null。逻辑调用成功指模型请求成功，不代表业务任务最终成功或模型返回内容通过业务校验。

暂停与恢复只为实际新发生的模型调用新增记录，不把 checkpoint 或历史用量回放重新写成调用。任务或网络请求取消不一定意味着所有后台工作都已取消；例如常驻显式压缩在断线后继续完成时，其模型调用仍可以成功。进程在写入终态前退出时，保留的 `running` 只表示未收到终态，不能据此保证模型仍在执行或断言实际零消耗；`interrupted` 是可保存的明确中断状态，不根据超时年龄猜测成功或失败。

流水写入尽量在取消过程中完成；存储故障不使业务模型调用失败。因此接口是新采集逻辑调用的可观测记录，不替代服务商对账账单，也不承诺进程崩溃或存储不可用时毫无遗漏。

## 分页、隐私和后续统计

结果按首次入库顺序倒序返回。需要旧页时，将 `next_before_id` 原样作为 `before_id` 并保留原筛选；为 null 表示当前没有更旧的一页。新调用不会插入已遍历旧页，终态更新仍使用原 `call_id`。刷新最新页与读取旧页分开处理；更改筛选时清空旧游标。这不是冻结账本快照，状态可在分页期间更新。

公开字段只有身份引用、模型、来源、用途、时间、状态和安全的用量与追踪元数据。不保存或返回提示词、回答正文、工具参数、完整模型响应、API Key、连接地址、HTTP 请求头或原始异常；元数据仅保留白名单。费用、节省、金额预算和物理重试次数均未新增，不用金额 0 代替未支持。

后续若统一统计迁移到调用流水，应明确切换采集范围与覆盖状态后再替换旧数据源，不能同时累计旧轮次用量与这些调用。接口验证见[调用流水集成测试](../tests/integration/test_model_call_ledger.py)。
