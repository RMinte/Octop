"""Exercise provider block shapes through the registered task stream projection."""

from langchain_core.messages import AIMessageChunk
from langchain_openai.chat_models.base import _convert_responses_chunk_to_generation_chunk
from octop_harness.protocols import resolve_protocol
from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent

from octop.infra.projects.content import TaskContent
from octop.infra.projects.stream import TASK_PROTOCOL


class Graph:
    def __init__(self, chunks):
        self.chunks = chunks

    async def astream(self, *args, **kwargs):
        for message, metadata in self.chunks:
            yield {"type": "messages", "data": (message, {"langgraph_node": "model", **metadata})}


async def test_openai_wire_summary_is_projected_after_sdk_json_parsing():
    wire = ResponseReasoningSummaryTextDeltaEvent.model_validate_json(
        '{"type":"response.reasoning_summary_text.delta","item_id":"rs_1",'
        '"output_index":0,"summary_index":0,"sequence_number":2,"delta":"公开摘要"}'
    )
    *_, generation = _convert_responses_chunk_to_generation_chunk(
        wire, -1, -1, -1, output_version="responses/v1"
    )
    assert generation is not None
    graph = Graph([(generation.message, {})])
    events = [event async for event in resolve_protocol(TASK_PROTOCOL, graph).stream([], {})]
    assert [(e["type"], e.get("content")) for e in events] == [("reasoning_summary", "公开摘要")]


async def test_responses_summary_parts_and_rounds_keep_their_boundaries():
    def summary(text, part=0):
        return [
            {
                "type": "reasoning",
                "index": 0,
                "summary": [{"type": "summary_text", "index": part, "text": text}],
            }
        ]

    chunks = [
        (AIMessageChunk(id="m1", content=summary("先检查")), {}),
        (AIMessageChunk(id="m1", content=summary("目录。")), {}),
        (AIMessageChunk(id="m1", content=summary("再核对配置。", 1)), {}),
        (AIMessageChunk(id="m1", content="", chunk_position="last"), {}),
        (AIMessageChunk(id="m2", content=summary("第二次思考。")), {}),
        (
            AIMessageChunk(
                id="m2",
                content=[{"type": "text", "index": 1, "text": "正式回答。"}],
                usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                chunk_position="last",
            ),
            {},
        ),
    ]
    events = [
        event async for event in resolve_protocol(TASK_PROTOCOL, Graph(chunks)).stream([], {})
    ]
    output = TaskContent("turn")
    for event in events:
        if event["type"] in {"token", "reasoning_summary"}:
            output.append(
                "text" if event["type"] == "token" else "reasoning_summary",
                event["content"],
                event["block_key"],
            )
        elif event["type"] == "message_end":
            output.end_text()
    assert [b.text for b in output.blocks] == [
        "先检查目录。",
        "再核对配置。",
        "第二次思考。",
        "正式回答。",
    ]
    assert [b.type for b in output.blocks] == ["reasoning_summary"] * 3 + ["text"]
    assert all(b.status == "completed" for b in output.blocks)
    assert len({b.block_id for b in output.blocks}) == 4
    assert len([e for e in events if e["type"] == "usage"]) == 1


async def test_public_reasoning_and_think_tags_work_and_internal_streams_stay_hidden():
    chunks = [
        (
            AIMessageChunk(
                id="glm", content="", additional_kwargs={"reasoning_content": "公开思考"}
            ),
            {},
        ),
        (
            AIMessageChunk(
                id="anthropic", content=[{"type": "thinking", "thinking": "另一种思考"}]
            ),
            {},
        ),
        (AIMessageChunk(id="tags", content="<think>思考片段</think>答案"), {}),
        (
            AIMessageChunk(id="internal", content="内部压缩摘要", chunk_position="last"),
            {"lc_source": "summarization"},
        ),
        (
            AIMessageChunk(
                id="encrypted", content=[{"type": "reasoning", "encrypted_content": "private"}]
            ),
            {},
        ),
    ]
    events = [
        event async for event in resolve_protocol(TASK_PROTOCOL, Graph(chunks)).stream([], {})
    ]
    assert [(str(e["type"]), e.get("content")) for e in events] == [
        ("reasoning", "公开思考"),
        ("reasoning", "另一种思考"),
        ("reasoning", "思考片段"),
        ("token", "答案"),
    ]


def test_parallel_tools_and_partial_output_have_honest_terminal_states():
    output = TaskContent("turn")
    output.append("reasoning_summary", "准备读取。", "m1")
    output.start_tool("a", "read_file")
    output.start_tool("b", "ls")
    output.start_tool("a", "read_file")
    output.end_tool("b", failed=True)
    output.append("text", "部分回答", "m2")
    output.finish(completed=False)
    assert [b.type for b in output.blocks] == ["reasoning_summary", "tool", "tool", "text"]
    assert [b.status for b in output.blocks] == [
        "completed",
        "interrupted",
        "failed",
        "interrupted",
    ]
