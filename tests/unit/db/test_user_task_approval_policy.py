"""Migration from per-task approvals to a single account authority."""

from __future__ import annotations

import json
from pathlib import Path

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.user_task_approval_policies import UserTaskApprovalPolicyRepo


def test_global_policy_migration_advances_old_versions_and_preserves_snapshots(tmp_path: Path):
    pool = SqlitePool(tmp_path / "policy.db")
    try:
        run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute("INSERT INTO users(username,role,created_at) VALUES('one','admin',123)")
            conn.execute("INSERT INTO users(username,role,created_at) VALUES('two','user',124)")
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at,approval_mode,approval_revision) VALUES('old-task',1,'old','old-request',100,100,'full_access',19)"
            )
            conn.execute(
                "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,reply,status,error_code,activity,configuration_json,model_ref,worker_id,cancel_requested,created_at,updated_at) VALUES('old-turn','old-task','old-turn-request','test','','completed','','',?,'model','',0,100,100)",
                (
                    json.dumps(
                        {"approval_policy_at_start": {"mode": "full_access", "revision": 19}}
                    ),
                ),
            )
            original = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            turn = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
            conn.execute("DROP TABLE user_task_approval_policies")
            conn.execute("UPDATE _schema_version SET version=44")
        run_migrations(pool)
        with pool.connect() as conn:
            one = UserTaskApprovalPolicyRepo.get(conn, 1)
            two = UserTaskApprovalPolicyRepo.get(conn, 2)
            assert one.mode == two.mode == "ask_for_approval"
            assert one.revision == 20 and two.revision == 1
            assert dict(conn.execute("SELECT * FROM project_tasks").fetchone()) == original
            assert dict(conn.execute("SELECT * FROM project_task_turns").fetchone()) == turn
            conn.execute(
                "UPDATE user_task_approval_policies SET mode='approve_for_me',revision=21 WHERE owner_id=1"
            )
        run_migrations(pool)
        with pool.connect() as conn:
            assert UserTaskApprovalPolicyRepo.get(conn, 1).mode == "approve_for_me"
            assert UserTaskApprovalPolicyRepo.get(conn, 1).revision == 21
    finally:
        pool.close()
    reopened = SqlitePool(tmp_path / "policy.db")
    try:
        run_migrations(reopened)
        with reopened.connect() as conn:
            restored = UserTaskApprovalPolicyRepo.get(conn, 1)
            assert restored.mode == "approve_for_me" and restored.revision == 21
    finally:
        reopened.close()
