"""Branches preserve visible source work without copying execution authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from pydantic import ValidationError

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_artifacts import ProjectTaskArtifactRow
from octop.infra.db.repos.project_task_attachments import ProjectTaskAttachmentRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import branches, runtime, tasks
from octop.infra.projects.branches import TaskFork
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "branches.db")
    run_migrations(db)
    result = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    result.secret_repo.get_or_create("jwt", lambda: b"branch-test-secret")
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(2,'other','user',1)")
        for project_id, owner in (("project", 1), ("target", 1), ("foreign-project", 2)):
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES(?,?,?,1,1)",
                (project_id, owner, project_id),
            )
    result.agent_repo.create(agent_id="expert", user_id=1, name="Accountant")
    result.agent_repo.create(agent_id="foreign-expert", user_id=2, name="Other")
    with db.transaction() as conn:
        result.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                "source",
                1,
                "project",
                "Original work",
                "source-request",
                None,
                1,
                2,
                status="completed",
                selected_expert_id="expert",
                action_mode="plan",
                pinned=True,
                approval_mode="no_approval",
            ),
        )
        attached = b"Quarter,Revenue\nQ1,12\n"
        result.project_task_attachment_repo.insert(
            conn,
            ProjectTaskAttachmentRow(
                "input_original",
                1,
                "source",
                "sales.csv",
                "text/csv",
                len(attached),
                hashlib.sha256(attached).hexdigest(),
                1,
            ),
            attached,
        )
        for index in (1, 2):
            configuration = {
                "expert_profile": {
                    "expert_id": "expert",
                    "name": "Accountant",
                    "config_revision": 4,
                    "system_prompt": "Private persona",
                    "memory": {"credential": "do-not-copy"},
                },
                "directory": str(tmp_path / "source-directory"),
                "client_directory": "opaque-client-directory",
                "action_mode": "plan",
                "attachment_ids": ["input_original"] if index == 2 else [],
                "execution_options": {"client_tools": [{"name": "client__write"}]},
                "approval_policy_at_start": {"mode": "no_approval"},
            }
            result.project_task_turn_repo.insert_imported(
                conn,
                ProjectTaskTurnRow(
                    turn_id=f"source-turn-{index}",
                    task_id="source",
                    request_id=f"source-message-{index}",
                    prompt=f"Question {index}",
                    reply=(
                        "[Report](/api/project-tasks/source/artifacts/artifact_original/download)"
                        if index == 1
                        else "Second answer"
                    ),
                    status="completed",
                    error_code="",
                    activity="",
                    configuration_json=json.dumps(configuration),
                    model_ref="provider/model",
                    worker_id="old-worker",
                    cancel_requested=False,
                    created_at=index,
                    updated_at=index,
                    content_revision=1,
                    content_blocks_json=json.dumps(
                        [
                            {
                                "block_id": "old-block",
                                "type": "tool",
                                "status": "completed",
                                "tool_call_id": "original-call",
                                "tool_name": "publish_workspace_file",
                                "output": {"artifact_id": "artifact_original"},
                            }
                        ]
                    )
                    if index == 1
                    else "[]",
                    finished_at=index,
                ),
            )
        result.project_task_artifact_repo.insert(
            conn,
            ProjectTaskArtifactRow(
                "artifact_original",
                "source",
                "source-turn-1",
                "original-call",
                "old-hash",
                "report.txt",
                "text/plain",
                6,
                hashlib.sha256(b"Report").hexdigest(),
                "original/report.txt",
                1,
            ),
            b"Report",
        )
        conn.execute(
            "INSERT INTO project_task_contexts(task_id,turn_id,messages_json) VALUES('source','source-turn-2',?)",
            ('[{"type":"tool","data":{"content":"Private checkpoint output"}}]',),
        )
        conn.execute(
            "UPDATE project_task_turns SET input_tokens=12,output_tokens=4,worker_id='old-worker',"
            "checkpoint_id='old-checkpoint',interactions_json='[{\"approval\":true}]' WHERE task_id='source'"
        )
    try:
        yield result
    finally:
        db.close()


def test_fork_copies_visible_history_and_owned_bytes_without_execution_state(services):
    original = services.project_task_turn_repo.list("source")
    created = branches.create(services, 1, "source", TaskFork(request_id="fork-once"))
    assert created.continuation_prompt is None and not created.continuation_attachment_ids
    target = services.project_task_repo.get(created.task_id, 1)
    assert target.title == "Original work (2)"
    assert (target.project_id, target.selected_expert_id, target.action_mode) == (
        "project",
        "expert",
        "plan",
    )
    assert target.active_turn_id is None and not target.pinned and not target.archived
    assert target.approval_mode == "ask_for_approval"
    copied = services.project_task_turn_repo.list(created.task_id)
    assert [turn.prompt for turn in copied] == ["Question 1", "Question 2"]
    assert services.project_task_turn_repo.list("source") == original
    assert services.project_task_turn_repo.context(created.task_id) is None
    for source, turn in zip(original, copied, strict=True):
        configuration = json.loads(turn.configuration_json)
        assert configuration["branch_origin"] == {
            "source_task_id": "source",
            "source_turn_id": source.turn_id,
            "operation": "fork",
            "history_only": True,
            "source_status": "completed",
        }
        assert configuration["expert_profile"] == {
            "expert_id": "expert",
            "name": "Accountant",
            "config_revision": 4,
        }
        assert "directory" not in configuration and "execution_options" not in configuration
        assert (
            "approval_policy_at_start" not in configuration
            and "do-not-copy" not in turn.configuration_json
        )
        assert not turn.worker_id and not turn.checkpoint_id and turn.interactions_json == "[]"
        assert (turn.input_tokens, turn.output_tokens) == (0, 0)
    with services.db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT title_source FROM project_tasks WHERE task_id=?", (created.task_id,)
            ).fetchone()[0]
            == "generated"
        )
        owned = services.project_task_attachment_repo.for_task(conn, 1, created.task_id)
        assert len(owned) == 1 and owned[0].attachment_id != "input_original"
        assert (
            services.project_task_attachment_repo.download(conn, 1, owned[0].attachment_id)[1]
            == b"Quarter,Revenue\nQ1,12\n"
        )
        artifacts = services.project_task_artifact_repo.for_turns(
            conn, created.task_id, [copied[0].turn_id]
        )
        assert len(artifacts) == 1 and artifacts[0].source_path is None
        assert (
            services.project_task_artifact_repo.download(
                conn, created.task_id, artifacts[0].artifact_id
            )[1]
            == b"Report"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM project_task_tool_calls WHERE task_id=?", (created.task_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM project_task_execution_receipts WHERE task_id=?",
                (created.task_id,),
            ).fetchone()[0]
            == 0
        )
    assert (
        f"/api/project-tasks/{created.task_id}/artifacts/{artifacts[0].artifact_id}/download"
        in copied[0].reply
    )
    assert tasks.detail(services, 1, created.task_id).turns[0].history_only


@pytest.mark.parametrize("operation", ["edit", "regenerate"])
def test_edit_and_regenerate_branch_before_anchor_and_clone_continuation_inputs(
    services, operation
):
    body = {"request_id": "branch-" + operation, "operation": operation, "turn_id": "source-turn-2"}
    if operation == "edit":
        body["prompt"] = "Edited second question"
    created = branches.create(services, 1, "source", TaskFork.model_validate(body))
    assert services.project_task_repo.get(created.task_id, 1).title == "Original work (2)"
    assert [turn.prompt for turn in services.project_task_turn_repo.list(created.task_id)] == [
        "Question 1"
    ]
    assert created.continuation_prompt == (
        "Edited second question" if operation == "edit" else "Question 2"
    )
    assert len(created.continuation_attachment_ids) == 1
    assert created.continuation_attachment_ids != ["input_original"]
    with services.db.read_snapshot() as conn:
        attached = services.project_task_attachment_repo.get(
            conn, 1, created.continuation_attachment_ids[0]
        )
        assert attached.task_id == created.task_id


def test_idempotency_returns_same_branch_after_source_changes(services):
    body = TaskFork(request_id="repeat-fork", prompt="Continue here")
    created = branches.create(services, 1, "source", body)
    assert services.project_task_repo.get(created.task_id, 1).title == "Original work (2)"
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_tasks SET active_turn_id='another-turn' WHERE task_id='source'"
        )
        before = conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM project_task_attachments").fetchone()[0]
    assert branches.create(services, 1, "source", body) == created
    with services.db.read_snapshot() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM project_task_attachments").fetchone()[0] == files
    with pytest.raises(OctopError) as error:
        branches.create(
            services, 1, "source", body.model_copy(update={"prompt": "Different intent"})
        )
    assert error.value.code == ErrorCode.TASK_REQUEST_CONFLICT
    with services.db.transaction() as conn:
        conn.execute("UPDATE project_tasks SET active_turn_id=NULL WHERE task_id='source'")
    second = branches.create(services, 1, "source", TaskFork(request_id="second-fork"))
    assert services.project_task_repo.get(second.task_id, 1).title == "Original work (3)"
    assert services.project_task_repo.get("source", 1).title == "Original work"


def test_branch_title_uses_first_available_number_across_owned_tasks(services):
    with services.db.transaction() as conn:
        for task_id, owner, project, number, archived, internal_kind in (
            ("archived", 1, "target", 2, True, ""),
            ("unassigned", 1, None, 3, False, ""),
            ("other-owner", 2, "foreign-project", 4, False, ""),
            ("internal", 1, "project", 4, False, "resident"),
            ("later-number", 1, "project", 6, False, ""),
        ):
            services.project_task_repo.insert(
                conn,
                ProjectTaskRow(
                    task_id,
                    owner,
                    project,
                    f"Original work ({number})",
                    task_id,
                    None,
                    1,
                    1,
                    archived=archived,
                    internal_kind=internal_kind,
                ),
            )
    created = branches.create(services, 1, "source", TaskFork(request_id="occupied-titles"))
    assert services.project_task_repo.get(created.task_id, 1).title == "Original work (4)"


@pytest.mark.parametrize("original", ["目录_100%！'quoted'", "Release (2026)", "界" * 128])
def test_branch_title_preserves_original_text_and_fits_title_limit(services, original):
    with services.db.transaction() as conn:
        conn.execute("UPDATE project_tasks SET title=? WHERE task_id='source'", (original,))
    created = branches.create(services, 1, "source", TaskFork(request_id="title-text"))
    title = services.project_task_repo.get(created.task_id, 1).title
    assert title == original[:124] + " (2)"
    assert len(title) <= 128


@pytest.mark.parametrize("title_source", ["generated", "manual", "provisional"])
def test_nested_branch_title_flattens_only_generated_number(services, title_source):
    first = branches.create(services, 1, "source", TaskFork(request_id="first-level"))
    if title_source == "manual":
        row = services.project_task_repo.get(first.task_id, 1)
        tasks.patch(
            services,
            1,
            first.task_id,
            tasks.TaskPatch(expected_revision=row.list_revision, title="Original work (2)"),
        )
    with services.db.transaction() as conn:
        if title_source == "provisional":
            conn.execute(
                "UPDATE project_tasks SET title='Release (2026)',title_source='provisional' "
                "WHERE task_id=?",
                (first.task_id,),
            )
        services.project_task_turn_repo.insert_imported(
            conn,
            replace(
                services.project_task_turn_repo.get_in_transaction(conn, "source-turn-2"),
                task_id=first.task_id,
                turn_id="branch-new-turn",
                request_id="branch-new-message",
                prompt="New work in the branch",
                configuration_json="{}",
            ),
        )
    nested = branches.create(services, 1, first.task_id, TaskFork(request_id="second-level"))
    expected = {
        "generated": "Original work (3)",
        "manual": "Original work (2) (2)",
        "provisional": "Release (2026) (2)",
    }[title_source]
    assert services.project_task_repo.get(nested.task_id, 1).title == expected


def test_explicit_null_selections_clear_inherited_project_and_expert(services):
    created = branches.create(
        services,
        1,
        "source",
        TaskFork(request_id="clear-settings", project_id=None, selected_expert_id=None),
    )
    row = services.project_task_repo.get(created.task_id, 1)
    assert row.project_id is None and row.selected_expert_id is None
    target = branches.create(
        services, 1, "source", TaskFork(request_id="move-project", project_id="target")
    )
    assert services.project_task_repo.get(target.task_id, 1).project_id == "target"


@pytest.mark.parametrize(
    "failure",
    [
        "owner",
        "busy",
        "missing_anchor",
        "failed_anchor",
        "project",
        "expert",
        "attachment",
        "artifact",
    ],
)
def test_invalid_branch_is_atomic_and_never_drops_missing_files(services, failure):
    body = {"request_id": "invalid-" + failure}
    owner = 2 if failure == "owner" else 1
    with services.db.transaction() as conn:
        if failure == "busy":
            conn.execute("UPDATE project_tasks SET active_turn_id='active' WHERE task_id='source'")
        elif failure == "missing_anchor":
            body["turn_id"] = "missing"
        elif failure == "failed_anchor":
            conn.execute(
                "UPDATE project_task_turns SET status='failed' WHERE turn_id='source-turn-2'"
            )
        elif failure == "project":
            body["project_id"] = "foreign-project"
        elif failure == "expert":
            body["selected_expert_id"] = "foreign-expert"
        elif failure == "attachment":
            conn.execute(
                "UPDATE project_task_attachments SET content=? WHERE attachment_id='input_original'",
                (b"corrupt",),
            )
        elif failure == "artifact":
            conn.execute(
                "UPDATE project_task_artifacts SET content=? WHERE artifact_id='artifact_original'",
                (b"corrupt",),
            )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "project_tasks",
                "project_task_turns",
                "project_task_attachments",
                "project_task_artifacts",
                "project_task_branches",
            )
        }
    with pytest.raises(OctopError):
        branches.create(services, owner, "source", TaskFork.model_validate(body))
    with services.db.read_snapshot() as conn:
        assert {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before
        } == before


def test_forked_history_can_continue_and_source_remains_unchanged(services, monkeypatch):
    original = [asdict(row) for row in services.project_task_turn_repo.list("source")]
    created = branches.create(services, 1, "source", TaskFork(request_id="continue-fork"))
    monkeypatch.setattr(tasks, "_configuration", lambda *args, **kwargs: ("{}", "provider/model"))
    monkeypatch.setattr(
        "octop.infra.agents.providers.store.ProviderStore.is_model_ref_multimodal",
        lambda *_args: True,
    )
    result = tasks.send(
        services,
        1,
        tasks.TaskMessage(request_id="continue-message", prompt="Continue this work"),
        task_id=created.task_id,
    )
    assert result.turns[-1].prompt == "Continue this work" and not result.turns[-1].history_only
    assert result.active_turn_id == result.turns[-1].turn_id
    assert [asdict(row) for row in services.project_task_turn_repo.list("source")] == original


def test_branch_creation_does_not_allow_editing_a_history_copy(services):
    created = branches.create(services, 1, "source", TaskFork(request_id="readonly-fork"))
    copied = services.project_task_turn_repo.list(created.task_id)
    with pytest.raises(OctopError):
        branches.create(
            services,
            1,
            created.task_id,
            TaskFork(
                request_id="edit-copy",
                operation="edit",
                turn_id=copied[0].turn_id,
                prompt="Changed",
            ),
        )


def test_copied_history_and_bytes_survive_explicit_source_deletion(services):
    created = branches.create(services, 1, "source", TaskFork(request_id="preserve-copy"))
    copied = services.project_task_turn_repo.list(created.task_id)
    with services.db.transaction() as conn:
        services.project_task_repo.delete(
            conn, services.project_task_repo.get_in_transaction(conn, "source", 1)
        )
    assert services.project_task_repo.get("source", 1) is None
    assert services.project_task_turn_repo.list(created.task_id) == copied
    with services.db.read_snapshot() as conn:
        origin = services.project_task_branch_repo.origin(conn, created.task_id)
        assert origin.source_task_id == "source"
        assert services.project_task_attachment_repo.for_task(conn, 1, created.task_id)
        assert services.project_task_artifact_repo.for_turns(
            conn, created.task_id, [copied[0].turn_id]
        )
    assert branches.create(services, 1, "source", TaskFork(request_id="preserve-copy")) == created


async def test_worker_uses_copied_message_context_and_not_source_checkpoint(services, monkeypatch):
    created = branches.create(services, 1, "source", TaskFork(request_id="worker-context"))
    monkeypatch.setattr(
        tasks,
        "_configuration",
        lambda *args, **kwargs: ('{"action_timeline_enabled":false}', "provider/model"),
    )
    tasks.send(
        services,
        1,
        tasks.TaskMessage(request_id="worker-continue", prompt="Continue the copied work"),
        task_id=created.task_id,
    )
    seen = []

    async def execute(shared, owner, turn, history):
        seen.extend(history)
        yield {"type": "token", "content": "Continued answer"}

    monkeypatch.setattr(runtime, "run_turn", execute)
    worker = runtime.ProjectTaskWorker(services)
    claimed = services.project_task_turn_repo.claim("branch-worker", tasks.now_ms())
    assert claimed.task_id == created.task_id
    await worker._execute(claimed)
    assert [item["role"] for item in seen] == ["user", "assistant", "user", "assistant"]
    assert seen[0]["content"] == "Question 1" and seen[-1]["content"] == "Second answer"
    assert "Private checkpoint output" not in json.dumps(seen)
    done = services.project_task_turn_repo.get(claimed.turn_id)
    assert done.status == "completed" and done.reply == "Continued answer"


def test_operation_contract_rejects_ambiguous_edits_and_regeneration():
    with pytest.raises(ValidationError):
        TaskFork(request_id="edit-no-prompt", operation="edit")
    with pytest.raises(ValidationError):
        TaskFork(request_id="regen-new-prompt", operation="regenerate", prompt="New prompt")
