"""SQL access to AgentComputer tasks; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class AcTaskRow:
    pk: int
    task_id: str
    project_id: str
    owner_id: int
    session_id: str
    octop_thread_id: str | None
    preparation_state: str
    revision: int
    schema_version: int
    next_sequence: int
    active_run_id: str | None
    title: str
    created_at: int
    updated_at: int

    @property
    def id(self) -> str:
        return self.task_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcTaskRow:
        return cls(
            pk=strict_int(r["id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]),
            owner_id=strict_int(r["owner_id"]),
            session_id=str(r["session_id"]),
            octop_thread_id=str(r["octop_thread_id"]) if r["octop_thread_id"] is not None else None,
            preparation_state=str(r["preparation_state"]),
            revision=strict_int(r["revision"]),
            schema_version=strict_int(r["schema_version"]),
            next_sequence=strict_int(r["next_sequence"]),
            active_run_id=str(r["active_run_id"]) if r["active_run_id"] is not None else None,
            title=str(r["title"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
        )


class AcTaskRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, task_id: str, owner_id: int) -> AcTaskRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, task_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, task_id: str, owner_id: int) -> AcTaskRow | None:
        row = conn.execute(
            "SELECT * FROM ac_tasks WHERE task_id = ? AND owner_id = ?",
            (task_id, owner_id),
        ).fetchone()
        return AcTaskRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(
        conn: Any,
        *,
        task_id: str,
        project_id: str,
        owner_id: int,
        session_id: str,
        octop_thread_id: str | None,
        title: str,
        created_at: int,
        updated_at: int,
        preparation_state: str = "ready",
    ) -> AcTaskRow | None:
        row = conn.execute(
            "INSERT INTO ac_tasks (task_id, project_id, owner_id, session_id, "
            "octop_thread_id, title, preparation_state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING *",
            (
                task_id,
                project_id,
                owner_id,
                session_id,
                octop_thread_id,
                title,
                preparation_state,
                created_at,
                updated_at,
            ),
        ).fetchone()
        return AcTaskRow.from_row(row) if row is not None else None

    @staticmethod
    def bind_thread(
        conn: Any, task_id: str, owner_id: int, thread_id: str, *, updated_at: int
    ) -> bool:
        cursor = conn.execute(
            "UPDATE ac_tasks SET octop_thread_id = ?, preparation_state = 'ready', "
            "updated_at = ? WHERE task_id = ? AND owner_id = ? "
            "AND preparation_state = 'pending' AND octop_thread_id IS NULL",
            (thread_id, updated_at, task_id, owner_id),
        )
        return bool(cursor.rowcount == 1)

    @staticmethod
    def acquire_run(
        conn: Any, task_id: str, owner_id: int, run_id: str, *, updated_at: int
    ) -> bool:
        cursor = conn.execute(
            "UPDATE ac_tasks SET active_run_id = ?, revision = revision + 1, "
            "next_sequence = next_sequence + 1, updated_at = ? "
            "WHERE task_id = ? AND owner_id = ? "
            "AND preparation_state = 'ready' AND active_run_id IS NULL",
            (run_id, updated_at, task_id, owner_id),
        )
        return bool(cursor.rowcount == 1)

    @staticmethod
    def release_run(
        conn: Any, task_id: str, owner_id: int, run_id: str, *, updated_at: int
    ) -> bool:
        cursor = conn.execute(
            "UPDATE ac_tasks SET active_run_id = NULL, revision = revision + 1, "
            "updated_at = ? WHERE task_id = ? AND owner_id = ? AND active_run_id = ?",
            (updated_at, task_id, owner_id, run_id),
        )
        return bool(cursor.rowcount == 1)

    @staticmethod
    def allocate_event_sequence(
        conn: Any,
        task_id: str,
        owner_id: int,
        run_id: str,
        *,
        expected_revision: int,
        updated_at: int,
    ) -> tuple[int, int] | None:
        row = conn.execute(
            "UPDATE ac_tasks SET next_sequence = next_sequence + 1, revision = revision + 1, "
            "updated_at = ? WHERE task_id = ? AND owner_id = ? AND active_run_id = ? "
            "AND revision = ? RETURNING next_sequence - 1 AS sequence, revision",
            (updated_at, task_id, owner_id, run_id, expected_revision),
        ).fetchone()
        return (
            (strict_int(row["sequence"]), strict_int(row["revision"])) if row is not None else None
        )
