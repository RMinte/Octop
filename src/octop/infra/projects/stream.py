"""Task-specific Harness projection, including Responses summaries and boundaries."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from langgraph.types import Command
from octop_harness.protocols import register_protocol
from octop_harness.protocols.langgraph import LangGraphProtocol
from octop_harness.protocols.think_splitter import ThinkSplitter

TASK_PROTOCOL = "octop-project-task"


class TaskStreamProtocol(LangGraphProtocol):
    async def stream(
        self, messages: list[Any], config: dict[str, Any], **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        # Each node must have a durable predecessor before work can advance.
        kwargs["durability"] = "sync"
        async for event in super().stream(messages, config, **kwargs):
            yield event

    def _project_chunk(
        self, chunk: dict[str, Any], splitter: ThinkSplitter
    ) -> list[dict[str, Any]]:
        events = super()._project_chunk(chunk, splitter)
        if chunk.get("interrupts"):
            events = [event for event in events if event["type"] != "hitl_required"]
            for item in chunk["interrupts"]:
                request = getattr(item, "value", item)
                identifier = getattr(item, "id", None)
                if isinstance(request, dict) and isinstance(request.get("value"), dict):
                    identifier = identifier or request.get("id")
                    request = request["value"]
                if isinstance(request, dict):
                    events.append(
                        {"type": "hitl_required", "request": request, "interrupt_id": identifier}
                    )
        return events

    async def resume(
        self, values: dict[str, Any] | None, config: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        splitter = ThinkSplitter()
        async for chunk in self._graph.astream(
            Command(resume=values) if values is not None else None,
            config=config,
            stream_mode=["values", "updates", "messages", "custom"],
            version="v2",
            durability="sync",
        ):
            for event in self._project_chunk(chunk, splitter):
                yield event
        for event in self._drain_splitter(splitter):
            yield event

    def _project_messages_chunk(self, data: Any, splitter: ThinkSplitter) -> list[dict[str, Any]]:
        message, metadata = data
        # Let Harness filter tool/human/internal-compaction streams before adding boundaries.
        if (
            message.type in {"tool", "human", "system"}
            or metadata.get("lc_source") == "summarization"
        ):
            return super()._project_messages_chunk(data, splitter)
        key = (
            message.id
            or f"{metadata.get('langgraph_node', '')}:{metadata.get('langgraph_step', '')}"
        )
        out: list[dict[str, Any]] = []

        def project(content: Any, part: str, *, summary: bool = False, **updates: Any) -> None:
            chunk = message.model_copy(update={"content": content, **updates})
            for event in super(TaskStreamProtocol, self)._project_messages_chunk(
                (chunk, metadata), splitter
            ):
                if event["type"] in {"token", "reasoning"}:
                    event["block_key"] = f"{key}:{part}"
                    if summary and event["type"] == "reasoning":
                        event["type"] = "reasoning_summary"
                out.append(event)

        empty: dict[str, Any] = {
            "additional_kwargs": {},
            "tool_call_chunks": [],
            "usage_metadata": None,
            "response_metadata": {},
        }
        project("", "reasoning", **{**empty, "additional_kwargs": message.additional_kwargs})
        content = message.content
        if isinstance(content, list):
            for position, block in enumerate(content):
                part = (
                    str(block.get("index", position)) if isinstance(block, dict) else str(position)
                )
                if isinstance(block, dict) and block.get("type") == "reasoning":
                    for index, item in enumerate(block.get("summary") or []):
                        if item.get("type") == "summary_text" and item.get("text"):
                            project(
                                [{"type": "thinking", "thinking": item["text"]}],
                                f"{part}:summary:{item.get('index', index)}",
                                summary=True,
                                **empty,
                            )
                    if block.get("reasoning"):
                        project(
                            [{"type": "thinking", "thinking": block["reasoning"]}], part, **empty
                        )
                else:
                    project([block], part, **empty)
        elif content:
            project(content, "text", **empty)
        project("", "tools", additional_kwargs={})
        if getattr(message, "chunk_position", None) == "last" or message.response_metadata.get(
            "finish_reason"
        ):
            for event in self._drain_splitter(splitter):
                event["block_key"] = f"{key}:text"
                out.append(event)
            out.append({"type": "message_end"})
        return out


register_protocol(TASK_PROTOCOL, TaskStreamProtocol)
