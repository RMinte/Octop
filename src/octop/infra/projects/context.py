"""Restore tool transcripts as context without resuming interrupted actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.config import get_config
from octop_memory import MemoryService

from octop.i18n import tr

if TYPE_CHECKING:
    from pathlib import Path

    from octop_harness import HarnessAgent


def task_backend(workspace: Path, runtime_dir: Path) -> dict[str, Any]:
    """Keep archived context readable when the project's working directory changes."""
    return {
        "type": "composite",
        "default": {"type": "local_shell", "root_dir": str(workspace), "virtual_mode": True},
        "routes": {
            "/.octop/task-history/": {
                "type": "filesystem",
                "root_dir": str(runtime_dir / "history"),
                "virtual_mode": True,
            }
        },
        "artifacts_root": "/.octop/task-history",
    }


class TaskContextMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, task_id: str, turn_id: str) -> None:
        self.task_id, self.turn_id = task_id, turn_id

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        configurable = get_config().get("configurable") or {}
        if (
            configurable.get("thread_id") == self.turn_id
            and configurable.get("ls_agent_type") != "subagent"
        ):
            # Every fresh root graph appends to the same task-owned archive.
            # Nested subagents keep their own generated session identifiers.
            return {"_summarization_session_id": self.task_id}
        return None


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


async def model_context(agent: HarnessAgent, thread_id: str) -> list[BaseMessage]:
    """Carry the effective summary and tail into the next turn's fresh graph.

    Harness history is the full transcript. DeepAgents keeps compaction in a
    separate checkpoint channel, so copying only history would expand it again.
    The original transcript remains in that turn's checkpoint and public journal.
    """
    messages = await agent.aget_history(thread_id, limit=1_000_000)
    state = await agent.protocol._graph.aget_state({"configurable": {"thread_id": thread_id}})
    event = state.values.get("_summarization_event")
    if event is None:
        return messages
    return [event["summary_message"], *messages[event["cutoff_index"] :]]


def is_compaction(prompt: str, attachment_ids: list[str]) -> bool:
    return prompt.strip() == "/compact" and not attachment_ids


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
