"""Durable server-tool execution receipts fenced by their original worker lease."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ProjectTaskExecutionReceiptRow:
    receipt_id: str
    task_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    arguments_digest: str
    state: str
    result_json: str
    worker_id: str
    created_at: int
    updated_at: int
    pk: int = 0

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskExecutionReceiptRow:
        return cls(
            receipt_id=str(row["receipt_id"]),
            task_id=str(row["task_id"]),
            turn_id=str(row["turn_id"]),
            tool_call_id=str(row["tool_call_id"]),
            tool_name=str(row["tool_name"]),
            arguments_digest=str(row["arguments_digest"]),
            state=str(row["state"]),
            result_json=str(row["result_json"]),
            worker_id=str(row["worker_id"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            pk=strict_int(row["id"]),
        )


class ProjectTaskExecutionReceiptRepo:
    @staticmethod
    def get(conn: Any, turn_id: str, call_id: str) -> ProjectTaskExecutionReceiptRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_execution_receipts WHERE turn_id=? AND tool_call_id=?",
            (turn_id, call_id),
        ).fetchone()
        return ProjectTaskExecutionReceiptRow.from_row(row) if row else None

    @staticmethod
    def start(
        conn: Any,
        receipt_id: str,
        task_id: str,
        turn_id: str,
        call_id: str,
        name: str,
        digest: str,
        worker_id: str,
        now: int,
    ) -> bool:
        return bool(
            conn.execute(
                "INSERT INTO project_task_execution_receipts("
                "receipt_id,task_id,turn_id,tool_call_id,tool_name,arguments_digest,state,worker_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,'dispatched',?,?,?) "
                "ON CONFLICT DO NOTHING",
                (receipt_id, task_id, turn_id, call_id, name, digest, worker_id, now, now),
            ).rowcount
            == 1
        )

    @staticmethod
    def complete(
        conn: Any, turn_id: str, call_id: str, worker_id: str, result_json: str, now: int
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_execution_receipts SET state='completed',result_json=?,"
                "updated_at=? WHERE turn_id=? AND tool_call_id=? AND worker_id=? "
                "AND state='dispatched'",
                (result_json, now, turn_id, call_id, worker_id),
            ).rowcount
            == 1
        )

    @staticmethod
    def unknown(conn: Any, turn_id: str, call_id: str, worker_id: str, now: int) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_execution_receipts SET state='unknown',updated_at=? "
                "WHERE turn_id=? AND tool_call_id=? AND worker_id=? AND state='dispatched'",
                (now, turn_id, call_id, worker_id),
            ).rowcount
            == 1
        )

    @staticmethod
    def unfinished(conn: Any, turn_id: str) -> list[ProjectTaskExecutionReceiptRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_execution_receipts WHERE turn_id=? "
            "AND state IN ('dispatched','unknown') ORDER BY id",
            (turn_id,),
        ).fetchall()
        return [ProjectTaskExecutionReceiptRow.from_row(row) for row in rows]
