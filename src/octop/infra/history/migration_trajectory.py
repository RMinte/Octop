"""Public legacy trajectory copies carry provenance, never execution authority."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from octop.infra.projects.content import TaskContentBlock
from octop.infra.projects.legacy_conversation_models import LegacyTrajectoryEvent
from octop.infra.utils.llm_text import strip_thinking

_PRIVATE = {
    "reasoning",
    "reasoning_content",
    "thinking",
    "analysis",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "credentials",
    "secret",
    "token",
}
_PRIVATE_BLOCKS = {"reasoning", "thinking", "analysis", "redacted_thinking"}


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        block_type = value.get("type")
        if isinstance(block_type, str) and block_type in _PRIVATE_BLOCKS:
            return None
        return {
            str(key): _public(child)
            for key, child in value.items()
            if str(key).lower().replace("-", "_") not in _PRIVATE
        }
    if isinstance(value, list):
        return [public for child in value if (public := _public(child)) is not None]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except ValueError:
                pass
            else:
                return _public(structured)
        return strip_thinking(value)
    return value


def _timestamp(value: Any) -> datetime | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def attach_trajectory(
    turns: list[Any], events: list[dict[str, Any]]
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """Match explicit source IDs only; otherwise retain the thread-level event."""
    groups: list[list[dict[str, Any]]] = [[] for _ in turns]
    for turn in turns:
        for block in turn.blocks:
            if block.type == "tool":
                block.arguments = _public(block.arguments)
                block.output = _public(block.output)
    messages = {
        identifier: index for index, turn in enumerate(turns) for identifier in turn.message_ids
    }
    calls = {
        block.tool_call_id: (index, block)
        for index, turn in enumerate(turns)
        for block in turn.blocks
        if block.tool_call_id
    }
    warnings: set[str] = set()
    for raw in events:
        kind = str(raw.get("kind") or "unknown")
        raw_payload = raw.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        block_type = payload.get("type")
        visible = kind in {"user", "assistant", "tool"} and not (
            isinstance(block_type, str) and block_type in _PRIVATE_BLOCKS
        )
        public = _public(payload) if visible else {}
        status = "recorded" if visible else "redacted"
        call_id = str(payload.get("call_id") or "")
        message_id = str(payload.get("message_id") or "")
        index, mapping = 0, "source_thread"
        if call_id and call_id in calls:
            index, _block = calls[call_id]
            mapping = "tool_call"
        elif message_id and message_id in messages:
            index, mapping = messages[message_id], "message"
        else:
            warnings.add("trajectory_thread_level_mapping")
        if kind == "tool" and "result" not in payload:
            status = "incomplete"
            warnings.add("trajectory_incomplete_tool")
        event = LegacyTrajectoryEvent.model_validate(
            {
                "source_event_id": str(raw["event_id"]),
                "source_sequence": raw["seq"],
                "source_turn_id": raw.get("turn_id"),
                "source_agent_id": raw["agent_id"],
                "kind": kind,
                "created_at": _timestamp(raw.get("ts")),
                "summary": strip_thinking(str(raw.get("summary") or ""))
                if status != "redacted"
                else "",
                "payload": public,
                "source_status": status,
                "mapping": mapping,
                "speaker_agent_id": public.get("speaker_agent_id"),
                "speaker_name": public.get("speaker_name"),
            }
        )
        groups[index].append(event.model_dump(mode="json"))
        if kind == "tool":
            existing = calls.get(call_id) if call_id else None
            if existing is None:
                arguments = public.get("args")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {"source_arguments": arguments}
                block = TaskContentBlock(
                    block_id="source",
                    type="tool",
                    status="interrupted"
                    if status == "incomplete"
                    else "failed"
                    if raw.get("is_error")
                    else "completed",
                    tool_call_id=call_id or None,
                    tool_name=str(public.get("name") or "tool"),
                    arguments=arguments if isinstance(arguments, dict) else None,
                    output=public.get("result"),
                    speaker_agent_id=event.speaker_agent_id,
                    speaker_name=event.speaker_name,
                    finished_at=event.created_at if status == "recorded" else None,
                )
                turns[index].blocks.append(block)
                if call_id:
                    calls[call_id] = (index, block)
            else:
                block = existing[1]
                if "result" in public and block.output is None:
                    block.output = public["result"]
                    block.status = "failed" if raw.get("is_error") else "completed"
                if block.finished_at is None and status == "recorded":
                    block.finished_at = event.created_at
        # user/assistant content is retained as a source event. It is never
        # promoted to a fresh executable message or duplicated into the reply.
    return groups, sorted(warnings)
