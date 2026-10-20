"""Authorized proactive messages and their replay-safe delivery receipts."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.resident_inbox import ResidentInboxRepo


@dataclass(frozen=True)
class ResidentNotificationRow:
    notification_id: str
    owner_id: int
    agent_id: str
    thread_id: str
    followup_id: str
    authorization_version: int
    kind: str
    text: str
    payload_json: str
    message_id: str
    day_key: str
    timezone: str
    created_at: int
    updated_at: int
    status: str
    archive_written_at: int | None
    context_written_at: int | None
    message_json: str
    delivery_error: str

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentNotificationRow:
        return cls(
            notification_id=str(row["notification_id"]),
            owner_id=strict_int(row["owner_id"]),
            agent_id=str(row["agent_id"]),
            thread_id=str(row["thread_id"]),
            followup_id=str(row["followup_id"]),
            authorization_version=strict_int(row["authorization_version"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            payload_json=str(row["payload_json"]),
            message_id=str(row["message_id"]),
            day_key=str(row["day_key"]),
            timezone=str(row["timezone"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            status=str(row["status"]),
            archive_written_at=strict_int(row["archive_written_at"])
            if row["archive_written_at"] is not None
            else None,
            context_written_at=strict_int(row["context_written_at"])
            if row["context_written_at"] is not None
            else None,
            message_json=str(row["message_json"]),
            delivery_error=str(row["delivery_error"]),
        )


class ResidentNotificationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def enqueue(
        conn: Any,
        *,
        notification_id: str,
        owner_id: int,
        agent_id: str,
        thread_id: str,
        followup_id: str,
        authorization_version: int,
        source_key: str,
        kind: str,
        text: str,
        payload_json: str,
        day_key: str,
        timezone: str,
        now: int,
    ) -> bool:
        inserted = conn.execute(
            "INSERT INTO resident_notifications(notification_id,owner_id,agent_id,thread_id,followup_id,"
            "authorization_version,source_key,kind,text,payload_json,message_id,day_key,timezone,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
            (
                notification_id,
                owner_id,
                agent_id,
                thread_id,
                followup_id,
                authorization_version,
                source_key,
                kind,
                text,
                payload_json,
                f"resident:notification:{notification_id}",
                day_key,
                timezone,
                now,
                now,
            ),
        ).rowcount
        return bool(inserted)

    def get(self, owner: int, notification_id: str) -> ResidentNotificationRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resident_notifications WHERE owner_id=? AND notification_id=?",
                (owner, notification_id),
            ).fetchone()
        return ResidentNotificationRow.from_row(row) if row else None

    @staticmethod
    def page(
        conn: Any,
        owner: int,
        thread: str,
        limit: int,
        boundary: tuple[int, str] | None = None,
    ) -> builtins.list[ResidentNotificationRow]:
        position = " AND (n.created_at,n.notification_id)<(?,?)" if boundary else ""
        params: builtins.list[Any] = [owner, thread]
        if boundary:
            params.extend(boundary)
        params.append(limit)
        rows = conn.execute(
            "SELECT n.* FROM resident_notifications n WHERE n.owner_id=? AND n.thread_id=? AND "
            "(n.status='delivered' OR (n.status='pending' AND EXISTS("
            "SELECT 1 FROM resident_followups f JOIN agents a ON a.agent_id=f.agent_id "
            "JOIN users u ON u.id=f.owner_id "
            "LEFT JOIN resident_profiles p ON p.agent_id=f.agent_id WHERE f.followup_id=n.followup_id "
            "AND f.owner_id=n.owner_id AND f.agent_id=n.agent_id AND f.thread_id=n.thread_id "
            "AND f.authorization_version=n.authorization_version AND f.status IN ('enabled','completed') "
            "AND a.enabled=1 AND a.is_shared=0 AND a.user_id=f.owner_id AND u.disabled=0 AND COALESCE(p.archived,0)=0)))"
            + position
            + " ORDER BY n.created_at DESC,n.notification_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [ResidentNotificationRow.from_row(row) for row in rows]

    def pending(
        self, limit: int = 100, *, owner: int | None = None, thread: str | None = None
    ) -> builtins.list[ResidentNotificationRow]:
        scope = " AND owner_id=? AND thread_id=?" if owner is not None else ""
        params = (owner, thread, limit) if owner is not None else (limit,)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resident_notifications WHERE status='pending'"
                + scope
                + " ORDER BY id LIMIT ?",
                params,
            ).fetchall()
        return [ResidentNotificationRow.from_row(row) for row in rows]

    def is_authorized(self, row: ResidentNotificationRow) -> bool:
        with self._db.connect() as conn:
            result = conn.execute(
                "SELECT 1 FROM resident_followups f JOIN resident_conversations b ON b.thread_id=f.thread_id "
                "JOIN agents a ON a.agent_id=f.agent_id JOIN users u ON u.id=f.owner_id "
                "LEFT JOIN resident_profiles p ON p.agent_id=f.agent_id "
                "WHERE f.followup_id=? AND f.owner_id=? AND f.agent_id=? AND f.thread_id=? "
                "AND f.authorization_version=? AND f.status IN ('enabled','completed') "
                "AND b.status='ready' AND b.owner_id=f.owner_id AND b.agent_id=f.agent_id "
                "AND a.user_id=f.owner_id AND a.enabled=1 AND a.is_shared=0 AND u.disabled=0 "
                "AND COALESCE(p.archived,0)=0",
                (
                    row.followup_id,
                    row.owner_id,
                    row.agent_id,
                    row.thread_id,
                    row.authorization_version,
                ),
            ).fetchone()
        return result is not None

    def prepare_message(self, notification_id: str, wire: str) -> str:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE resident_notifications SET message_json=? WHERE notification_id=? AND message_json=''",
                (wire, notification_id),
            )
            row = conn.execute(
                "SELECT message_json FROM resident_notifications WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown notification")
            return str(row["message_json"])

    def mark_stage(self, notification_id: str, stage: str, now: int) -> None:
        if stage not in {"archive_written_at", "context_written_at"}:
            raise ValueError("Invalid notification stage")
        with self._db.connect() as conn:
            conn.execute(
                f"UPDATE resident_notifications SET {stage}=?,updated_at=?,delivery_error='' WHERE notification_id=? AND {stage} IS NULL",
                (now, now, notification_id),
            )

    def mark_delivered(self, notification_id: str, now: int) -> None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE resident_notifications SET status='delivered',updated_at=?,delivery_error='' "
                "WHERE notification_id=? AND status='pending' AND archive_written_at IS NOT NULL "
                "AND context_written_at IS NOT NULL RETURNING *",
                (now, notification_id),
            ).fetchone()
            if row:
                ResidentInboxRepo.record(
                    conn,
                    strict_int(row["owner_id"]),
                    str(row["thread_id"]),
                    "notification",
                    notification_id,
                    str(row["message_id"]),
                    now,
                )

    def discard(self, notification_id: str, now: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE resident_notifications SET status='discarded',updated_at=? WHERE notification_id=? AND status='pending'",
                (now, notification_id),
            )

    def record_error(self, notification_id: str, error_code: str, now: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE resident_notifications SET delivery_error=?,updated_at=? WHERE notification_id=? AND status='pending'",
                (error_code, now, notification_id),
            )
