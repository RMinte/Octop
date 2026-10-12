"""SQL access to AgentComputer events; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow


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
            pk=int(r["id"]),
            event_id=str(r["event_id"]),
            task_id=str(r["task_id"]),
            owner_id=int(r["owner_id"]),
            sequence=int(r["sequence"]),
            run_id=str(r["run_id"]),
            turn_id=str(r["turn_id"]),
            event_type=str(r["event_type"]),
            payload_version=int(r["payload_version"]),
            schema_version=int(r["schema_version"]),
            payload_json=str(r["payload_json"]),
            created_at=int(r["created_at"]),
        )


class AcEventRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

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
