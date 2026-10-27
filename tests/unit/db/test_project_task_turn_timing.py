"""Turn wall time follows observed execution boundaries across durable waits."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRepo, ProjectTaskTurnRow
from octop.infra.projects.tasks import TaskTurn, turn_view


@pytest.fixture
def turns(tmp_path: Path):
    db = SqlitePool(tmp_path / "turn-timing.db")
    run_migrations(db)
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('timing-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,created_at,updated_at) "
            "VALUES('timing-task',?,'Keep task','task-request','timing-turn',100,100)",
            (owner,),
        )
        ProjectTaskTurnRepo.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id="timing-turn",
                task_id="timing-task",
                request_id="turn-request",
                prompt="Keep prompt",
                reply="",
                status="queued",
                error_code="",
                activity="",
                configuration_json="{}",
                model_ref="fixture/model",
                worker_id="",
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            ),
        )
    try:
        yield db, ProjectTaskTurnRepo(db)
    finally:
        db.close()


def pause(db, repo, *, now=2_000, wait_kind="approval"):
    with db.transaction() as conn:
        assert repo.pause(
            conn,
            "timing-turn",
            "worker-one",
            "checkpoint-one",
            '{"calls":[]}',
            "Public answer",
            "Saved reasoning",
            "[]",
            0,
            now,
            wait_kind,
        )


def test_narration_write_requires_new_revision_and_preserves_public_body(turns):
    _db, repo = turns
    repo.claim("worker-one", 1_000)
    assert not repo.progress(
        "timing-turn",
        "worker-one",
        "",
        "",
        1_100,
        content_blocks_json='[{"text":"latest"}]',
        content_revision=5,
    )
    assert repo.progress(
        "timing-turn",
        "worker-one",
        "",
        "",
        1_200,
        content_blocks_json='[{"text":"stale"}]',
        content_revision=4,
        require_active=True,
    )
    assert repo.get("timing-turn").content_blocks_json == '[{"text":"latest"}]'
    assert not repo.progress(
        "timing-turn",
        "worker-one",
        "First answer token",
        "",
        1_300,
        content_blocks_json='[{"text":"answer"}]',
        content_revision=6,
    )
    for revision in (5, 6):
        assert repo.progress(
            "timing-turn",
            "worker-one",
            "",
            "",
            1_400,
            content_blocks_json='[{"text":"late narration"}]',
            content_revision=revision,
            require_active=True,
        )
    saved = repo.get("timing-turn")
    assert saved.reply == "First answer token" and saved.content_revision == 6
    assert saved.content_blocks_json == '[{"text":"answer"}]'
    refined = '[{"text":"answer"},{"brief":"latest actual tool result"}]'
    assert not repo.progress(
        "timing-turn",
        "worker-one",
        "First answer token",
        "",
        1_500,
        content_blocks_json=refined,
        content_revision=7,
        require_active=True,
    )
    updated = repo.get("timing-turn")
    assert updated.reply == "First answer token" and updated.content_revision == 7
    assert updated.content_blocks_json == refined


@pytest.mark.parametrize("guard", ["worker", "cancel", "pause", "waiting", "completed"])
def test_narration_after_public_body_keeps_active_worker_and_lifecycle_guards(turns, guard):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    assert not repo.progress(
        "timing-turn", "worker-one", "Public body", "", 1_100, content_revision=1
    )
    with db.transaction() as conn:
        if guard == "worker":
            conn.execute("UPDATE project_task_turns SET worker_id='worker-two'")
        elif guard == "cancel":
            conn.execute("UPDATE project_task_turns SET cancel_requested=1")
        elif guard == "pause":
            conn.execute("UPDATE project_task_turns SET pause_requested=1")
        elif guard == "waiting":
            assert repo.pause(
                conn,
                "timing-turn",
                "worker-one",
                "saved-checkpoint",
                '{"calls":[]}',
                "Public body",
                "",
                "[]",
                1,
                1_200,
                "approval",
            )
        else:
            assert repo.finish(
                conn, "timing-turn", "worker-one", "completed", "Public body", "", 1_200
            )
    before = repo.get("timing-turn")
    assert repo.progress(
        "timing-turn",
        "worker-one",
        "Public body",
        "",
        1_300,
        content_blocks_json='[{"brief":"must not be saved"}]',
        content_revision=2,
        require_active=True,
    )
    assert repo.get("timing-turn") == before


@pytest.mark.parametrize("wait_kind", ["approval", "question", "client_tool", "user"])
def test_first_claim_excludes_queue_and_keeps_wait_and_restart_wall_time(turns, wait_kind):
    db, repo = turns
    queued = repo.get("timing-turn")
    assert queued.started_at is None and queued.finished_at is None
    claimed = repo.claim("worker-one", 1_000)
    assert claimed.started_at == 1_000 and claimed.finished_at is None
    assert not repo.progress("timing-turn", "worker-one", "Public answer", "execute", 1_500)
    pause(db, repo, wait_kind=wait_kind)
    waiting = repo.get("timing-turn")
    assert waiting.started_at == 1_000 and waiting.finished_at is None
    assert turn_view(waiting).processing_duration_ms is None

    # A fresh repository represents a new worker reading the saved checkpoint.
    restarted = ProjectTaskTurnRepo(db)
    with db.transaction() as conn:
        assert restarted.resume_wait(conn, "timing-turn", 8_000)
    resumed = restarted.claim("worker-two", 9_000)
    assert resumed.started_at == 1_000 and resumed.finished_at is None
    with db.transaction() as conn:
        assert restarted.finish(
            conn, "timing-turn", "worker-two", "completed", "Final answer", "", 10_000
        )
    result = restarted.get("timing-turn")
    assert result.started_at == 1_000 and result.finished_at == 10_000
    view = turn_view(result)
    assert view.started_at == datetime.fromtimestamp(1, UTC)
    assert view.finished_at == datetime.fromtimestamp(10, UTC)
    assert view.processing_duration_ms == 9_000


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_worker_terminal_states_record_finish_once_and_reject_stale_worker(turns, status):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    with db.transaction() as conn:
        assert not repo.finish(conn, "timing-turn", "stale-worker", status, "Wrong", "", 2_000)
    assert repo.get("timing-turn").finished_at is None
    with db.transaction() as conn:
        assert repo.finish(conn, "timing-turn", "worker-one", status, "Keep reply", "", 3_000)
    before = repo.get("timing-turn")
    assert before.status == status and before.finished_at == 3_000
    assert turn_view(before).processing_duration_ms == 2_000
    with db.transaction() as conn:
        assert not repo.finish(
            conn, "timing-turn", "worker-one", "failed", "Overwrite", "TASK_INTERRUPTED", 5_000
        )
        repo.cancel(conn, "timing-turn", 6_000)
        assert not repo.interrupt(conn, "timing-turn", 10_000, 11_000)
        assert not repo.resume_wait(conn, "timing-turn", 12_000, "TASK_INTERRUPTED")
    assert repo.get("timing-turn") == before


def test_queued_cancel_has_known_finish_and_unknown_start_without_duration(turns):
    db, repo = turns
    with db.transaction() as conn:
        repo.cancel(conn, "timing-turn", 700)
    row = repo.get("timing-turn")
    assert row.status == "cancelled" and row.started_at is None and row.finished_at == 700
    assert turn_view(row).processing_duration_ms is None
    assert repo.claim("worker-one", 1_000) is None


def test_running_cancel_request_finishes_when_worker_reaches_terminal_boundary(turns):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    with db.transaction() as conn:
        repo.cancel(conn, "timing-turn", 2_000)
    requested = repo.get("timing-turn")
    assert requested.status == "running" and requested.finished_at is None
    with db.transaction() as conn:
        assert repo.finish(conn, "timing-turn", "worker-one", "completed", "Keep", "", 3_000)
    row = repo.get("timing-turn")
    assert row.status == "cancelled" and row.started_at == 1_000 and row.finished_at == 3_000


@pytest.mark.parametrize("wait_kind", ["approval", "user"])
def test_cancel_during_durable_wait_records_immediate_terminal_time(turns, wait_kind):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    pause(db, repo, wait_kind=wait_kind)
    with db.transaction() as conn:
        repo.cancel(conn, "timing-turn", 9_000)
    row = repo.get("timing-turn")
    assert row.status == "cancelled" and row.started_at == 1_000 and row.finished_at == 9_000
    assert turn_view(row).processing_duration_ms == 8_000


def test_unrecoverable_checkpoint_records_failed_finish(turns):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    pause(db, repo)
    recovery = (
        '{"phase":"checkpoint","can_resume":false,"reason_code":"TASK_INTERRUPTED",'
        '"cause_type":"ValueError","diagnostic_id":"diagnostic-one"}'
    )
    with db.transaction() as conn:
        assert repo.resume_wait(
            conn,
            "timing-turn",
            9_000,
            "TASK_INTERRUPTED",
            recovery_json=recovery,
        )
    row = repo.get("timing-turn")
    assert row.status == "failed" and row.started_at == 1_000 and row.finished_at == 9_000
    assert row.recovery_json == recovery
    assert turn_view(row).processing_duration_ms == 8_000


def test_expired_worker_records_failed_finish_only_when_lease_is_stale(turns):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    with db.transaction() as conn:
        assert not repo.interrupt(conn, "timing-turn", 1_000, 4_000)
    assert repo.get("timing-turn").finished_at is None
    with db.transaction() as conn:
        assert repo.interrupt(conn, "timing-turn", 2_000, 9_000)
    row = repo.get("timing-turn")
    assert row.status == "failed" and row.error_code == "TASK_INTERRUPTED"
    assert row.started_at == 1_000 and row.finished_at == 9_000


def test_pre_timing_checkpoint_resume_does_not_invent_first_start(turns):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    pause(db, repo)
    with db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET started_at=NULL")
        assert repo.resume_wait(conn, "timing-turn", 8_000)
    assert repo.claim("worker-two", 9_000).started_at is None
    with db.transaction() as conn:
        assert repo.finish(conn, "timing-turn", "worker-two", "completed", "Keep", "", 10_000)
    row = repo.get("timing-turn")
    assert row.started_at is None and row.finished_at == 10_000
    assert turn_view(row).processing_duration_ms is None


@pytest.mark.parametrize(
    "started,finished,duration",
    [(None, None, None), (1_000, None, None), (None, 2_000, None), (2_000, 1_000, None), (0, 0, 0)],
)
def test_api_timing_uses_only_known_ordered_boundaries(turns, started, finished, duration):
    _db, repo = turns
    row = replace(
        repo.get("timing-turn"), started_at=started, finished_at=finished, updated_at=99_999
    )
    view = turn_view(row)
    assert view.processing_duration_ms == duration
    assert view.started_at == (
        datetime.fromtimestamp(started / 1_000, UTC) if started is not None else None
    )
    assert view.finished_at == (
        datetime.fromtimestamp(finished / 1_000, UTC) if finished is not None else None
    )
    old_payload = view.model_dump(exclude={"started_at", "finished_at", "processing_duration_ms"})
    old_model = TaskTurn.model_validate(old_payload)
    assert old_model.started_at is None and old_model.finished_at is None
    assert old_model.processing_duration_ms is None


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "cancelled"])
def test_v43_migration_adds_null_timing_and_preserves_every_original_turn_column(turns, status):
    db, _repo = turns
    with db.transaction() as conn:
        conn.execute("ALTER TABLE project_task_turns DROP COLUMN started_at")
        conn.execute("ALTER TABLE project_task_turns DROP COLUMN finished_at")
        conn.execute(
            "UPDATE project_task_turns SET status=?,reply='Keep saved reply',reasoning='Keep reasoning',"
            "content_blocks_json='[]',content_revision=7,input_tokens=13,output_tokens=17",
            (status,),
        )
        conn.execute("UPDATE _schema_version SET version=43")
        before = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        assert after.pop("started_at") is None and after.pop("finished_at") is None
        assert after == before
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 58
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("missing", ["started_at", "finished_at"])
def test_current_watermark_repairs_missing_timing_column_without_changing_saved_facts(
    turns, missing
):
    db, repo = turns
    repo.claim("worker-one", 1_000)
    with db.transaction() as conn:
        assert repo.finish(conn, "timing-turn", "worker-one", "completed", "Keep", "", 3_000)
        conn.execute(f"ALTER TABLE project_task_turns DROP COLUMN {missing}")
        before = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        assert after.pop(missing) is None
        assert after == before
