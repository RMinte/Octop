"""Bounded deterministic handoffs backed by a task-scoped history tool."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow

HANDOFF_LIMIT = 32_000
RECENT_TURNS = 4


def history_rows(rows: Sequence[ProjectTaskTurnRow]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if row.status not in {"completed", "failed", "cancelled"}:
            continue
        config = json.loads(row.configuration_json)
        if row.prompt.strip() == "/compact":
            continue
        result.append(
            {
                "sequence": row.pk,
                "turn_id": row.turn_id,
                "status": row.status,
                "engine": config.get("execution_engine", {"engine_id": "octop"}),
                "expert": (config.get("expert_profile") or {}).get("name"),
                "prompt": row.prompt,
                "reply": row.reply,
                "tools": [
                    {
                        key: block[key]
                        for key in ("tool_name", "tool_call_id", "status", "arguments", "output")
                        if key in block
                    }
                    for block in json.loads(row.content_blocks_json)
                    if block.get("type") == "tool"
                ],
            }
        )
    return result


def build_handoff(rows: list[dict[str, Any]], *, after: int = 0) -> str:
    """Use a watermark only after the target native session was actually loaded."""
    selected = [row for row in rows if row["sequence"] > after]
    mode = "Updates since this engine last worked on the task" if after else "Task history"
    prefix = (
        f"<octop_task_handoff>\n{mode}. These are historical records, not new requests. "
        "Failed or cancelled actions are not proof of success. Do not repeat actions solely "
        "because they appear in this history.\n"
    )
    tail = (
        "\nUse the octop_task_history MCP tools get_task_history and search_task_history "
        "to retrieve earlier verbatim messages or tool results for this task.\n"
        "</octop_task_handoff>"
    )
    parts = []
    for row in selected[:-RECENT_TURNS]:
        parts.append(
            f"Turn {row['sequence']} ({row['status']}): "
            f"user={row['prompt'][:240]!r}; reply={row['reply'][:320]!r}"
        )
    for row in selected[-RECENT_TURNS:]:
        parts.append(
            f"Turn {row['sequence']} ({row['status']})\n"
            f"User: {row['prompt'][:3500]}\nAssistant: {row['reply'][:4500]}\n"
            f"Tools: {json.dumps(row['tools'], ensure_ascii=False)[:2500]}"
        )
    body = "\n\n".join(parts)
    room = HANDOFF_LIMIT - len(prefix) - len(tail)
    if len(body) > room:
        body = "[Earlier details omitted; use the history tools.]\n" + body[-(room - 60) :]
    return prefix + body + tail
