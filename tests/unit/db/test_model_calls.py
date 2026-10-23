"""Invocation receipts are private, durable and separate from legacy totals."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.model_calls import TOKEN_FIELDS, ModelCallRepo, ModelCallRow
from octop.infra.db.repos.usage import UsageRepo
from octop.infra.db.services import RepoBundle


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "model-calls.db")
    migrate.run_migrations(pool)
    try:
        yield pool
    finally:
        pool.close()


def call(call_id: str = "call-one", **updates: object) -> ModelCallRow:
    return replace(
        ModelCallRow(
            call_id=call_id,
            started_at=1000,
            user_id=1,
            provider="Box",
            model_ref="Box/local",
            source="third_party",
            purpose="execution",
            agent_id="agent-one",
            thread_id="thread-one",
            project_id="project-one",
            task_id="task-one",
            turn_id="turn-one",
            submission_id="submission-one",
        ),
        **updates,
    )


def test_source_defaults_to_third_party_and_only_accepts_box_or_third_party(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    default = repo.start(ModelCallRow(call_id="default-source", started_at=1000))
    assert default.source == "third_party"
    assert repo.start(call("box-source", source="box")).source == "box"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO model_calls(call_id, started_at) VALUES ('sql-default-source', 1000)"
        )
    assert repo.get("sql-default-source").source == "third_party"
    for source in ("self_hosted", "unknown"):
        with pytest.raises(ValueError, match="model call source"):
            repo.start(call("invalid-" + source, source=source))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
            conn.execute(
                "INSERT INTO model_calls(call_id, started_at, source) VALUES (?, 1000, ?)",
                ("sql-invalid-" + source, source),
            )


def test_unknown_usage_is_distinct_from_reported_zero(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    running = repo.start(call())
    unknown = json.loads(running.usage_json)
    assert all(unknown[key] is None for key in TOKEN_FIELDS)
    assert all(unknown["presence"][key] is False for key in TOKEN_FIELDS)
    finished = repo.finish(
        replace(
            running,
            status="succeeded",
            finished_at=1100,
            usage_json=json.dumps({"input_tokens": 0, "output_tokens": 3}),
        )
    )
    usage = json.loads(finished.usage_json)
    assert usage["input_tokens"] == 0 and usage["presence"]["input_tokens"] is True
    assert usage["output_tokens"] == 3 and usage["presence"]["output_tokens"] is True
    assert usage["cache_read_tokens"] is None
    assert usage["total_tokens"] is None
    assert finished.status == "succeeded" and finished.finished_at == 1100


def test_replayed_start_and_finish_keep_one_receipt_without_accumulation(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    initial = repo.start(call())
    receipt = replace(
        initial,
        status="succeeded",
        finished_at=1200,
        usage_json=json.dumps({"input_tokens": 11, "output_tokens": 7}),
    )
    saved = repo.finish(receipt)
    assert repo.finish(receipt) == saved
    assert repo.start(replace(call(), started_at=2000)) == saved
    assert repo.list_page() == [saved]
    assert json.loads(saved.usage_json)["input_tokens"] == 11


def test_finish_can_arrive_first_and_late_usage_does_not_restore_cancelled_status(
    db: SqlitePool,
) -> None:
    repo = ModelCallRepo(db)
    cancelled = repo.finish(call(status="cancelled", finished_at=1100))
    result = repo.finish(
        replace(
            cancelled,
            status="succeeded",
            finished_at=1200,
            usage_json=json.dumps({"input_tokens": 4, "output_tokens": 0}),
        )
    )
    assert result.status == "cancelled" and result.finished_at == 1100
    assert json.loads(result.usage_json)["input_tokens"] == 4
    assert json.loads(result.usage_json)["presence"]["output_tokens"] is True
    assert repo.start(call()) == result


def test_late_reported_usage_upgrades_matching_presence_without_replacing_values(
    db: SqlitePool,
) -> None:
    repo = ModelCallRepo(db)
    first = repo.finish(
        call(
            status="succeeded",
            finished_at=1100,
            usage_json=json.dumps(
                {
                    "input_tokens": 0,
                    "total_tokens": 3,
                    "presence": {"input_tokens": True, "total_tokens": False},
                }
            ),
            metadata_json='{"usage_source":"derived"}',
        )
    )
    enriched = repo.finish(
        replace(
            first,
            usage_json='{"input_tokens":7,"total_tokens":10}',
            metadata_json='{"usage_source":"usage_metadata"}',
        )
    )
    usage = json.loads(enriched.usage_json)
    assert usage["input_tokens"] == 0 and usage["total_tokens"] == 3
    assert usage["presence"]["total_tokens"] is False
    enriched = repo.finish(replace(enriched, usage_json='{"input_tokens":0,"total_tokens":3}'))
    assert json.loads(enriched.usage_json)["presence"]["total_tokens"] is True
    duplicate = repo.finish(
        replace(enriched, usage_json="{}", metadata_json='{"usage_source":"missing"}')
    )
    assert duplicate == enriched


def test_conflicting_identity_is_rejected_without_changing_the_receipt(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    original = repo.start(call())
    for update in ({"user_id": 2}, {"model_ref": "Remote/other"}, {"purpose": "title"}):
        with pytest.raises(ValueError, match="identity changed"):
            repo.start(replace(call(), **update))
    assert repo.get(original.call_id) == original


def test_private_json_fields_are_trimmed_to_numeric_and_trace_whitelists(db: SqlitePool) -> None:
    saved = ModelCallRepo(db).finish(
        call(
            status="failed",
            finished_at=1100,
            usage_json=json.dumps(
                {
                    "input_tokens": 9,
                    "output_tokens": True,
                    "total_tokens": -1,
                    "reasoning_tokens": 1.5,
                    "cache_read_tokens": "5",
                    "prompt": "private content",
                    "raw": {"headers": "secret"},
                }
            ),
            metadata_json=json.dumps(
                {
                    "version": 1,
                    "run_id": "run_123",
                    "parent_run_id": "parent-123",
                    "usage_source": "response_metadata",
                    "trace_id": "invalid trace with spaces",
                    "prompt": "private content",
                    "api_key": "secret",
                    "headers": {"Authorization": "secret"},
                    "exception": "private error",
                }
            ),
        )
    )
    usage = json.loads(saved.usage_json)
    assert set(usage) == {*TOKEN_FIELDS, "presence"}
    assert usage["input_tokens"] == 9
    assert all(usage[key] is None for key in TOKEN_FIELDS if key != "input_tokens")
    assert json.loads(saved.metadata_json) == {
        "version": 1,
        "run_id": "run_123",
        "parent_run_id": "parent-123",
        "usage_source": "response_metadata",
    }
    assert "private" not in saved.usage_json + saved.metadata_json
    assert "secret" not in saved.usage_json + saved.metadata_json


def test_owner_filters_and_stable_cursor_keep_identical_timestamps_separate(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    first = repo.start(call("first"))
    second = repo.start(call("second", user_id=2))
    third = repo.start(call("third", user_id=None))
    last = repo.start(call("last", purpose="routing", source="third_party"))
    assert repo.get(second.call_id, user_id=1) is None
    assert repo.get(third.call_id, user_id=1) is None
    assert repo.list_page(user_id=1, limit=1) == [last]
    assert repo.list_page(user_id=1, before_id=last.pk) == [first]
    assert repo.list_page(before_id=last.pk, limit=2) == [third, second]
    assert repo.list_page(purpose="routing", source="third_party") == [last]
    assert repo.list_page(thread_id="thread-one", provider="Box", model_ref="Box/local") == [
        last,
        third,
        second,
        first,
    ]
    assert repo.list_page(task_id="missing") == []
    assert repo.list_page(status="running", submission_id="submission-one") == [
        last,
        third,
        second,
        first,
    ]


def test_calls_do_not_change_legacy_usage_and_survive_resource_deletion(db: SqlitePool) -> None:
    repos = RepoBundle.from_pool(db)
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES('owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) VALUES('task-one',?,'Task','req',1,1)",
            (owner,),
        )
    saved = repos.model_call_repo.finish(
        call(user_id=owner, status="succeeded", finished_at=1100, usage_json='{"input_tokens":99}')
    )
    assert UsageRepo(db).summary(window="all")["total_tokens"] == 0
    assert UsageRepo(db).total_tokens_for_user(owner) == 0
    with db.transaction() as conn:
        conn.execute("DELETE FROM project_tasks WHERE task_id='task-one'")
        conn.execute("DELETE FROM users WHERE id=?", (owner,))
    assert repos.model_call_repo.get(saved.call_id) == saved
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_list(model_calls)").fetchall() == []


@pytest.mark.parametrize("value", [0, 501, True, 1.5])
def test_page_size_is_bounded(db: SqlitePool, value: object) -> None:
    with pytest.raises(ValueError, match="page size"):
        ModelCallRepo(db).list_page(limit=value)  # type: ignore[arg-type]


def test_running_and_terminal_timestamps_are_validated(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    with pytest.raises(ValueError, match="running"):
        repo.start(call(status="succeeded", finished_at=1100))
    with pytest.raises(ValueError, match="terminal"):
        repo.finish(call())
    repo.start(call())
    with pytest.raises(ValueError, match="original start"):
        repo.finish(call(started_at=500, finished_at=900, status="failed"))
    assert repo.get("call-one").status == "running"


def test_migration_upgrades_v53_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_migrations = tmp_path / "v53-migrations"
    old_migrations.mkdir()
    for version, path in migrate._discover("sqlite"):
        if version <= 53:
            shutil.copy2(path, old_migrations / path.name)
    pool = SqlitePool(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            migrate.run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 53
        migrate.run_migrations(pool)
        saved = ModelCallRepo(pool).start(call())
        migrate.run_migrations(pool)
        assert ModelCallRepo(pool).get(saved.call_id) == saved
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 56
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_migration_upgrades_v54_sources_without_changing_receipt_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_migrations = tmp_path / "v54-migrations"
    old_migrations.mkdir()
    for version, path in migrate._discover("sqlite"):
        if version <= 54:
            shutil.copy2(path, old_migrations / path.name)
    pool = SqlitePool(tmp_path / "upgrade-v54.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            migrate.run_migrations(pool)
        with pool.transaction() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 54
            for index, (source, status) in enumerate(
                (
                    ("box", "running"),
                    ("third_party", "succeeded"),
                    ("self_hosted", "failed"),
                    ("unknown", "interrupted"),
                )
            ):
                conn.execute(
                    "INSERT INTO model_calls(id,call_id,parent_call_id,submission_id,user_id,"
                    "agent_id,thread_id,project_id,task_id,turn_id,provider,model_ref,source,"
                    "purpose,started_at,finished_at,status,usage_json,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        10 + index * 7,
                        "legacy-" + source,
                        "parent-" + source,
                        "submission-" + source,
                        index + 1 if index < 3 else None,
                        "agent-" + source,
                        "thread-" + source,
                        "project-" + source,
                        "task-" + source,
                        "turn-" + source,
                        "provider-" + source,
                        "provider/model-" + source,
                        source,
                        "execution",
                        1000 + index,
                        None if status == "running" else 2000 + index,
                        status,
                        json.dumps(
                            {
                                "input_tokens": index,
                                "output_tokens": 0,
                                "cache_read_tokens": None,
                                "presence": {"input_tokens": True, "output_tokens": True},
                            }
                        ),
                        json.dumps({"trace_id": "trace-" + source, "usage_source": "missing"}),
                    ),
                )
            before = [dict(row) for row in conn.execute("SELECT * FROM model_calls ORDER BY id")]
            indexes = {
                row["name"]: tuple(
                    item["name"] for item in conn.execute(f"PRAGMA index_info({row['name']})")
                )
                for row in conn.execute("PRAGMA index_list(model_calls)")
                if row["name"].startswith("idx_model_calls_")
            }
        migrate.run_migrations(pool)
        expected = [
            {**row, "source": "box" if row["source"] == "box" else "third_party"} for row in before
        ]
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 56
            assert [
                dict(row) for row in conn.execute("SELECT * FROM model_calls ORDER BY id")
            ] == expected
            assert {
                row["name"]: tuple(
                    item["name"] for item in conn.execute(f"PRAGMA index_info({row['name']})")
                )
                for row in conn.execute("PRAGMA index_list(model_calls)")
                if row["name"].startswith("idx_model_calls_")
            } == indexes
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        saved = ModelCallRepo(pool).start(ModelCallRow(call_id="after-upgrade", started_at=3000))
        assert saved.source == "third_party"
        assert saved.pk > max(row["id"] for row in before)
        migrate.run_migrations(pool)
        assert ModelCallRepo(pool).get(saved.call_id) == saved
        with pool.connect() as conn:
            assert [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM model_calls WHERE call_id <> 'after-upgrade' ORDER BY id"
                )
            ] == expected
    finally:
        pool.close()
