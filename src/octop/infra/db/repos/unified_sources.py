"""SQL receipts for source routing, durable input and outbound delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.utils.ulid import new_ulid


@dataclass(frozen=True)
class UnifiedSourceRoute:
    route_id: str
    owner_id: int
    source_kind: str
    source_instance: str
    source_key: str
    task_id: str | None
    pending_task_id: str | None
    legacy_thread_id: str | None
    legacy_agent_id: str | None
    state: str
    revision: int
    source_digest: str
    metadata_json: str

    @classmethod
    def from_row(cls, row: DbRow) -> UnifiedSourceRoute:
        return cls(
            **{
                key: str(row[key])
                for key in (
                    "route_id",
                    "source_kind",
                    "source_instance",
                    "source_key",
                    "state",
                    "source_digest",
                    "metadata_json",
                )
            },
            task_id=str(row["task_id"]) if row["task_id"] is not None else None,
            pending_task_id=str(row["pending_task_id"])
            if row["pending_task_id"] is not None
            else None,
            legacy_thread_id=str(row["legacy_thread_id"])
            if row["legacy_thread_id"] is not None
            else None,
            legacy_agent_id=str(row["legacy_agent_id"])
            if row["legacy_agent_id"] is not None
            else None,
            owner_id=strict_int(row["owner_id"]),
            revision=strict_int(row["revision"]),
        )


class UnifiedSourceRouteRepo:
    @staticmethod
    def get(conn: Any, owner_id: int, route_id: str) -> UnifiedSourceRoute | None:
        row = conn.execute(
            "SELECT * FROM unified_source_routes WHERE owner_id=? AND route_id=?",
            (owner_id, route_id),
        ).fetchone()
        return UnifiedSourceRoute.from_row(row) if row else None

    @staticmethod
    def by_identity(
        conn: Any, *, owner_id: int, source_kind: str, source_instance: str, source_key: str
    ) -> UnifiedSourceRoute | None:
        row = conn.execute(
            "SELECT * FROM unified_source_routes WHERE owner_id=? AND source_kind=? AND source_instance=? AND source_key=?",
            (owner_id, source_kind, source_instance, source_key),
        ).fetchone()
        return UnifiedSourceRoute.from_row(row) if row else None

    @classmethod
    def ensure(
        cls,
        conn: Any,
        *,
        owner_id: int,
        source_kind: str,
        source_instance: str,
        source_key: str,
        route_id: str | None = None,
        legacy_thread_id: str | None = None,
        legacy_agent_id: str | None = None,
        metadata_json: str = "{}",
        now: int,
    ) -> UnifiedSourceRoute:
        route_id = route_id or "source_" + new_ulid()
        conn.execute(
            "INSERT INTO unified_source_routes(route_id,owner_id,source_kind,source_instance,source_key,legacy_thread_id,legacy_agent_id,state,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,source_kind,source_instance,source_key) DO NOTHING",
            (
                route_id,
                owner_id,
                source_kind,
                source_instance,
                source_key,
                legacy_thread_id,
                legacy_agent_id,
                "legacy" if legacy_thread_id else "task",
                metadata_json,
                now,
                now,
            ),
        )
        row = cls.by_identity(
            conn,
            owner_id=owner_id,
            source_kind=source_kind,
            source_instance=source_instance,
            source_key=source_key,
        )
        assert row is not None
        return row

    @staticmethod
    def transition(
        conn: Any,
        owner_id: int,
        route_id: str,
        expected_revision: int,
        expected_state: str,
        state: str,
        *,
        task_id: str | None = None,
        pending_task_id: str | None = None,
        source_digest: str = "",
        now: int,
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE unified_source_routes SET state=?,task_id=?,pending_task_id=?,source_digest=?,revision=revision+1,updated_at=? WHERE route_id=? AND owner_id=? AND revision=? AND state=?",
                (
                    state,
                    task_id,
                    pending_task_id,
                    source_digest,
                    now,
                    route_id,
                    owner_id,
                    expected_revision,
                    expected_state,
                ),
            ).rowcount
        )


@dataclass(frozen=True)
class UnifiedInbox:
    inbox_id: str
    route_id: str
    owner_id: int
    event_id: str
    fingerprint: str
    payload_json: str
    status: str
    task_id: str | None
    turn_id: str | None
    error_code: str = ""

    @classmethod
    def from_row(cls, row: DbRow) -> UnifiedInbox:
        return cls(
            **{
                key: str(row[key])
                for key in (
                    "inbox_id",
                    "route_id",
                    "event_id",
                    "fingerprint",
                    "payload_json",
                    "status",
                    "error_code",
                )
            },
            owner_id=strict_int(row["owner_id"]),
            task_id=str(row["task_id"]) if row["task_id"] is not None else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
        )


class UnifiedSourceInboxRepo:
    @staticmethod
    def get(conn: Any, owner_id: int, inbox_id: str) -> UnifiedInbox | None:
        row = conn.execute(
            "SELECT * FROM unified_source_inbox WHERE inbox_id=? AND owner_id=?",
            (inbox_id, owner_id),
        ).fetchone()
        return UnifiedInbox.from_row(row) if row else None

    @classmethod
    def receive(
        cls,
        conn: Any,
        *,
        inbox_id: str,
        route_id: str,
        owner_id: int,
        event_id: str,
        fingerprint: str,
        payload_json: str,
        now: int,
    ) -> UnifiedInbox:
        conn.execute(
            "INSERT INTO unified_source_inbox(inbox_id,route_id,owner_id,event_id,fingerprint,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(route_id,event_id) DO NOTHING",
            (inbox_id, route_id, owner_id, event_id, fingerprint, payload_json, now, now),
        )
        row = conn.execute(
            "SELECT * FROM unified_source_inbox WHERE route_id=? AND event_id=? AND owner_id=?",
            (route_id, event_id, owner_id),
        ).fetchone()
        assert row is not None
        return UnifiedInbox.from_row(row)

    @staticmethod
    def pending(conn: Any, limit: int = 100) -> list[UnifiedInbox]:
        return [
            UnifiedInbox.from_row(row)
            for row in conn.execute(
                "SELECT i.* FROM unified_source_inbox i JOIN unified_source_routes r ON r.route_id=i.route_id WHERE i.status='received' AND r.state IN ('task','draft') ORDER BY i.id LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    @staticmethod
    def is_head(conn: Any, row: UnifiedInbox) -> bool:
        first = conn.execute(
            "SELECT inbox_id FROM unified_source_inbox WHERE route_id=? AND status='received' ORDER BY id LIMIT 1",
            (row.route_id,),
        ).fetchone()
        return bool(first and first["inbox_id"] == row.inbox_id)

    @staticmethod
    def admit(
        conn: Any, row: UnifiedInbox, task_id: str | None, turn_id: str | None, now: int
    ) -> None:
        conn.execute(
            "UPDATE unified_source_inbox SET status='admitted',task_id=?,turn_id=?,updated_at=? WHERE inbox_id=? AND owner_id=? AND status='received'",
            (task_id, turn_id, now, row.inbox_id, row.owner_id),
        )


@dataclass(frozen=True)
class UnifiedDelivery:
    delivery_id: str
    route_id: str
    owner_id: int
    task_id: str
    turn_id: str
    delivery_key: str
    payload_json: str
    status: str
    worker_id: str

    @classmethod
    def from_row(cls, row: DbRow) -> UnifiedDelivery:
        return cls(
            **{
                key: str(row[key])
                for key in (
                    "delivery_id",
                    "route_id",
                    "task_id",
                    "turn_id",
                    "delivery_key",
                    "payload_json",
                    "status",
                    "worker_id",
                )
            },
            owner_id=strict_int(row["owner_id"]),
        )


class UnifiedDeliveryRepo:
    @staticmethod
    def enqueue(
        conn: Any,
        *,
        delivery_id: str,
        route_id: str,
        owner_id: int,
        task_id: str,
        turn_id: str,
        delivery_key: str,
        payload_json: str,
        now: int,
    ) -> UnifiedDelivery:
        conn.execute(
            "INSERT INTO unified_source_deliveries(delivery_id,route_id,owner_id,task_id,turn_id,delivery_key,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(route_id,turn_id,delivery_key) DO NOTHING",
            (
                delivery_id,
                route_id,
                owner_id,
                task_id,
                turn_id,
                delivery_key,
                payload_json,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM unified_source_deliveries WHERE route_id=? AND turn_id=? AND delivery_key=?",
            (route_id, turn_id, delivery_key),
        ).fetchone()
        assert row is not None
        return UnifiedDelivery.from_row(row)

    @staticmethod
    def claim(conn: Any, delivery_id: str, worker_id: str, now: int) -> UnifiedDelivery | None:
        conn.execute(
            "UPDATE unified_source_deliveries SET status='uncertain',error_code='unknown_delivery_outcome',updated_at=? WHERE delivery_id=? AND status='sending' AND lease_until<=?",
            (now, delivery_id, now),
        )
        row = conn.execute(
            "UPDATE unified_source_deliveries SET status='sending',worker_id=?,lease_until=?,updated_at=? WHERE delivery_id=? AND status='pending' RETURNING *",
            (worker_id, now + 60_000, now, delivery_id),
        ).fetchone()
        return UnifiedDelivery.from_row(row) if row else None

    @staticmethod
    def finish(
        conn: Any,
        row: UnifiedDelivery,
        *,
        status: str,
        receipt_json: str = "{}",
        error_code: str = "",
        now: int,
    ) -> None:
        conn.execute(
            "UPDATE unified_source_deliveries SET status=?,receipt_json=?,error_code=?,updated_at=? WHERE delivery_id=? AND worker_id=? AND status='sending'",
            (status, receipt_json, error_code, now, row.delivery_id, row.worker_id),
        )


class CronOccurrenceRepo:
    @staticmethod
    def receive(
        conn: Any,
        *,
        occurrence_id: str,
        cron_id: str,
        owner_id: int,
        event_key: str,
        payload_json: str,
        now: int,
    ) -> Any:
        conn.execute(
            "INSERT INTO unified_cron_occurrences(occurrence_id,cron_id,owner_id,event_key,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(cron_id,event_key) DO NOTHING",
            (occurrence_id, cron_id, owner_id, event_key, payload_json, now, now),
        )
        return conn.execute(
            "SELECT * FROM unified_cron_occurrences WHERE cron_id=? AND event_key=?",
            (cron_id, event_key),
        ).fetchone()

    @staticmethod
    def update(
        conn: Any,
        occurrence_id: str,
        *,
        status: str,
        task_id: str | None = None,
        turn_id: str | None = None,
        now: int,
    ) -> None:
        conn.execute(
            "UPDATE unified_cron_occurrences SET status=?,task_id=COALESCE(?,task_id),turn_id=COALESCE(?,turn_id),updated_at=? WHERE occurrence_id=?",
            (status, task_id, turn_id, now, occurrence_id),
        )
