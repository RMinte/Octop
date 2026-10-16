"""Restore tool transcripts as context without resuming interrupted actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from octop_memory import MemoryService

from octop.i18n import tr

if TYPE_CHECKING:
    from octop_harness import HarnessAgent


async def capture_memory(
    agent: HarnessAgent, owner_id: int, session_id: str, prompt: str, reply: str
) -> None:
    if agent.memory is None:
        return
    service = MemoryService(agent.memory, host="octop-harness")
    # Task harnesses are short-lived. Await the write before releasing their
    # memory backend instead of racing the harness background capture pool.
    await asyncio.to_thread(
        service.capture_turn,
        user=prompt,
        assistant=reply,
        session_id=session_id,
        thread_id=session_id,
        user_id=str(owner_id),
    )


def close_pending_tools(messages: list[BaseMessage], locale: str) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    pending: dict[str, str] = {}

    def close() -> None:
        for call_id, name in pending.items():
            result.append(
                ToolMessage(
                    content=tr("projects.task_incomplete_context", locale),
                    tool_call_id=call_id,
                    name=name,
                    status="error",
                )
            )
        pending.clear()

    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id in pending:
                result.append(message)
                pending.pop(message.tool_call_id)
            continue
        close()
        result.append(message)
        if isinstance(message, AIMessage):
            pending.update((str(call["id"]), call["name"]) for call in message.tool_calls)
    close()
    return result
