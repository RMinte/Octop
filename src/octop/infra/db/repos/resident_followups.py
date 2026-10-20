"""SQL-only storage for explicit conversation notification authorizations."""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ResidentFollowupRow:
    followup_id: str
    owner_id: int
    agent_id: str
    thread_id: str
    request_id: str
    fingerprint: str
    purpose: str
    kind: str
    schedule: str
    timezone: str
    subject_task_id: str
    payload_json: str
    capabilities_json: str
    notification_target: str
    authorization_version: int
    revision: int
    status: str
    next_fire_at: int | None
    last_observed_json: str
    created_at: int
    updated_at: int
    pk: int = 0

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentFollowupRow:
        return cls(
            followup_id=str(row["followup_id"]),
            owner_id=strict_int(row["owner_id"]),
            agent_id=str(row["agent_id"]),
            thread_id=str(row["thread_id"]),
            request_id=str(row["request_id"]),
            fingerprint=str(row["fingerprint"]),
            purpose=str(row["purpose"]),
            kind=str(row["kind"]),
            schedule=str(row["schedule"]),
            timezone=str(row["timezone"]),
            subject_task_id=str(row["subject_task_id"]),
            payload_json=str(row["payload_json"]),
            capabilities_json=str(row["capabilities_json"]),
            notification_target=str(row["notification_target"]),
            authorization_version=strict_int(row["authorization_version"]),
            revision=strict_int(row["revision"]),
            status=str(row["status"]),
            next_fire_at=(
                strict_int(row["next_fire_at"]) if row["next_fire_at"] is not None else None
            ),
            last_observed_json=str(row["last_observed_json"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            pk=strict_int(row["id"]),
        )


class ResidentFollowupRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    @contextmanager
    def task_admission(conn: Any) -> Iterator[None]:
        """Roll back a rejected task admission without losing its failure notification."""
        conn.execute("SAVEPOINT resident_followup_admission")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT resident_followup_admission")
            conn.execute("RELEASE SAVEPOINT resident_followup_admission")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT resident_followup_admission")

    @staticmethod
    def source_exists(conn: Any, owner_id: int, thread_id: str, turn_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM resident_requests WHERE owner_id=? AND thread_id=? AND turn_id=?",
                (owner_id, thread_id, turn_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def bump_conversation(conn: Any, owner_id: int, thread_id: str, now: int) -> None:
        conn.execute(
            "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE owner_id=? AND thread_id=?",
            (now, owner_id, thread_id),
        )

    @staticmethod
    def cancel_for_agent_in_transaction(conn: Any, owner_id: int, agent_id: str, now: int) -> int:
        return int(
            conn.execute(
                "UPDATE resident_followups SET status='cancelled',next_fire_at=NULL,"
                "authorization_version=authorization_version+1,revision=revision+1,updated_at=? "
                "WHERE owner_id=? AND agent_id=? AND status<>'cancelled'",
                (now, owner_id, agent_id),
            ).rowcount
        )

    @staticmethod
    def get_in_transaction(
        conn: Any, owner_id: int, followup_id: str
    ) -> ResidentFollowupRow | None:
        row = conn.execute(
            "SELECT * FROM resident_followups WHERE owner_id=? AND followup_id=?",
            (owner_id, followup_id),
        ).fetchone()
        return ResidentFollowupRow.from_row(row) if row else None

    def get(self, owner_id: int, followup_id: str) -> ResidentFollowupRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, owner_id, followup_id)

    @staticmethod
    def by_request(conn: Any, owner_id: int, request_id: str) -> ResidentFollowupRow | None:
        row = conn.execute(
            "SELECT * FROM resident_followups WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return ResidentFollowupRow.from_row(row) if row else None

    @staticmethod
    def insert(conn: Any, row: ResidentFollowupRow) -> None:
        conn.execute(
            "INSERT INTO resident_followups(followup_id,owner_id,agent_id,thread_id,request_id,"
            "fingerprint,purpose,kind,schedule,timezone,subject_task_id,payload_json,"
            "capabilities_json,notification_target,authorization_version,revision,status,"
            "next_fire_at,last_observed_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.followup_id,
                row.owner_id,
                row.agent_id,
                row.thread_id,
                row.request_id,
                row.fingerprint,
                row.purpose,
                row.kind,
                row.schedule,
                row.timezone,
                row.subject_task_id,
                row.payload_json,
                row.capabilities_json,
                row.notification_target,
                row.authorization_version,
                row.revision,
                row.status,
                row.next_fire_at,
                row.last_observed_json,
                row.created_at,
                row.updated_at,
            ),
        )

    @staticmethod
    def page(
        conn: Any, owner_id: int, thread_id: str, limit: int, before: int | None = None
    ) -> builtins.list[ResidentFollowupRow]:
        where = " AND id<?" if before is not None else ""
        params: builtins.list[Any] = [owner_id, thread_id]
        if before is not None:
            params.append(before)
        params.append(limit)
        rows = conn.execute(
            "SELECT * FROM resident_followups WHERE owner_id=? AND thread_id=?"
            + where
            + " ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [ResidentFollowupRow.from_row(row) for row in rows]

    def due(self, now: int, limit: int = 100) -> builtins.list[ResidentFollowupRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT f.* FROM resident_followups f JOIN users u ON u.id=f.owner_id "
                "WHERE f.status='enabled' AND f.next_fire_at<=? AND u.disabled=0 "
                "ORDER BY f.next_fire_at,f.id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [ResidentFollowupRow.from_row(row) for row in rows]

    @staticmethod
    def lock(conn: Any, owner_id: int, followup_id: str) -> ResidentFollowupRow | None:
        conn.execute(
            "UPDATE resident_followups SET updated_at=updated_at WHERE owner_id=? AND followup_id=?",
            (owner_id, followup_id),
        )
        return ResidentFollowupRepo.get_in_transaction(conn, owner_id, followup_id)

    @staticmethod
    def replace(conn: Any, row: ResidentFollowupRow, expected_revision: int) -> bool:
        return bool(
            conn.execute(
                "UPDATE resident_followups SET purpose=?,kind=?,schedule=?,timezone=?,subject_task_id=?,"
                "payload_json=?,capabilities_json=?,authorization_version=?,revision=?,status=?,"
                "next_fire_at=?,last_observed_json=?,updated_at=? WHERE followup_id=? AND owner_id=? "
                "AND revision=?",
                (
                    row.purpose,
                    row.kind,
                    row.schedule,
                    row.timezone,
                    row.subject_task_id,
                    row.payload_json,
                    row.capabilities_json,
                    row.authorization_version,
                    row.revision,
                    row.status,
                    row.next_fire_at,
                    row.last_observed_json,
                    row.updated_at,
                    row.followup_id,
                    row.owner_id,
                    expected_revision,
                ),
            ).rowcount
        )

    @staticmethod
    def record_poll(
        conn: Any,
        row: ResidentFollowupRow,
        *,
        next_fire_at: int | None,
        last_observed_json: str,
        status: str,
        now: int,
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE resident_followups SET next_fire_at=?,last_observed_json=?,status=?,"
                "revision=revision+?,updated_at=? WHERE followup_id=? AND authorization_version=? "
                "AND status='enabled'",
                (
                    next_fire_at,
                    last_observed_json,
                    status,
                    int(status != row.status),
                    now,
                    row.followup_id,
                    row.authorization_version,
                ),
            ).rowcount
        )
