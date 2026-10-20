"""Durable provenance and explicit template revisions of private professional instances."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ResidentProfileRow:
    profile_id: str
    owner_id: int
    request_id: str
    fingerprint: str
    agent_id: str
    thread_id: str
    name: str
    template_kind: str
    template_id: str
    template_version: str
    snapshot_json: str
    revision: int
    archived: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentProfileRow:
        return cls(
            profile_id=str(row["profile_id"]),
            owner_id=strict_int(row["owner_id"]),
            request_id=str(row["request_id"]),
            fingerprint=str(row["fingerprint"]),
            agent_id=str(row["agent_id"]),
            thread_id=str(row["thread_id"]),
            name=str(row["name"]),
            template_kind=str(row["template_kind"]),
            template_id=str(row["template_id"]),
            template_version=str(row["template_version"]),
            snapshot_json=str(row["snapshot_json"]),
            revision=strict_int(row["revision"]),
            archived=bool(strict_int(row["archived"])),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
        )


class ResidentProfileRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, owner_id: int, profile_id: str) -> ResidentProfileRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, owner_id, profile_id)

    def configuration_snapshot(self, owner_id: int, agent_id: str) -> dict[str, Any]:
        """Read prompt and template audit facts from one consistent database snapshot."""
        with self._db.read_snapshot() as conn:
            row = conn.execute(
                "SELECT a.system_prompt,a.updated_at AS agent_updated_at,p.profile_id,"
                "p.revision AS profile_revision,p.template_version FROM agents a "
                "LEFT JOIN resident_profiles p ON p.agent_id=a.agent_id AND p.owner_id=a.user_id "
                "WHERE a.user_id=? AND a.agent_id=?",
                (owner_id, agent_id),
            ).fetchone()
        if row is None:
            return {}
        return {
            "system_prompt": row["system_prompt"],
            "agent_updated_at": strict_int(row["agent_updated_at"]),
            "profile_id": row["profile_id"],
            "profile_revision": strict_int(row["profile_revision"])
            if row["profile_revision"] is not None
            else None,
            "template_version": row["template_version"],
        }

    @staticmethod
    def get_in_transaction(conn: Any, owner_id: int, profile_id: str) -> ResidentProfileRow | None:
        row = conn.execute(
            "SELECT * FROM resident_profiles WHERE owner_id=? AND profile_id=?",
            (owner_id, profile_id),
        ).fetchone()
        return ResidentProfileRow.from_row(row) if row else None

    def get_request(self, owner_id: int, request_id: str) -> ResidentProfileRow | None:
        with self._db.connect() as conn:
            return self.request_in_transaction(conn, owner_id, request_id)

    def get_for_agent(self, owner_id: int, agent_id: str) -> ResidentProfileRow | None:
        with self._db.connect() as conn:
            return self.get_by_agent_in_transaction(conn, owner_id, agent_id)

    @staticmethod
    def get_by_agent_in_transaction(
        conn: Any, owner_id: int, agent_id: str
    ) -> ResidentProfileRow | None:
        row = conn.execute(
            "SELECT * FROM resident_profiles WHERE owner_id=? AND agent_id=?",
            (owner_id, agent_id),
        ).fetchone()
        return ResidentProfileRow.from_row(row) if row else None

    @staticmethod
    def request_in_transaction(
        conn: Any, owner_id: int, request_id: str
    ) -> ResidentProfileRow | None:
        row = conn.execute(
            "SELECT * FROM resident_profiles WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return ResidentProfileRow.from_row(row) if row else None

    def list_by_owner(self, owner_id: int) -> list[ResidentProfileRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resident_profiles WHERE owner_id=? ORDER BY id",
                (owner_id,),
            ).fetchall()
        return [ResidentProfileRow.from_row(row) for row in rows]

    @staticmethod
    def insert_in_transaction(conn: Any, row: ResidentProfileRow) -> None:
        conn.execute(
            "INSERT INTO resident_profiles(profile_id,owner_id,request_id,fingerprint,agent_id,"
            "thread_id,name,template_kind,template_id,template_version,snapshot_json,revision,archived,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.profile_id,
                row.owner_id,
                row.request_id,
                row.fingerprint,
                row.agent_id,
                row.thread_id,
                row.name,
                row.template_kind,
                row.template_id,
                row.template_version,
                row.snapshot_json,
                row.revision,
                int(row.archived),
                row.created_at,
                row.updated_at,
            ),
        )

    @staticmethod
    def adopt_in_transaction(
        conn: Any,
        *,
        owner_id: int,
        profile_id: str,
        expected_revision: int,
        template_version: str,
        snapshot_json: str,
        now: int,
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE resident_profiles SET template_version=?,snapshot_json=?,revision=revision+1,"
                "updated_at=? WHERE owner_id=? AND profile_id=? AND revision=?",
                (template_version, snapshot_json, now, owner_id, profile_id, expected_revision),
            ).rowcount
        )

    @staticmethod
    def archive_in_transaction(
        conn: Any,
        *,
        owner_id: int,
        profile_id: str,
        expected_revision: int,
        archived: bool,
        now: int,
    ) -> bool:
        return bool(
            conn.execute(
                "UPDATE resident_profiles SET archived=?,revision=revision+1,updated_at=? "
                "WHERE owner_id=? AND profile_id=? AND revision=?",
                (int(archived), now, owner_id, profile_id, expected_revision),
            ).rowcount
        )
