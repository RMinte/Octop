"""tests/unit/test_repo_users.py"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.resident_conversations import ResidentConversationRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo, UserRow

_RESIDENT_TABLES = (
    "resident_conversations",
    "resident_requests",
    "resident_cancel_requests",
    "resident_delegations",
    "resident_task_reports",
    "resident_profiles",
    "resident_followups",
    "resident_notifications",
    "resident_inbox",
)


def _seed_resident_account(repo: UserRepo, username: str) -> int:
    uid = repo.create(username=username, password_hash="h", role="user")
    db = repo._db
    for role in ("primary", "profile:legacy"):
        agent_id = f"{username}-{role}"
        AgentRepo(db).create(agent_id=agent_id, user_id=uid, name=role)
        ThreadRepo(db).insert(
            thread_id=agent_id,
            agent_id=agent_id,
            user_id=uid,
            channel_type="resident",
            session_key=f"{agent_id}:resident:dm",
        )
        with db.transaction() as conn:
            assert ResidentConversationRepo.reserve_in_transaction(
                conn, uid, agent_id, agent_id, 0, role
            )
    agent_id = f"{username}-primary"
    profile_agent_id = f"{username}-profile:legacy"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO resident_requests(request_id,turn_id,owner_id,thread_id,prompt,"
            "created_at,updated_at) VALUES(?,?,?,?,?,0,0)",
            (username, username, uid, agent_id, "A question"),
        )
        conn.execute(
            "INSERT INTO resident_cancel_requests(owner_id,request_id,thread_id,"
            "target_turn_id,created_at) VALUES(?,?,?,?,0)",
            (uid, username, agent_id, username),
        )
        conn.execute(
            "INSERT INTO resident_delegations(delegation_id,owner_id,origin_agent_id,"
            "origin_thread_id,source_turn_id,task_id,initial_turn_id,request_id,"
            "fingerprint,timezone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'UTC',0,0)",
            (username, uid, agent_id, agent_id, username, username, username, username, "fp"),
        )
        conn.execute(
            "INSERT INTO resident_task_reports(report_id,message_id,delegation_id,owner_id,"
            "thread_id,task_id,task_turn_id,status,day_key,timezone,captured_at,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,'completed','1970-01-01','UTC',0,0,0)",
            (username, username, username, uid, agent_id, username, username),
        )
        conn.execute(
            "INSERT INTO resident_profiles(profile_id,owner_id,request_id,fingerprint,"
            "agent_id,thread_id,name,template_kind,template_id,template_version,snapshot_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,'builtin','default','1','{}',0,0)",
            (username, uid, username, "fp", profile_agent_id, profile_agent_id, "Legacy"),
        )
        conn.execute(
            "INSERT INTO resident_followups(followup_id,owner_id,agent_id,thread_id,request_id,"
            "fingerprint,purpose,kind,schedule,timezone,payload_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'note','date','UTC','{}',0,0)",
            (username, uid, agent_id, agent_id, username, "fp", "Reminder"),
        )
        conn.execute(
            "INSERT INTO resident_notifications(notification_id,owner_id,agent_id,thread_id,"
            "followup_id,authorization_version,source_key,kind,text,message_id,day_key,"
            "timezone,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,?,'note','Reminder',?,'1970-01-01','UTC',0,0)",
            (username, uid, agent_id, agent_id, username, username, f"notice-{username}"),
        )
        conn.execute(
            "INSERT INTO resident_inbox(event_id,owner_id,thread_id,source,source_id,"
            "message_id,created_at) VALUES(?,?,?,'notification',?,?,0)",
            (username, uid, agent_id, username, f"notice-{username}"),
        )
    return uid


def _resident_snapshot(repo: UserRepo, uid: int) -> dict[str, list[tuple[object, ...]]]:
    with repo._db.connect() as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} WHERE owner_id = ?", (uid,)
                ).fetchall()
            ]
            for table in _RESIDENT_TABLES
        }


@pytest.fixture
def repo(tmp_path: Path) -> UserRepo:
    db = SqlitePool(tmp_path / "x.db")
    run_migrations(db)
    return UserRepo(db)


def test_create_and_get(repo: UserRepo):
    uid = repo.create(username="alice", password_hash="h", role="admin", display_name="Alice")
    row = repo.get(uid)
    assert isinstance(row, UserRow)
    assert row.username == "alice"
    assert row.role == "admin"
    assert row.disabled == 0
    assert row.created_at <= int(time.time())


def test_get_by_username(repo: UserRepo):
    repo.create(username="bob", password_hash="h", role="user")
    assert repo.get_by_username("bob").username == "bob"
    assert repo.get_by_username("nobody") is None


def test_unique_username(repo: UserRepo):
    repo.create(username="a", password_hash="h", role="user")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(username="a", password_hash="h2", role="user")


def test_list_filters_disabled(repo: UserRepo):
    a = repo.create(username="a", password_hash="h", role="user")
    repo.create(username="b", password_hash="h", role="user")
    repo.set_disabled(a, True)
    rows = repo.list(include_disabled=False)
    assert [r.username for r in rows] == ["b"]
    assert len(repo.list(include_disabled=True)) == 2


def test_set_role(repo: UserRepo):
    uid = repo.create(username="a", password_hash="h", role="user")
    repo.set_role(uid, "admin")
    assert repo.get(uid).role == "admin"


def test_set_password_hash(repo: UserRepo):
    uid = repo.create(username="a", password_hash="h1", role="user")
    repo.set_password_hash(uid, "h2")
    assert repo.get(uid).password_hash == "h2"


def test_delete(repo: UserRepo):
    uid = repo.create(username="a", password_hash="h", role="user")
    repo.delete(uid)
    assert repo.get(uid) is None


def test_delete_cleans_only_the_requested_resident_account(repo: UserRepo) -> None:
    uid = _seed_resident_account(repo, "removed")
    other_uid = _seed_resident_account(repo, "retained")
    other_before = _resident_snapshot(repo, other_uid)
    assert all(other_before.values())
    repo.set_disabled(uid, True)

    repo.delete(uid)
    repo.delete(uid)

    assert repo.get(uid) is None
    assert repo.get(other_uid) is not None
    assert not any(_resident_snapshot(repo, uid).values())
    assert _resident_snapshot(repo, other_uid) == other_before
    assert AgentRepo(repo._db).list_by_user(uid) == []
    assert len(AgentRepo(repo._db).list_by_user(other_uid)) == 2
    assert ThreadRepo(repo._db).get("removed-primary") is None
    assert ThreadRepo(repo._db).get("retained-primary") is not None
    with repo._db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_failure_rolls_back_the_whole_resident_aggregate(repo: UserRepo) -> None:
    uid = _seed_resident_account(repo, "blocked")
    before = _resident_snapshot(repo, uid)
    with repo._db.transaction() as conn:
        conn.execute("CREATE TABLE deletion_blocker(owner_id INTEGER REFERENCES users(id))")
        conn.execute("INSERT INTO deletion_blocker(owner_id) VALUES(?)", (uid,))

    with pytest.raises(sqlite3.IntegrityError):
        repo.delete(uid)

    assert repo.get(uid) is not None
    assert _resident_snapshot(repo, uid) == before
    assert len(AgentRepo(repo._db).list_by_user(uid)) == 2
    assert ThreadRepo(repo._db).get("blocked-primary") is not None
    with repo._db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_count(repo: UserRepo):
    assert repo.count() == 0
    repo.create(username="a", password_hash="h", role="user")
    assert repo.count() == 1
