"""Engine admission, immutable turn identity and fenced native-session persistence."""
# ruff: noqa: F811

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from tests.unit.projects.test_expert_execution import services  # noqa: F401
from tests.unit.projects.test_unified_ingress import message

from octop.infra.agents.acp_setup import ACPSetupService
from octop.infra.agents.settings.acp import ACPSettingsStore
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_tasks import ProjectTaskRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import (
    engine_catalog,
    ingress,
    interactions,
    service,
    task_submission,
    tasks,
)
from octop.infra.projects.interaction_models import TaskControl
from octop.infra.projects.models import ProjectFields


def ready(services, monkeypatch, owner=1):
    services.secret_repo.get_or_create("jwt", lambda: b"engine-test-only-secret")
    store = ACPSettingsStore(settings_repo=services.settings_repo, agents_repo=services.agent_repo)
    store.save_runners(
        owner,
        {
            "native-test": {
                "command": "native-test",
                "enabled": True,
                "env": {"API_KEY": "secret-never-expose"},
            }
        },
    )
    selection = engine_catalog.EngineSelection(engine_id="acp", connection_id="native-test")
    store.save_setup_status(
        owner,
        "native-test",
        {
            "fingerprint": engine_catalog.connection_fingerprint(services, owner, selection),
            "connection": "ready",
            "auth_scope_version": 1,
            "engine_models": [{"model_id": "native-model", "label": "Native model"}],
            "engine_capabilities": {"images": False, "native_resume": True, "plugins": True},
        },
    )
    monkeypatch.setattr(
        ACPSetupService, "_launch", lambda self, runner, config: [config["command"]]
    )
    return selection, store


def finish(services, task):
    turn = services.project_task_turn_repo.claim("worker-one", tasks.now_ms())
    with services.db.transaction() as conn:
        assert services.project_task_turn_repo.finish(
            conn, turn.turn_id, "worker-one", "completed", "done", "", tasks.now_ms()
        )
        services.project_task_repo.finish(conn, task.task_id, turn.turn_id, tasks.now_ms())
    return tasks.detail(services, 1, task.task_id)


def test_catalog_is_passive_owner_scoped_and_intersects_protocol_capabilities(
    services, monkeypatch
):
    selection, store = ready(services, monkeypatch)
    monkeypatch.setattr(
        ACPSetupService, "_probe", lambda *args: pytest.fail("passive catalog spawned an agent")
    )
    entry = engine_catalog.validate_engine(services, 1, selection)
    assert not entry.capabilities.images and not entry.capabilities.plugins
    assert entry.models[0].model_id == "native-model"
    assert "secret-never-expose" not in entry.model_dump_json()
    with pytest.raises(OctopError):
        engine_catalog.validate_engine(services, 2, selection)
    store.save_runners(1, {"native-test": {"command": "changed", "enabled": True}})
    assert not next(
        item for item in engine_catalog.catalog(services, 1) if item.engine == selection
    ).available


def test_acp_needs_no_octop_provider_and_history_keeps_frozen_engine(services, monkeypatch):
    selection, _ = ready(services, monkeypatch)
    body = tasks.TaskCreate(request_id="native-create", prompt="hello", engine=selection)
    task = tasks.send(services, 1, body)
    assert task.engine == selection
    assert task.turns[0].model_ref == ""
    assert task.turns[0].execution_engine.connection_id == "native-test"
    assert "secret-never-expose" not in task.model_dump_json()
    with pytest.raises(OctopError) as error:
        tasks.patch(
            services,
            1,
            task.task_id,
            tasks.TaskPatch(
                expected_revision=task.list_revision,
                expected_configuration_revision=1,
                engine=engine_catalog.EngineSelection(),
            ),
        )
    assert error.value.code == ErrorCode.TASK_BUSY
    task = finish(services, task)
    message(services)
    task = tasks.patch(
        services,
        1,
        task.task_id,
        tasks.TaskPatch(
            expected_revision=task.list_revision,
            expected_configuration_revision=1,
            engine=engine_catalog.EngineSelection(),
        ),
    )
    assert task.configuration_revision == 2
    assert task.turns[0].execution_engine.engine_id == "acp"
    assert tasks.send(services, 1, body).task_id == task.task_id
    next_turn = tasks.send(
        services,
        1,
        tasks.TaskMessage(
            request_id="native-followup", prompt="again", expected_configuration_revision=2
        ),
        task_id=task.task_id,
    )
    assert next_turn.turns[-1].execution_engine.engine_id == "octop"
    assert next_turn.turns[0].execution_engine.engine_id == "acp"


def test_native_binding_is_fenced_durable_and_absent_from_public_sync(services, monkeypatch):
    selection, _ = ready(services, monkeypatch)
    task = tasks.send(
        services, 1, tasks.TaskCreate(request_id="native-binding", prompt="hello", engine=selection)
    )
    repo = services.project_task_repo
    turn = services.project_task_turn_repo.claim("worker-one", tasks.now_ms())
    binding = {"session_id": "private-native-session", "last_completed_turn_id": None}
    assert not repo.save_engine_session(
        2, task.task_id, turn.turn_id, "worker-one", "scope", binding
    )
    assert not repo.save_engine_session(1, task.task_id, turn.turn_id, "stale", "scope", binding)
    assert repo.save_engine_session(1, task.task_id, turn.turn_id, "worker-one", "scope", binding)
    reopened = SqlitePool(services.paths.root / "control.sqlite")
    try:
        assert (
            ProjectTaskRepo(reopened).load_engine_session(
                1, task.task_id, turn.turn_id, "worker-one", "scope"
            )
            == binding
        )
    finally:
        reopened.close()
    with services.db.transaction() as conn:
        row = repo.get_in_transaction(conn, task.task_id, 1)
        repo.update_metadata(conn, replace(row, title="new title"), row.list_revision)
        payloads = conn.execute("SELECT payload_json FROM project_sync_changes").fetchall()
        conn.execute(
            "UPDATE project_task_turns SET worker_id='replacement' WHERE turn_id=?", (turn.turn_id,)
        )
    assert all("private-native-session" not in row["payload_json"] for row in payloads)
    assert "private-native-session" not in tasks.detail(services, 1, task.task_id).model_dump_json()
    assert repo.load_engine_session(1, task.task_id, turn.turn_id, "worker-one", "scope") is None
    assert not repo.save_engine_session(1, task.task_id, turn.turn_id, "worker-one", "scope", {})


@pytest.mark.parametrize("paused", [True, False])
def test_acp_pause_and_resume_do_not_replay_native_work(services, monkeypatch, paused):
    selection, _ = ready(services, monkeypatch)
    task = tasks.send(
        services, 1, tasks.TaskCreate(request_id="native-control", prompt="hello", engine=selection)
    )
    with pytest.raises(OctopError) as error:
        interactions.control(
            services,
            1,
            task.task_id,
            TaskControl(request_id="native-control-one", turn_id=task.active_turn_id),
            paused=paused,
        )
    assert error.value.code == ErrorCode.TASK_CONTROL_INVALID


async def test_acp_does_not_invoke_octop_routing_classifier(services, monkeypatch):
    selection, _ = ready(services, monkeypatch)

    async def unexpected(*args):
        pytest.fail("ACP invoked the Octop classifier")

    monkeypatch.setattr(task_submission, "classify_difficulty", unexpected)
    with pytest.raises(OctopError) as error:
        await task_submission.submit(
            services,
            1,
            tasks.TaskCreate(
                request_id="native-routing",
                prompt="hello",
                engine=selection,
                options={"routing": {"mode": "box"}},
            ),
        )
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID


async def test_im_cannot_inherit_logged_in_native_engine_and_admission_rolls_back(
    services, monkeypatch
):
    selection, _ = ready(services, monkeypatch)
    with pytest.raises(OctopError) as error:
        await ingress.submit(
            services,
            1,
            ingress.SourceEnvelope(
                source_kind="im",
                source_instance="channel-one",
                source_key="conversation",
                event_id="native-unverified",
            ),
            tasks.TaskCreate(request_id="native-unverified", prompt="work", engine=selection),
        )
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_task_turns").fetchone()[0] == 0


def test_native_compaction_is_rejected_before_work_is_queued(services, monkeypatch):
    selection, _ = ready(services, monkeypatch)
    with pytest.raises(OctopError) as error:
        tasks.send(
            services,
            1,
            tasks.TaskCreate(request_id="native-compact", prompt="/compact", engine=selection),
        )
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0


def test_acp_cannot_reinterpret_client_directory_as_host_workspace(services, monkeypatch, tmp_path):
    selection, _ = ready(services, monkeypatch)
    project = service.create_project(
        services,
        1,
        ProjectFields(name="Client work", client_directory=str(tmp_path / "client-only")),
        "native-client-project",
    )
    with pytest.raises(OctopError) as error:
        tasks.send(
            services,
            1,
            tasks.TaskCreate(
                request_id="native-client-directory",
                prompt="work",
                project_id=project.project_id,
                engine=selection,
            ),
            project_id=project.project_id,
        )
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID
    assert error.value.details["capability"] == "client_directory"


def test_acp_rejects_expert_tool_policy_before_queueing_native_work(services, monkeypatch):
    selection, _ = ready(services, monkeypatch)
    expert = services.agent_repo.get("expert-one")
    configuration = json.loads(expert.config_json)
    configuration["tools_disabled"] = ["execute"]
    services.agent_repo.update_config("expert-one", config_json=json.dumps(configuration))
    with pytest.raises(OctopError) as error:
        tasks.send(
            services,
            1,
            tasks.TaskCreate(
                request_id="native-expert-disabled-tool",
                prompt="work",
                engine=selection,
                selected_expert_id="expert-one",
            ),
        )
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID
    assert error.value.details["capability"] == "tool_policy"
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_task_turns").fetchone()[0] == 0
