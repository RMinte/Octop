"""SQL journal for client tool handoffs. Tokens/results never appear in public views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ProjectTaskToolCallRow:
    call_id: str
    task_id: str
    turn_id: str
    owner_id: int
    client_id: str
    name: str
    arguments_json: str
    status: str
    claim_request_id: str
    claim_token: str
    result_json: str
    error_code: str
    created_at: int
    updated_at: int
    expires_at: int
    approval_context_json: str = "{}"

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskToolCallRow:
        return cls(
            call_id=str(row["call_id"]),
            task_id=str(row["task_id"]),
            turn_id=str(row["turn_id"]),
            owner_id=strict_int(row["owner_id"]),
            client_id=str(row["client_id"]),
            name=str(row["name"]),
            arguments_json=str(row["arguments_json"]),
            status=str(row["status"]),
            claim_request_id=str(row["claim_request_id"]),
            claim_token=str(row["claim_token"]),
            result_json=str(row["result_json"]),
            error_code=str(row["error_code"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            expires_at=strict_int(row["expires_at"]),
            approval_context_json=str(row["approval_context_json"]),
        )


class ProjectTaskToolCallRepo:
    @staticmethod
    def get(conn: Any, call_id: str) -> ProjectTaskToolCallRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_tool_calls WHERE call_id=?", (call_id,)
        ).fetchone()
        return ProjectTaskToolCallRow.from_row(row) if row else None

    @staticmethod
    def open_for_client(conn: Any, owner_id: int, client_id: str) -> list[ProjectTaskToolCallRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_tool_calls WHERE owner_id=? AND client_id=? "
            "AND status IN ('pending', 'claimed') ORDER BY id",
            (owner_id, client_id),
        ).fetchall()
        return [ProjectTaskToolCallRow.from_row(row) for row in rows]

    @staticmethod
    def insert(conn: Any, row: ProjectTaskToolCallRow) -> None:
        conn.execute(
            "INSERT INTO project_task_tool_calls(call_id,task_id,turn_id,owner_id,client_id,name,"
            "arguments_json,status,created_at,updated_at,expires_at,approval_context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.call_id,
                row.task_id,
                row.turn_id,
                row.owner_id,
                row.client_id,
                row.name,
                row.arguments_json,
                row.status,
                row.created_at,
                row.updated_at,
                row.expires_at,
                row.approval_context_json,
            ),
        )

    @staticmethod
    def claim(conn: Any, call_id: str, request_id: str, token: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_tool_calls SET status='claimed',claim_request_id=?,claim_token=?,"
            "updated_at=? WHERE call_id=? AND status='pending'",
            (request_id, token, now, call_id),
        )

    @staticmethod
    def complete(conn: Any, call_id: str, result_json: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_tool_calls SET status='completed',result_json=?,error_code='',updated_at=? "
            "WHERE call_id=? AND status='claimed'",
            (result_json, now, call_id),
        )

    @staticmethod
    def close(conn: Any, call_id: str, status: str, error: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_tool_calls SET status=?,error_code=?,updated_at=? "
            "WHERE call_id=? AND status IN ('pending','claimed')",
            (status, error, now, call_id),
        )

    @staticmethod
    def close_turn(conn: Any, turn_id: str, status: str, error: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_tool_calls SET status=?,error_code=?,updated_at=? "
            "WHERE turn_id=? AND status IN ('pending','claimed')",
            (status, error, now, turn_id),
        )
