"""Monotonic, owner-scoped read watermarks over durable visible messages."""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import strict_int


class ResidentInboxRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def record(
        conn: Any,
        owner_id: int,
        thread_id: str,
        source: str,
        source_id: str,
        message_id: str,
        now: int,
    ) -> None:
        # PostgreSQL sequence values can be allocated in a different order from
        # commits. Serialize this thread before allocating an event ID so a read
        # watermark cannot hide a lower-ID event that commits later.
        conn.execute(
            "UPDATE resident_conversations SET revision=revision WHERE owner_id=? AND thread_id=?",
            (owner_id, thread_id),
        )
        inserted = conn.execute(
            "INSERT INTO resident_inbox(event_id,owner_id,thread_id,source,source_id,message_id,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
            (f"{source}:{source_id}", owner_id, thread_id, source, source_id, message_id, now),
        ).rowcount
        if inserted:
            conn.execute(
                "UPDATE resident_conversations SET inbox_initialized=1,revision=revision+1 WHERE owner_id=? AND thread_id=?",
                (owner_id, thread_id),
            )

    @staticmethod
    def record_turn(conn: Any, turn_id: str, now: int) -> None:
        row = conn.execute(
            "SELECT owner_id,thread_id,reply,status FROM resident_requests WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        if row and (row["reply"] or row["status"] not in {"queued", "running"}):
            ResidentInboxRepo.record(
                conn,
                strict_int(row["owner_id"]),
                str(row["thread_id"]),
                "turn",
                turn_id,
                f"resident:{turn_id}:assistant",
                now,
            )

    def initialize_existing(self) -> None:
        """Existing history is the read baseline; never announce old messages as new."""
        with self._db.transaction() as conn:
            bindings = conn.execute(
                "SELECT owner_id,thread_id FROM resident_conversations WHERE inbox_initialized=0"
            ).fetchall()
            for binding in bindings:
                owner, thread = strict_int(binding["owner_id"]), str(binding["thread_id"])
                turns = conn.execute(
                    "SELECT turn_id,updated_at FROM resident_requests WHERE owner_id=? AND thread_id=? "
                    "AND (reply<>'' OR status NOT IN ('queued','running')) ORDER BY id",
                    (owner, thread),
                ).fetchall()
                for turn in turns:
                    self.record(
                        conn,
                        owner,
                        thread,
                        "turn",
                        str(turn["turn_id"]),
                        f"resident:{turn['turn_id']}:assistant",
                        strict_int(turn["updated_at"]),
                    )
                reports = conn.execute(
                    "SELECT report_id,message_id,updated_at FROM resident_task_reports WHERE owner_id=? "
                    "AND thread_id=? AND delivery_status='delivered' ORDER BY id",
                    (owner, thread),
                ).fetchall()
                for report in reports:
                    self.record(
                        conn,
                        owner,
                        thread,
                        "task_report",
                        str(report["report_id"]),
                        str(report["message_id"]),
                        strict_int(report["updated_at"]),
                    )
                conn.execute(
                    "UPDATE resident_conversations SET read_event_seq=(SELECT COALESCE(MAX(id),0) FROM resident_inbox "
                    "WHERE owner_id=? AND thread_id=?),inbox_initialized=1 WHERE owner_id=? AND thread_id=?",
                    (owner, thread, owner, thread),
                )

    @staticmethod
    def _state(conn: Any, owner: int, thread: str) -> dict[str, Any]:
        binding = conn.execute(
            "SELECT revision,read_event_seq FROM resident_conversations WHERE owner_id=? AND thread_id=?",
            (owner, thread),
        ).fetchone()
        if binding is None:
            raise ValueError("Unknown resident inbox")
        watermark = strict_int(binding["read_event_seq"])
        unread = conn.execute(
            "SELECT COUNT(*) FROM resident_inbox WHERE owner_id=? AND thread_id=? AND id>?",
            (owner, thread, watermark),
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT message_id FROM resident_inbox WHERE owner_id=? AND thread_id=? ORDER BY id DESC LIMIT 1",
            (owner, thread),
        ).fetchone()
        read = conn.execute(
            "SELECT message_id FROM resident_inbox WHERE owner_id=? AND thread_id=? AND id<=? ORDER BY id DESC LIMIT 1",
            (owner, thread, watermark),
        ).fetchone()
        return {
            "revision": strict_int(binding["revision"]),
            "unread_count": strict_int(unread),
            "last_message_id": str(latest["message_id"]) if latest else None,
            "last_read_message_id": str(read["message_id"]) if read else None,
        }

    def state(self, owner: int, thread: str) -> dict[str, Any]:
        with self._db.read_snapshot() as conn:
            return self._state(conn, owner, thread)

    def mark_read(self, owner: int, thread: str, message_id: str) -> dict[str, Any] | None:
        with self._db.transaction() as conn:
            anchor = conn.execute(
                "SELECT id FROM resident_inbox WHERE owner_id=? AND thread_id=? AND message_id=?",
                (owner, thread, message_id),
            ).fetchone()
            if anchor is None:
                return None
            conn.execute(
                "UPDATE resident_conversations SET read_event_seq=?,revision=revision+1 "
                "WHERE owner_id=? AND thread_id=? AND read_event_seq<?",
                (anchor["id"], owner, thread, anchor["id"]),
            )
            return self._state(conn, owner, thread)
