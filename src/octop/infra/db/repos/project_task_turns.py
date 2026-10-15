"""SQL journal for task inputs, streamed replies and worker leases."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.project_tasks import ProjectTaskRepo


@dataclass(frozen=True)
class ProjectTaskTurnRow:
    turn_id: str
    task_id: str
    request_id: str
    prompt: str
    reply: str
    status: str
    error_code: str
    activity: str
    configuration_json: str
    model_ref: str
    worker_id: str
    cancel_requested: bool
    created_at: int
    updated_at: int
    input_tokens: int = 0
    output_tokens: int = 0
    pk: int = 0

    @classmethod
    def from_row(cls, r: DbRow) -> ProjectTaskTurnRow:
        return cls(
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            request_id=str(r["request_id"]),
            prompt=str(r["prompt"]),
            reply=str(r["reply"]),
            status=str(r["status"]),
            error_code=str(r["error_code"]),
            activity=str(r["activity"]),
            configuration_json=str(r["configuration_json"]),
            model_ref=str(r["model_ref"]),
            worker_id=str(r["worker_id"]),
            cancel_requested=bool(r["cancel_requested"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
            input_tokens=strict_int(r["input_tokens"]),
            output_tokens=strict_int(r["output_tokens"]),
            pk=strict_int(r["id"]),
        )


class ProjectTaskTurnRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list(self, task_id: str) -> builtins.list[ProjectTaskTurnRow]:
        with self._db.connect() as conn:
            return self.list_in_transaction(conn, task_id)

    @staticmethod
    def list_in_transaction(conn: Any, task_id: str) -> builtins.list[ProjectTaskTurnRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in rows]

    @staticmethod
    def page(
        conn: Any, task_id: str, limit: int, before: int | None = None
    ) -> builtins.list[ProjectTaskTurnRow]:
        position = " AND id<?" if before is not None else ""
        params = (task_id, before, limit) if before is not None else (task_id, limit)
        rows = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=?"
            + position
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in reversed(rows)]

    @staticmethod
    def since(
        conn: Any, task_id: str, position: int, limit: int
    ) -> builtins.list[ProjectTaskTurnRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=? AND id>=? ORDER BY id LIMIT ?",
            (task_id, position, limit),
        ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in rows]

    def get(self, turn_id: str) -> ProjectTaskTurnRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, turn_id)

    @staticmethod
    def get_in_transaction(conn: Any, turn_id: str) -> ProjectTaskTurnRow | None:
        r = conn.execute("SELECT * FROM project_task_turns WHERE turn_id=?", (turn_id,)).fetchone()
        return ProjectTaskTurnRow.from_row(r) if r else None

    @staticmethod
    def by_request(conn: Any, task_id: str, request_id: str) -> ProjectTaskTurnRow | None:
        r = conn.execute(
            "SELECT * FROM project_task_turns WHERE task_id=? AND request_id=?",
            (task_id, request_id),
        ).fetchone()
        return ProjectTaskTurnRow.from_row(r) if r else None

    @staticmethod
    def insert(conn: Any, row: ProjectTaskTurnRow) -> None:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id, task_id, request_id, prompt, status, "
            "configuration_json, model_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                row.turn_id,
                row.task_id,
                row.request_id,
                row.prompt,
                row.configuration_json,
                row.model_ref,
                row.created_at,
                row.updated_at,
            ),
        )

    def claim(self, worker_id: str, now: int) -> ProjectTaskTurnRow | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT turn_id FROM project_task_turns WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            r = conn.execute(
                "UPDATE project_task_turns SET status='running', worker_id=?, updated_at=? "
                "WHERE turn_id=? AND status='queued' RETURNING *",
                (worker_id, now, row["turn_id"]),
            ).fetchone()
            if r is None:
                return None
            turn = ProjectTaskTurnRow.from_row(r)
            ProjectTaskRepo.start(conn, turn.task_id, turn.turn_id, now)
            return turn

    def progress(self, turn_id: str, worker_id: str, reply: str, activity: str, now: int) -> bool:
        with self._db.connect() as conn:
            r = conn.execute(
                "UPDATE project_task_turns SET reply=?, activity=?, updated_at=? "
                "WHERE turn_id=? AND worker_id=? AND status='running' RETURNING cancel_requested",
                (reply, activity, now, turn_id, worker_id),
            ).fetchone()
        return r is None or bool(r["cancel_requested"])

    @staticmethod
    def finish(
        conn: Any, turn_id: str, worker_id: str, status: str, reply: str, error_code: str, now: int
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET status=CASE WHEN cancel_requested=1 "
                "THEN 'cancelled' ELSE ? END, reply=?, error_code=?, activity='', updated_at=? "
                "WHERE turn_id=? AND worker_id=? AND status='running'",
                (status, reply, error_code, now, turn_id, worker_id),
            ).rowcount
            == 1
        )

    @staticmethod
    def cancel(conn: Any, turn_id: str, now: int) -> None:
        conn.execute(
            "UPDATE project_task_turns SET cancel_requested=1, updated_at=?, "
            "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END "
            "WHERE turn_id=? AND status IN ('queued', 'running')",
            (now, turn_id),
        )

    def stale(self, before: int) -> builtins.list[ProjectTaskTurnRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_task_turns WHERE status='running' AND updated_at<?",
                (before,),
            ).fetchall()
        return [ProjectTaskTurnRow.from_row(r) for r in rows]

    @staticmethod
    def interrupt(conn: Any, turn_id: str, before: int, now: int) -> bool:
        return bool(
            conn.execute(
                "UPDATE project_task_turns SET status='failed', error_code='TASK_INTERRUPTED', "
                "activity='', updated_at=? WHERE turn_id=? AND status='running' AND updated_at<?",
                (now, turn_id, before),
            ).rowcount
            == 1
        )

    def record_usage(
        self, turn_id: str, worker_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE project_task_turns SET input_tokens=?, output_tokens=? "
                "WHERE turn_id=? AND worker_id=? AND status='running'",
                (input_tokens, output_tokens, turn_id, worker_id),
            )
