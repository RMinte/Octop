"""Remote Task summaries are cached references, never local execution rows."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.db.pool import DatabasePool


class FederatedTaskRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def peer_identity(self, owner_id: int, host_id: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT peer_identity FROM federated_task_hosts WHERE owner_id=? AND host_id=?",
                (owner_id, host_id),
            ).fetchone()
        return str(row["peer_identity"]) if row else None

    def bind_host(
        self, owner_id: int, host_id: str, peer_identity: str, connection_fingerprint: str
    ) -> tuple[str, str]:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO federated_task_hosts(host_id,owner_id,peer_identity,connection_fingerprint) VALUES(?,?,?,?) ON CONFLICT(host_id) DO NOTHING",
                (host_id, owner_id, peer_identity, connection_fingerprint),
            )
            row = conn.execute(
                "SELECT peer_identity,connection_fingerprint FROM federated_task_hosts WHERE host_id=? AND owner_id=?",
                (host_id, owner_id),
            ).fetchone()
        assert row is not None
        return str(row["peer_identity"]), str(row["connection_fingerprint"])

    def connection_fingerprint(self, owner_id: int, host_id: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT connection_fingerprint FROM federated_task_hosts WHERE owner_id=? AND host_id=?",
                (owner_id, host_id),
            ).fetchone()
        return str(row["connection_fingerprint"]) if row else None

    def remember(
        self, owner_id: int, host_id: str, peer_identity: str, task: dict[str, Any], now: int
    ) -> None:
        from octop.infra.utils.ulid import new_ulid

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO federated_task_refs(ref_id,owner_id,host_id,peer_identity,task_id,summary_json,observed_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,host_id,task_id) DO UPDATE SET "
                "peer_identity=excluded.peer_identity,summary_json=excluded.summary_json,observed_at=excluded.observed_at",
                (
                    "ref_" + new_ulid(),
                    owner_id,
                    host_id,
                    peer_identity,
                    str(task["task_id"]),
                    json.dumps(task, ensure_ascii=False),
                    now,
                ),
            )

    def list(
        self, owner_id: int, host_id: str, peer_identity: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT summary_json,observed_at FROM federated_task_refs WHERE owner_id=? AND host_id=? AND peer_identity=? ORDER BY observed_at DESC,id DESC LIMIT ?",
                (owner_id, host_id, peer_identity, limit),
            ).fetchall()
        return [
            {
                "host_id": host_id,
                "task_id": json.loads(row["summary_json"])["task_id"],
                "summary": json.loads(row["summary_json"]),
                "observed_at": row["observed_at"],
            }
            for row in rows
        ]

    def forget(self, owner_id: int, host_id: str, task_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM federated_task_refs WHERE owner_id=? AND host_id=? AND task_id=?",
                (owner_id, host_id, task_id),
            )
