"""SQL-only source receipts and read-only message copies for task branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow


@dataclass(frozen=True)
class ProjectTaskBranchRow:
    task_id: str
    owner_id: int
    request_id: str
    fingerprint: str
    source_task_id: str
    source_turn_id: str
    operation: str
    continuation_prompt: str | None
    continuation_attachments_json: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskBranchRow:
        return cls(
            task_id=str(row["task_id"]),
            owner_id=strict_int(row["owner_id"]),
            request_id=str(row["request_id"]),
            fingerprint=str(row["fingerprint"]),
            source_task_id=str(row["source_task_id"]),
            source_turn_id=str(row["source_turn_id"]),
            operation=str(row["operation"]),
            continuation_prompt=(
                str(row["continuation_prompt"]) if row["continuation_prompt"] is not None else None
            ),
            continuation_attachments_json=str(row["continuation_attachments_json"]),
            created_at=strict_int(row["created_at"]),
        )


class ProjectTaskBranchRepo:
    @staticmethod
    def by_request(conn: Any, owner_id: int, request_id: str) -> ProjectTaskBranchRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_branches WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return ProjectTaskBranchRow.from_row(row) if row is not None else None

    @staticmethod
    def origin(conn: Any, task_id: str) -> ProjectTaskBranchRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_branches WHERE task_id=?", (task_id,)
        ).fetchone()
        return ProjectTaskBranchRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(conn: Any, row: ProjectTaskBranchRow) -> None:
        conn.execute(
            "INSERT INTO project_task_branches(task_id,owner_id,request_id,fingerprint,"
            "source_task_id,source_turn_id,operation,continuation_prompt,"
            "continuation_attachments_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row.task_id,
                row.owner_id,
                row.request_id,
                row.fingerprint,
                row.source_task_id,
                row.source_turn_id,
                row.operation,
                row.continuation_prompt,
                row.continuation_attachments_json,
                row.created_at,
            ),
        )

    @staticmethod
    def insert_history(conn: Any, row: ProjectTaskTurnRow) -> None:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,status,"
            "error_code,configuration_json,model_ref,created_at,updated_at,content_blocks_json,"
            "content_revision,finished_at,reasoning) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.turn_id,
                row.task_id,
                row.request_id,
                row.prompt,
                row.reply,
                row.status,
                row.error_code,
                row.configuration_json,
                "",
                row.created_at,
                row.updated_at,
                row.content_blocks_json,
                row.content_revision,
                row.finished_at,
                row.reasoning,
            ),
        )

    @staticmethod
    def update_history_content(
        conn: Any,
        turn_id: str,
        *,
        prompt: str,
        reply: str,
        reasoning: str,
        content_blocks_json: str,
        configuration_json: str,
    ) -> None:
        conn.execute(
            "UPDATE project_task_turns SET prompt=?,reply=?,reasoning=?,content_blocks_json=?,"
            "configuration_json=? WHERE turn_id=?",
            (prompt, reply, reasoning, content_blocks_json, configuration_json, turn_id),
        )
