"""Durable conversation command receipts and fenced worker writes."""

from __future__ import annotations

import builtins
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.resident_inbox import ResidentInboxRepo


class ResidentRequestConflict(ValueError):
    """A command identity was reused for different input."""


class ResidentRequestArchived(ValueError):
    """The professional profile no longer admits new work."""


class ResidentRequestBusy(ValueError):
    """The thread already owns an active command."""


@dataclass(frozen=True)
class ResidentRequestRow:
    request_id: str
    turn_id: str
    owner_id: int
    thread_id: str
    prompt: str
    configuration_json: str
    reply: str
    status: str
    error_code: str
    revision: int
    cancel_requested: bool
    worker_id: str
    lease_until: int
    created_at: int
    updated_at: int
    started_at: int | None
    finished_at: int | None
    pk: int = 0
    day_key: str = ""
    timezone: str = ""
    input_json: str = "{}"

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentRequestRow:
        return cls(
            request_id=str(row["request_id"]),
            turn_id=str(row["turn_id"]),
            owner_id=strict_int(row["owner_id"]),
            thread_id=str(row["thread_id"]),
            prompt=str(row["prompt"]),
            configuration_json=str(row["configuration_json"]),
            reply=str(row["reply"]),
            status=str(row["status"]),
            error_code=str(row["error_code"]),
            revision=strict_int(row["revision"]),
            cancel_requested=bool(row["cancel_requested"]),
            worker_id=str(row["worker_id"]),
            lease_until=strict_int(row["lease_until"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            started_at=strict_int(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=strict_int(row["finished_at"]) if row["finished_at"] is not None else None,
            pk=strict_int(row["id"]),
            day_key=str(row["day_key"]),
            timezone=str(row["timezone"]),
            input_json=str(row["input_json"]),
        )


class ResidentRequestRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def _bump_turn(conn: Any, turn_id: str, now: int) -> None:
        ResidentInboxRepo.record_turn(conn, turn_id, now)
        conn.execute(
            "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE thread_id="
            "(SELECT thread_id FROM resident_requests WHERE turn_id=?)",
            (now, turn_id),
        )

    def snapshot(
        self, owner_id: int, thread_id: str, limit: int = 51
    ) -> tuple[int, builtins.list[ResidentRequestRow]]:
        with self._db.read_snapshot() as conn:
            binding = conn.execute(
                "SELECT revision FROM resident_conversations WHERE owner_id=? AND thread_id=?",
                (owner_id, thread_id),
            ).fetchone()
            if binding is None:
                raise ResidentRequestConflict("resident binding unavailable")
            rows = conn.execute(
                "SELECT * FROM resident_requests WHERE owner_id=? AND thread_id=? ORDER BY id DESC LIMIT ?",
                (owner_id, thread_id, limit),
            ).fetchall()
            return strict_int(binding["revision"]), [
                ResidentRequestRow.from_row(row) for row in reversed(rows)
            ]

    @staticmethod
    def get_in_transaction(conn: Any, owner_id: int, request_id: str) -> ResidentRequestRow | None:
        row = conn.execute(
            "SELECT * FROM resident_requests WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return ResidentRequestRow.from_row(row) if row else None

    def get(self, owner_id: int, request_id: str) -> ResidentRequestRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, owner_id, request_id)

    def get_by_turn(self, turn_id: str, owner_id: int) -> ResidentRequestRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resident_requests WHERE turn_id=? AND owner_id=?",
                (turn_id, owner_id),
            ).fetchone()
        return ResidentRequestRow.from_row(row) if row else None

    def list(
        self, thread_id: str, owner_id: int, limit: int = 50, before: int | None = None
    ) -> builtins.list[ResidentRequestRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resident_requests WHERE thread_id=? AND owner_id=? "
                + ("AND id<? " if before is not None else "")
                + "ORDER BY id DESC LIMIT ?",
                (thread_id, owner_id, before, limit)
                if before is not None
                else (thread_id, owner_id, limit),
            ).fetchall()
        return [ResidentRequestRow.from_row(row) for row in reversed(rows)]

    def create(
        self,
        owner_id: int,
        thread_id: str,
        request_id: str,
        turn_id: str,
        prompt: str,
        now: int,
        configuration_json: str = "{}",
        day_key: str = "",
        timezone: str = "",
        input_json: str = "{}",
        *,
        connection: Any | None = None,
    ) -> tuple[ResidentRequestRow, bool]:
        with self._db.transaction() if connection is None else nullcontext(connection) as conn:
            conn.execute("UPDATE users SET id=id WHERE id=?", (owner_id,))
            prior = conn.execute(
                "SELECT * FROM resident_requests WHERE owner_id=? AND request_id=?",
                (owner_id, request_id),
            ).fetchone()
            if prior is not None:
                existing = ResidentRequestRow.from_row(prior)
                if (
                    existing.thread_id != thread_id
                    or existing.prompt != prompt
                    or existing.input_json != input_json
                ):
                    raise ResidentRequestConflict("request input changed")
                return existing, False
            archived = conn.execute(
                "SELECT 1 FROM resident_profiles p JOIN resident_conversations b ON b.agent_id=p.agent_id "
                "WHERE b.owner_id=? AND b.thread_id=? AND p.archived=1",
                (owner_id, thread_id),
            ).fetchone()
            if archived:
                raise ResidentRequestArchived("professional profile archived")
            binding = conn.execute(
                "SELECT 1 FROM resident_conversations WHERE thread_id=? AND owner_id=? AND status='ready'",
                (thread_id, owner_id),
            ).fetchone()
            if binding is None:
                raise ResidentRequestConflict("resident binding unavailable")
            inserted = conn.execute(
                "INSERT INTO resident_requests(owner_id,thread_id,request_id,turn_id,prompt,configuration_json,created_at,updated_at,day_key,timezone,input_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    owner_id,
                    thread_id,
                    request_id,
                    turn_id,
                    prompt,
                    configuration_json,
                    now,
                    now,
                    day_key,
                    timezone,
                    input_json,
                ),
            ).rowcount
            row = conn.execute(
                "SELECT * FROM resident_requests WHERE owner_id=? AND request_id=?",
                (owner_id, request_id),
            ).fetchone()
            if row is None:
                raise ResidentRequestBusy("resident thread has an active request")
            existing = ResidentRequestRow.from_row(row)
            if (
                existing.thread_id != thread_id
                or existing.prompt != prompt
                or existing.input_json != input_json
            ):
                raise ResidentRequestConflict("request input changed")
            if inserted:
                self._bump_turn(conn, turn_id, now)
            return existing, bool(inserted)

    def pending(self, now: int, limit: int = 100) -> builtins.list[ResidentRequestRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resident_requests WHERE status='queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [ResidentRequestRow.from_row(row) for row in rows]

    def mark_running(self, turn_id: str, worker_id: str, now: int, lease_until: int) -> bool:
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE resident_requests SET status='running',worker_id=?,lease_until=?,started_at=?,"
                "updated_at=?,revision=revision+1 WHERE turn_id=? AND status='queued' AND cancel_requested=0",
                (worker_id, lease_until, now, now, turn_id),
            ).rowcount
            if changed:
                self._bump_turn(conn, turn_id, now)
            return bool(changed)

    def renew(self, turn_id: str, worker_id: str, now: int, lease_until: int) -> bool:
        with self._db.transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE resident_requests SET lease_until=? WHERE turn_id=? AND worker_id=? "
                    "AND status='running' AND lease_until>?",
                    (lease_until, turn_id, worker_id, now),
                ).rowcount
            )

    def update_reply(self, turn_id: str, worker_id: str, reply: str, now: int) -> bool:
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE resident_requests SET reply=?,updated_at=?,revision=revision+1 "
                "WHERE turn_id=? AND worker_id=? AND status='running' AND lease_until>?",
                (reply, now, turn_id, worker_id, now),
            ).rowcount
            if changed:
                self._bump_turn(conn, turn_id, now)
            return bool(changed)

    def finish(
        self, turn_id: str, worker_id: str, status: str, reply: str, error_code: str, now: int
    ) -> bool:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("terminal status required")
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE resident_requests SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,"
                "reply=?,error_code=CASE WHEN cancel_requested=1 THEN '' ELSE ? END,"
                "updated_at=?,finished_at=?,worker_id='',lease_until=0,revision=revision+1 "
                "WHERE turn_id=? AND worker_id=? AND status='running' AND lease_until>?",
                (status, reply, error_code, now, now, turn_id, worker_id, now),
            ).rowcount
            if changed:
                self._bump_turn(conn, turn_id, now)
            return bool(changed)

    def cancel(
        self, owner_id: int, thread_id: str, cancel_request_id: str, target_turn_id: str, now: int
    ) -> ResidentRequestRow | None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET id=id WHERE id=?", (owner_id,))
            previous = conn.execute(
                "SELECT thread_id,target_turn_id FROM resident_cancel_requests WHERE owner_id=? AND request_id=?",
                (owner_id, cancel_request_id),
            ).fetchone()
            if previous is not None and (
                previous["thread_id"] != thread_id or previous["target_turn_id"] != target_turn_id
            ):
                raise ResidentRequestConflict("cancel target changed")
            target = conn.execute(
                "SELECT * FROM resident_requests WHERE owner_id=? AND thread_id=? AND turn_id=?",
                (owner_id, thread_id, target_turn_id),
            ).fetchone()
            if target is None:
                return None
            inserted = conn.execute(
                "INSERT INTO resident_cancel_requests(owner_id,thread_id,request_id,target_turn_id,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(owner_id,request_id) DO NOTHING",
                (owner_id, thread_id, cancel_request_id, target_turn_id, now),
            ).rowcount
            receipt = conn.execute(
                "SELECT thread_id,target_turn_id FROM resident_cancel_requests WHERE owner_id=? AND request_id=?",
                (owner_id, cancel_request_id),
            ).fetchone()
            if receipt["thread_id"] != thread_id or receipt["target_turn_id"] != target_turn_id:
                raise ResidentRequestConflict("cancel target changed")
            if inserted:
                changed = conn.execute(
                    "UPDATE resident_requests SET cancel_requested=1,"
                    "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,"
                    "finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END,"
                    "updated_at=?,revision=revision+1 WHERE turn_id=? "
                    "AND status IN ('queued','running') AND cancel_requested=0",
                    (now, now, target_turn_id),
                ).rowcount
                if changed:
                    self._bump_turn(conn, target_turn_id, now)
            target = conn.execute(
                "SELECT * FROM resident_requests WHERE turn_id=?", (target_turn_id,)
            ).fetchone()
        return ResidentRequestRow.from_row(target)

    def interrupt_active(self, now: int) -> int:
        with self._db.transaction() as conn:
            expired = conn.execute(
                "UPDATE resident_requests SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'interrupted' END,"
                "error_code=CASE WHEN cancel_requested=1 THEN '' ELSE 'RESIDENT_INTERRUPTED' END,"
                "worker_id='',lease_until=0,finished_at=?,updated_at=?,revision=revision+1 "
                "WHERE status='running' AND lease_until<=? RETURNING turn_id",
                (now, now, now),
            ).fetchall()
            for row in expired:
                self._bump_turn(conn, str(row["turn_id"]), now)
            return len(expired)

    @staticmethod
    def active_in_transaction(conn: Any, owner_id: int, thread_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM resident_requests WHERE owner_id=? AND thread_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (owner_id, thread_id),
            ).fetchone()
            is not None
        )
