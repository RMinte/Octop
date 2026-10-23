"""Deletion fences block new work while accepted results can still settle."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from octop.config import OctopConfig
from octop.infra.cron import task_delivery
from octop.infra.cron.delivery import CronDeliveryCommand
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.unified_sources import UnifiedSourceRouteRepo
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import (
    branches,
    conversation_imports,
    deliveries,
    ingress,
    interactions,
    source_routes,
    tasks,
    workbench,
)
from octop.infra.projects.conversation_import_models import ConversationImport
from octop.infra.projects.deletion_state import assert_project_writable, assert_task_writable
from octop.infra.projects.interaction_models import InteractionResponse, TaskControl
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    result = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    with db.transaction() as conn:
        for owner in (1, 2):
            conn.execute(
                "INSERT INTO users(id,username,role,created_at) VALUES(?,?,'admin',1)",
                (owner, f"owner-{owner}"),
            )
        for project in ("project", "target"):
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) "
                "VALUES(?,1,?,1,1)",
                (project, project),
            )
        for identifier, project in (("task", "project"), ("standalone", None)):
            result.project_task_repo.insert(
                conn, ProjectTaskRow(identifier, 1, project, identifier, identifier, None, 1, 1)
            )
        result.project_task_turn_repo.insert_imported(
            conn,
            ProjectTaskTurnRow(
                "turn",
                "task",
                "accepted-message",
                "Question",
                "Answer",
                "completed",
                "",
                "",
                "{}",
                "",
                "",
                False,
                1,
                1,
            ),
        )
    try:
        yield result
    finally:
        db.close()


def mark(services, project="project"):
    with services.db.transaction() as conn:
        assert services.project_repo.lock_owner(conn, 1)
        services.project_repo.mark_deleting(conn, 1, project)


def assert_busy(operation):
    with pytest.raises(OctopError) as error:
        operation()
    assert error.value.code == ErrorCode.PROJECT_TASKS_BUSY
    assert error.value.status == 409


def test_fence_is_persistent_owner_scoped_and_idempotent(services):
    mark(services)
    mark(services)
    with services.db.transaction() as conn:
        assert services.project_repo.is_deleting(conn, 1, "project")
        assert not services.project_repo.is_deleting(conn, 2, "project")
        assert_project_writable(services, conn, 2, "project")
        assert_task_writable(services, conn, 1, "standalone")
        assert_busy(lambda: assert_task_writable(services, conn, 1, "task"))
        services.project_repo.clear_deleting(conn, 1, "project")
        assert_task_writable(services, conn, 1, "task")


def test_project_update_cannot_cross_or_clear_deletion_marker(services):
    current = services.project_repo.get("project", 1)
    mark(services)
    assert services.project_repo.update(replace(current, name="Changed"), current.revision) is None
    assert services.project_repo.get("project", 1) == current
    with services.db.connect() as conn:
        assert services.project_repo.is_deleting(conn, 1, "project")


@pytest.mark.parametrize("task_id", [None, "task"])
def test_new_message_is_blocked_before_admission(services, task_id):
    mark(services)
    with services.db.transaction() as conn:
        assert_busy(
            lambda: tasks._submission_state(
                services,
                conn,
                1,
                tasks.TaskMessage(request_id="new-message", prompt="Next"),
                task_id=task_id,
                project_id="project" if task_id is None else None,
            )
        )
        assert conn.execute("SELECT COUNT(*) FROM project_task_turns").fetchone()[0] == 1


def test_accepted_message_replay_remains_readable_but_patch_is_blocked(services):
    mark(services)
    with services.db.transaction() as conn:
        _, _, previous = tasks._submission_state(
            services,
            conn,
            1,
            tasks.TaskMessage(request_id="accepted-message", prompt="Question"),
            task_id="task",
        )
        assert previous.turn_id == "turn"
    assert_busy(
        lambda: tasks.patch(
            services, 1, "task", tasks.TaskPatch(expected_revision=1, title="Changed")
        )
    )
    assert services.project_task_repo.get("task", 1).title == "task"


@pytest.mark.parametrize("deleted_project", ["project", "target"])
def test_branch_checks_both_source_and_target_projects(services, deleted_project):
    mark(services, deleted_project)
    assert_busy(
        lambda: branches.create(
            services,
            1,
            "task",
            branches.TaskFork(request_id="fork-request", project_id="target"),
        )
    )
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 2


def test_new_notification_cannot_create_or_append_to_deleting_project(services):
    source = ingress.SourceEnvelope(
        source_kind="cron",
        source_instance="local",
        source_key="job",
        event_id="notice",
    )
    route = source_routes.resolve(
        services,
        1,
        source_kind="cron",
        source_instance="local",
        source_key="job",
    )
    source_routes.bind_task(services, 1, route.route_id, "task")
    mark(services)
    for selected in (source, source.model_copy(update={"source_key": "new-job"})):
        assert_busy(
            lambda selected=selected: ingress.record_notification(
                services, 1, selected, "Notification", project_id="project"
            )
        )
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM unified_source_inbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_task_turns").fetchone()[0] == 1


async def test_queued_notification_rechecks_fence_at_final_admission(services):
    source = ingress.SourceEnvelope(
        source_kind="cron",
        source_instance="local",
        source_key="job",
        event_id="notice",
        operation="notification",
        notification_text="Notification",
    )
    receipt = ingress.receive(
        services,
        1,
        source,
        tasks.TaskCreate(request_id="placeholder", prompt="Notification", project_id="project"),
    )
    mark(services)
    with pytest.raises(OctopError) as error:
        await ingress.admit(services, 1, receipt.inbox_id)
    assert error.value.code == ErrorCode.PROJECT_TASKS_BUSY
    assert ingress.get_receipt(services, 1, receipt.inbox_id).status == "received"
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 2


def test_external_import_cannot_publish_into_deleting_project(services):
    mark(services)
    body = ConversationImport(
        source="codex",
        external_session_id="imported",
        project_id="project",
        title="Imported",
        source_updated_at=datetime.now(UTC),
        turns=[
            {
                "external_turn_id": "original",
                "user_message": {"text": "Question"},
                "assistant_message": {"text": "Answer"},
            }
        ],
    )
    assert_busy(lambda: conversation_imports.import_conversation(services, 1, body))
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_import_sessions").fetchone()[0] == 0


def test_resume_and_new_interaction_decision_are_blocked(services):
    mark(services)
    assert_busy(
        lambda: interactions.control(
            services,
            1,
            "task",
            TaskControl(request_id="resume-request", turn_id="turn"),
            paused=False,
        )
    )
    assert_busy(
        lambda: interactions.respond(
            services,
            1,
            "task",
            "turn",
            "question",
            InteractionResponse(
                request_id="answer-request",
                expected_revision=1,
                response={"kind": "question", "action": "answer", "answer": "Yes"},
            ),
        )
    )
    assert services.project_task_turn_repo.get("turn").requests_json == "[]"


def test_workbench_keeps_read_access_and_rejects_mutable_scope(services):
    mark(services)
    assert workbench.resolve(services, 1, "task").view.task_id == "task"
    assert_busy(lambda: workbench.resolve(services, 1, "task", mutable=True))


def test_deleting_project_cannot_enqueue_or_start_outbound_delivery(services):
    route = source_routes.resolve(
        services, 1, source_kind="im", source_instance="local", source_key="outbound"
    )
    source_routes.bind_task(services, 1, route.route_id, "task")
    pending = deliveries.enqueue(
        services, 1, route.route_id, "task", "turn", "first", {"text": "Saved reply"}
    )
    mark(services)
    assert deliveries.claim(services, pending) is None
    assert_busy(
        lambda: deliveries.enqueue(
            services, 1, route.route_id, "task", "turn", "next", {"text": "Later reply"}
        )
    )
    with services.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM unified_source_deliveries").fetchone()[0] == 1


@pytest.mark.parametrize("deleting", [False, True])
def test_direct_cron_reset_checks_project_fence_without_breaking_normal_reset(services, deleting):
    route = source_routes.resolve(
        services, 1, source_kind="cron", source_instance="local", source_key="fresh-job"
    )
    source_routes.bind_task(services, 1, route.route_id, "task")
    with services.db.connect() as conn:
        before = UnifiedSourceRouteRepo.get(conn, 1, route.route_id)
    if deleting:
        mark(services)
        assert_busy(lambda: source_routes.reset(services, 1, route.route_id, "cron-new-run"))
    else:
        source_routes.reset(services, 1, route.route_id, "cron-new-run")
    with services.db.connect() as conn:
        after = UnifiedSourceRouteRepo.get(conn, 1, route.route_id)
        if deleting:
            assert after == before
        else:
            assert after.state == "draft" and after.task_id is None
            assert after.revision == before.revision + 1
        assert services.project_task_repo.get_in_transaction(conn, "task", 1) is not None


def test_task_delete_removes_memory_jobs_and_late_capture_cannot_recreate_them(services):
    def enqueue(identifier, *, task="task", turn="turn"):
        services.expert_memory_job_repo.enqueue(
            job_id=identifier,
            owner_id=1,
            expert_id="expert",
            task_id=task,
            turn_id=turn,
            namespace="scope",
            payload_json='{"prompt":"Private task text"}',
            now=1,
        )

    enqueue("captured")
    assert services.expert_memory_job_repo.get("captured") is not None
    mark(services)
    enqueue("after-fence")
    assert services.expert_memory_job_repo.get("after-fence") is None
    with services.db.transaction() as conn:
        services.project_repo.clear_deleting(conn, 1, "project")
        conn.execute(
            "UPDATE expert_memory_jobs SET result_json=? WHERE job_id='captured'",
            ('{"proposal":"Private extracted text"}',),
        )
        services.project_task_repo.delete(
            conn, services.project_task_repo.get_in_transaction(conn, "task", 1)
        )
    assert services.expert_memory_job_repo.get("captured") is None
    enqueue("captured")
    enqueue("missing-turn", task="standalone")
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM expert_memory_jobs").fetchone()[0] == 0


def test_task_delete_clears_pending_source_and_disables_target_cron(services):
    source = ingress.SourceEnvelope(
        source_kind="im",
        source_instance="local",
        source_key="chat",
        event_id="pending",
    )
    route = source_routes.resolve(
        services,
        1,
        source_kind="im",
        source_instance="local",
        source_key="chat",
    )
    source_routes.bind_task(services, 1, route.route_id, "task")
    receipt = ingress.receive(
        services, 1, source, tasks.TaskCreate(request_id="placeholder", prompt="Pending")
    )
    with services.db.transaction() as conn:
        for identifier, target, project in (
            ("target-job", "task", None),
            ("project-job", None, "project"),
            ("other-job", "standalone", None),
        ):
            conn.execute(
                "INSERT INTO cron_jobs(cron_id,user_id,schedule_spec,prompt,session_key,"
                "created_at,updated_at,target_task_id,project_id) VALUES(?,1,'{}','Prompt',?,1,1,?,?)",
                (identifier, identifier, target, project),
            )
        services.project_task_repo.delete(
            conn, services.project_task_repo.get_in_transaction(conn, "task", 1)
        )
        pending = conn.execute(
            "SELECT task_id,status,payload_json FROM unified_source_inbox WHERE inbox_id=?",
            (receipt.inbox_id,),
        ).fetchone()
        assert tuple(pending) == (None, "target_deleted", "{}")
        saved_route = UnifiedSourceRouteRepo.get(conn, 1, route.route_id)
        assert saved_route.task_id is None and saved_route.state == "task"
        assert saved_route.revision > 1
        assert (
            conn.execute("SELECT enabled FROM cron_jobs WHERE cron_id='target-job'").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT enabled FROM cron_jobs WHERE cron_id='other-job'").fetchone()[0]
            == 1
        )
        services.project_repo.mark_deleting(conn, 1, "project")
        services.project_repo.delete(
            conn, services.project_repo.get_in_transaction(conn, "project", 1)
        )
        assert not services.project_repo.is_deleting(conn, 1, "project")
        assert conn.execute(
            "SELECT enabled,project_id FROM cron_jobs WHERE cron_id='project-job'"
        ).fetchone()[:] == (0, None)
        for identifier in ("target-job", "project-job"):
            assert (
                conn.execute(
                    "SELECT value FROM settings WHERE key=?",
                    (f"deleted_cron_binding:{identifier}",),
                ).fetchone()[0]
                == "1"
            )
        assert (
            conn.execute(
                "SELECT value FROM settings WHERE key='deleted_cron_binding:other-job'"
            ).fetchone()
            is None
        )
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    with pytest.raises(OctopError) as error:
        ingress.receive(
            services,
            1,
            source.model_copy(update={"event_id": "after-delete"}),
            tasks.TaskCreate(request_id="placeholder", prompt="After"),
        )
    assert error.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("binding", ["project", "task"])
async def test_deleted_cron_binding_blocks_manual_run_until_explicit_reselection(services, binding):
    services.cron_repo.create(
        cron_id="bound-job",
        agent_id=None,
        user_id=1,
        trigger="every:1h",
        prompt="Question",
        session_key="bound-session",
        conversation_policy="per_job" if binding == "project" else "task",
        project_id="project" if binding == "project" else None,
        target_task_id="task" if binding == "task" else None,
    )
    with services.db.transaction() as conn:
        services.project_task_repo.delete(
            conn, services.project_task_repo.get_in_transaction(conn, "task", 1)
        )
        if binding == "project":
            services.project_repo.delete(
                conn, services.project_repo.get_in_transaction(conn, "project", 1)
            )
    service = SimpleNamespace(_task_services=services)

    def command():
        row = services.cron_repo.get("bound-job")
        return CronDeliveryCommand(
            cron_id=row.cron_id,
            cron_name=row.name,
            agent_id=row.agent_id,
            user_id=row.user_id,
            session_key=row.session_key,
            prompt=row.prompt,
            fresh_thread=False,
            task_type="agent",
            model=None,
            mcp_servers=(),
            event_key="manual:after-restart",
            conversation_policy=row.conversation_policy,
            target_task_id=row.target_task_id,
            project_id=row.project_id,
        )

    for renamed in (False, True):
        if renamed:
            services.cron_repo.update("bound-job", name="Renamed", enabled=True)
        assert services.settings_repo.get("deleted_cron_binding:bound-job") == "1"
        with pytest.raises(OctopError) as error:
            await task_delivery.deliver(service, command())
        assert error.value.code == ErrorCode.FORBIDDEN
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM unified_cron_occurrences").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 1
    if binding == "project":
        services.cron_repo.update("bound-job", project_id=None)
    else:
        services.cron_repo.update("bound-job", target_task_id="standalone")
    assert services.settings_repo.get("deleted_cron_binding:bound-job") is None
    task_delivery._authorize(service, command())
