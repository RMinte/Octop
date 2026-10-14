"""SQL access to AgentComputer events; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, strict_int


@dataclass(frozen=True)
class AcEventRow:
    pk: int
    event_id: str
    task_id: str
    owner_id: int
    sequence: int
    run_id: str
    turn_id: str
    event_type: str
    payload_version: int
    schema_version: int
    payload_json: str
    created_at: int

    @property
    def id(self) -> str:
        return self.event_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcEventRow:
        return cls(
            pk=strict_int(r["id"]),
            event_id=str(r["event_id"]),
            task_id=str(r["task_id"]),
            owner_id=strict_int(r["owner_id"]),
            sequence=strict_int(r["sequence"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            event_type=str(r["event_type"]),
            payload_version=strict_int(r["payload_version"]),
            schema_version=strict_int(r["schema_version"]),
            payload_json=str(r["payload_json"]),
            created_at=strict_int(r["created_at"]),
        )


class AcEventRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def get_by_sequence_in_transaction(
        conn: Any, task_id: str, owner_id: int, sequence: int
    ) -> AcEventRow | None:
        row = conn.execute(
            "SELECT e.* FROM ac_events e JOIN ac_tasks t "
            "ON t.task_id = e.task_id AND t.owner_id = e.owner_id "
            "WHERE e.task_id = ? AND e.owner_id = ? AND e.sequence = ?",
            (task_id, owner_id, sequence),
        ).fetchone()
        return AcEventRow.from_row(row) if row is not None else None

    @staticmethod
    def list_for_run_in_transaction(
        conn: Any, run_id: str, owner_id: int, *, event_type: str = "run.accepted"
    ) -> list[AcEventRow]:
        """Read every matching event for an owned Run; duplicate acceptance stays visible."""
        rows = conn.execute(
            "SELECT e.* FROM ac_events e JOIN ac_runs r "
            "ON r.run_id = e.run_id AND r.owner_id = e.owner_id "
            "WHERE r.run_id = ? AND r.owner_id = ? AND e.event_type = ? "
            "ORDER BY e.sequence",
            (run_id, owner_id, event_type),
        ).fetchall()
        return map_rows(rows, AcEventRow)

    @staticmethod
    def list_page_for_task_in_transaction(
        conn: Any,
        task_id: str,
        owner_id: int,
        *,
        after_sequence: int,
        through_sequence: int,
        limit: int,
    ) -> list[AcEventRow]:
        rows = conn.execute(
            "SELECT * FROM ac_events WHERE task_id=? AND owner_id=? "
            "AND sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
            (task_id, owner_id, after_sequence, through_sequence, limit),
        ).fetchall()
        return map_rows(rows, AcEventRow)

    @staticmethod
    def insert(
        conn: Any,
        *,
        event_id: str,
        task_id: str,
        owner_id: int,
        sequence: int,
        run_id: str,
        turn_id: str,
        event_type: str,
        payload_version: int = 1,
        schema_version: int = 1,
        payload_json: str,
        created_at: int,
    ) -> AcEventRow:
        row = conn.execute(
            "INSERT INTO ac_events (event_id, task_id, owner_id, sequence, run_id, turn_id, event_type, payload_version, schema_version, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (
                event_id,
                task_id,
                owner_id,
                sequence,
                run_id,
                turn_id,
                event_type,
                payload_version,
                schema_version,
                payload_json,
                created_at,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("AgentComputer events insert returned no row")
        return AcEventRow.from_row(row)
