"""Expert identity, memory scope and read-only mode remain server owned."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from langgraph.prebuilt.tool_node import ToolCallRequest
from octop_memory.core import Memory

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import OctopError
from octop.infra.projects import runtime
from octop.infra.projects import tasks as task_records
from octop.infra.projects.expert_memory import ExpertMemory, memory_namespace
from octop.infra.projects.expert_profiles import (
    ExpertAccessMiddleware,
    _workspace,
    assert_profile_access,
    resolve_expert_profile,
    runtime_plugin_configuration,
    validate_expert_workspace_skills,
)
from octop.infra.projects.models import ProjectFields
from octop.infra.skills.skill_package_store import SkillPackageStore
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "control.sqlite")
    run_migrations(db)
    repos = RepoBundle.from_pool(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(2,'another','user',1)")
    result = SharedServices(PathLayout(tmp_path), OctopConfig(), repos)
    config = {"workspace_dir": str(tmp_path / "expert"), "memory": {"capture_enabled": True}}
    repos.agent_repo.create(
        agent_id="expert-one",
        user_id=1,
        name="Accountant",
        system_prompt="Financial method",
        config_json=json.dumps(config),
    )
    workspace = _workspace(result, repos.agent_repo.get("expert-one"), config)
    workspace.upload_bytes("SOUL.md", b"Expert identity")
    workspace.upload_bytes("USER.md", b"Personal preferences")
    workspace.upload_bytes("skills/local-method/SKILL.md", b"Local method")
    workspace.upload_bytes("skills/local-method/reference.txt", b"Reference content")
    try:
        yield result
    finally:
        db.close()


def profile(services):
    return resolve_expert_profile(services, 1, "expert-one")


@pytest.mark.parametrize("mutation", [None, "project", "expert"])
async def test_slow_expert_snapshot_releases_database_and_rechecks_versions(
    services, monkeypatch, mutation
):
    from octop.infra.projects import expert_profiles, service

    services.provider_repo.create(
        name="test",
        kind="openai",
        base_url="http://test.invalid/v1",
        api_key="test-key",
        models_json=json.dumps([{"id": "model", "enabled": True}]),
    )
    services.secret_repo.get_or_create("jwt", lambda: b"test-only-snapshot-secret")
    project = service.create_project(services, 1, ProjectFields(name="Project"), "project-request")
    entered, release = threading.Event(), threading.Event()
    original = expert_profiles._workspace

    class SlowWorkspace:
        def __init__(self, workspace):
            self.workspace = workspace

        def read_text(self, *args, **kwargs):
            entered.set()
            assert release.wait(5), "snapshot test did not release content read"
            return self.workspace.read_text(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.workspace, name)

    monkeypatch.setattr(
        expert_profiles,
        "_workspace",
        lambda *args: SlowWorkspace(original(*args)),
    )
    body = task_records.ProjectTaskCreate(
        request_id="snapshot-request",
        prompt="Work",
        selected_expert_id="expert-one",
        options=task_records.TaskExecutionOptions(model_ref="test/model"),
    )
    pending = asyncio.create_task(
        asyncio.to_thread(task_records.send, services, 1, body, project_id=project.project_id)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # An independent control-plane write must settle while content I/O is blocked.
        await asyncio.wait_for(
            asyncio.to_thread(services.settings_repo.set, "snapshot-probe", "saved"), 5
        )
        assert not pending.done()
        if mutation == "project":
            await asyncio.to_thread(
                services.project_repo.update,
                replace(services.project_repo.get(project.project_id, 1), instructions="Changed"),
                project.revision,
            )
        elif mutation == "expert":
            await asyncio.to_thread(
                services.agent_repo.update_config, "expert-one", system_prompt="Changed"
            )
    finally:
        release.set()
    if mutation:
        with pytest.raises(OctopError) as error:
            await pending
        assert error.value.code.value == (
            "PROJECT_REVISION_CONFLICT" if mutation == "project" else "TASK_REVISION_CONFLICT"
        )
        with services.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0
    else:
        result = await pending
        saved = json.loads(
            services.project_task_turn_repo.list(result.task_id)[0].configuration_json
        )
        assert saved["expert_profile"]["system_prompt"] == "Financial method"
        # A replay uses its accepted receipt even if source content is now unavailable.
        monkeypatch.setattr(
            expert_profiles, "_workspace", lambda *args: pytest.fail("replayed snapshot")
        )
        replay = await asyncio.to_thread(
            task_records.send, services, 1, body, project_id=project.project_id
        )
        assert replay.task_id == result.task_id


def test_profile_reads_content_hashes_without_adopting_expert_directory(services):
    snapshot = profile(services)
    assert snapshot["system_prompt"] == "Financial method"
    assert {item["path"] for item in snapshot["profile_files"]} == {"SOUL.md", "USER.md"}
    assert snapshot["workspace_skills"][0]["slug"] == "local-method"
    assert len(snapshot["workspace_skills"][0]["files"]) == 2
    assert "workspace_dir" not in snapshot
    assert "backend" not in snapshot["memory"]
    row = services.agent_repo.get("expert-one")
    workspace = _workspace(services, row, json.loads(row.config_json))
    workspace.upload_bytes("SOUL.md", b"Changed identity")
    assert profile(services)["profile_digest"] != snapshot["profile_digest"]


def test_plugin_parameters_are_bound_by_digest_without_persisting_secrets(services):
    row = services.agent_repo.get("expert-one")
    config = json.loads(row.config_json)
    config["plugins"] = {
        "proof": {
            "tools": {
                "configured": {
                    "config": {"api_key": "private-plugin-secret", "prefix": "expert value"}
                }
            }
        }
    }
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))
    snapshot = profile(services)
    assert "private-plugin-secret" not in json.dumps(snapshot)
    assert runtime_plugin_configuration(services, 1, snapshot) == config["plugins"]
    config["plugins"]["proof"]["tools"]["configured"]["config"]["prefix"] = "changed value"
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))
    with pytest.raises(OctopError):
        runtime_plugin_configuration(services, 1, snapshot)


def test_workspace_skill_collision_requires_explicit_override_or_matching_package_content(services):
    snapshot = profile(services)
    selected = ProjectFields(
        name="Project", skills=[{"package_id": "another-package", "slug": "local-method"}]
    ).skills
    with pytest.raises(OctopError):
        validate_expert_workspace_skills(snapshot, selected, inherit_workspace=True)
    validate_expert_workspace_skills(snapshot, selected, inherit_workspace=False)
    store = SkillPackageStore(
        repo=services.skill_package_repo, root=services.paths.skill_packages_dir
    )
    package = store.create(name="Matching package", description="", created_by="1")
    skill_root = store.package_skills_dir(package.id) / "local-method"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_bytes(b"Local method")
    (skill_root / "reference.txt").write_bytes(b"Reference content")
    services.agent_repo.update_config("expert-one", skill_package_ids=json.dumps([package.id]))
    same = profile(services)
    assert same["workspace_skills"] == []
    (skill_root / "SKILL.md").write_bytes(b"A different method")
    with pytest.raises(OctopError):
        profile(services)


@pytest.mark.parametrize("mutation", ["owner", "team", "resident", "disabled"])
def test_unavailable_experts_and_invalid_teams_are_rejected(services, mutation):
    with services.db.transaction() as conn:
        if mutation == "resident":
            conn.execute(
                "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,last_active,created_at) VALUES('resident-thread','expert-one',1,'dashboard','resident',1,1)"
            )
            services.resident_conversation_repo.reserve_in_transaction(
                conn, 1, "expert-one", "resident-thread", 1
            )
        else:
            column, value = {
                "owner": ("user_id", 2),
                "team": ("kind", "team"),
                "disabled": ("enabled", 0),
            }[mutation]
            conn.execute(f"UPDATE agents SET {column}=? WHERE agent_id='expert-one'", (value,))
    with pytest.raises(OctopError):
        profile(services)


def test_shared_expert_keeps_public_identity_but_excludes_owner_private_settings(services):
    row = services.agent_repo.get("expert-one")
    config = json.loads(row.config_json)
    config.update(
        {
            "memory_files": ["private.md"],
            "plugins": {"proof": {"token": "secret"}},
            "memory": {
                "legacy_recall_enabled": True,
                "extract_on_session_end": False,
                "general_promotion_enabled": False,
            },
        }
    )
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))
    services.agent_repo.set_shared("expert-one", True)
    workspace = _workspace(services, services.agent_repo.get("expert-one"), config)
    workspace.upload_bytes("private.md", b"Owner's private memory")
    shared = resolve_expert_profile(services, 2, "expert-one")
    assert shared["is_shared"] and shared["owner_id"] == 1 and shared["kind"] == "expert"
    assert {item["path"] for item in shared["profile_files"]} == {"SOUL.md"}
    assert "secret" not in json.dumps(shared)
    assert runtime_plugin_configuration(services, 2, shared) == {}
    assert not shared["memory"]["legacy_recall_enabled"]
    assert not shared["memory"]["auto_extract_enabled"]
    assert not shared["memory"]["general_promotion_enabled"]
    assert "USER.md" in {item["path"] for item in profile(services)["profile_files"]}


def test_team_snapshot_preserves_member_profiles_and_rechecks_shared_access(services):
    from octop.infra.projects import tasks

    services.agent_repo.create(agent_id="second", user_id=2, name="Second")
    services.agent_repo.set_shared("second", True)
    services.agent_repo.create(agent_id="host", user_id=1, name="Team", kind="team")
    row = services.agent_repo.get("host")
    workspace = _workspace(services, row, {})
    workspace.upload_bytes(".octop/manifest.json", b'{"members":["expert-one","second"]}')
    team = resolve_expert_profile(services, 1, "host")
    assert team["team"]["member_ids"] == ["expert-one", "second"]
    assert [item["name"] for item in team["team"]["members"]] == ["Accountant", "Second"]
    option = next(item for item in tasks.resources(services, 1).experts if item.expert_id == "host")
    assert option.kind == "team" and option.is_owner and not option.is_shared
    assert option.member_ids == ["expert-one", "second"]
    configuration = tasks._base_configuration(
        services, 1, None, tasks.TaskExecutionOptions(team_enabled=False), "host"
    )
    assert configuration["team_enabled"] is True and configuration["subagents"] == []
    assert configuration["project_defaults"]["team_enabled"] is False
    services.agent_repo.set_shared("second", False)
    with pytest.raises(OctopError):
        assert_profile_access(services, 1, team)
    with pytest.raises(OctopError):
        resolve_expert_profile(services, 1, "host")
    assert "host" not in {item.expert_id for item in tasks.resources(services, 1).experts}


@pytest.mark.parametrize("mutation", ["unshared", "disabled", "resident"])
async def test_frozen_shared_profile_rechecks_access_before_model_and_tool(services, mutation):
    services.agent_repo.set_shared("expert-one", True)
    frozen = resolve_expert_profile(services, 2, "expert-one")
    middleware = ExpertAccessMiddleware(services, 2, frozen)
    handler = AsyncMock(return_value="accepted")
    assert await middleware.awrap_model_call(None, handler) == "accepted"
    if mutation == "unshared":
        services.agent_repo.set_shared("expert-one", False)
    else:
        with services.db.transaction() as conn:
            if mutation == "disabled":
                conn.execute("UPDATE agents SET enabled=0 WHERE agent_id='expert-one'")
            else:
                conn.execute(
                    "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,last_active,created_at) VALUES('resident-thread','expert-one',1,'dashboard','resident',1,1)"
                )
                services.resident_conversation_repo.reserve_in_transaction(
                    conn, 1, "expert-one", "resident-thread", 1
                )
    with pytest.raises(OctopError):
        await middleware.awrap_tool_call(None, handler)
    with pytest.raises(OctopError):
        await middleware.awrap_model_call(None, handler)
    handler.assert_awaited_once()


def expert_memory(
    services, task="task-one", project="project-one", turn="turn-one", snapshot=None, enabled=None
):
    return ExpertMemory(
        services, 1, task, turn, project, snapshot or profile(services), enabled=enabled
    )


async def test_capture_is_project_scoped_reuses_original_storage_and_does_not_duplicate(services):
    first = expert_memory(services)
    await first.capture(
        "Please remember that the Mercury project budget is 123456 yuan.",
        "The Mercury budget is recorded as 123456 yuan.",
    )
    result = await asyncio.to_thread(first._call, "scope", "search", query="Mercury")
    assert result["hits"]
    count = len(result["hits"])
    await first.capture(
        "Please remember a second different Mercury value.",
        "Replacement must not overwrite history.",
    )
    assert (
        len((await asyncio.to_thread(first._call, "scope", "search", query="Mercury"))["hits"])
        == count
    )
    same_project = expert_memory(services, task="task-two", turn="turn-two")
    assert (await asyncio.to_thread(same_project._call, "scope", "search", query="Mercury"))["hits"]
    other_project = expert_memory(services, project="project-two")
    assert not (await asyncio.to_thread(other_project._call, "scope", "search", query="Mercury"))[
        "hits"
    ]
    assert not (await asyncio.to_thread(first._call, "general", "search", query="Mercury"))["hits"]
    assert (services.paths.root / "expert" / "memory.sqlite").exists()
    assert not (services.paths.root / "project-tasks" / "task-one" / "memory.sqlite").exists()
    journal = first.journal_path("capture")
    assert json.loads(journal.read_text())["status"] == "completed"


async def test_legacy_is_opt_in_read_only_and_disabled_expert_memory_opens_nothing(services):
    original = Memory(
        "agent_expert-one",
        backend_config={"db_path": str(services.paths.root / "expert" / "memory.sqlite")},
    )
    original.add_raw("Please remember the legacy Neptune project fact.", event_type="user_message")
    original.backend.close()
    first = expert_memory(services)
    with pytest.raises(ValueError):
        first._call("legacy", "search", query="Neptune")
    snapshot = profile(services)
    snapshot["memory"]["legacy_recall_enabled"] = True
    row = services.agent_repo.get("expert-one")
    config = json.loads(row.config_json)
    config["memory"]["legacy_recall_enabled"] = True
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))
    first = expert_memory(services, snapshot=snapshot)
    assert first._call("legacy", "search", query="Neptune")["hits"]
    off = expert_memory(services, enabled=False)
    assert off.tools("en") == []
    assert await off.prompt("Neptune") == ""
    assert memory_namespace(1, "a:b", "project", "c") != memory_namespace(1, "a", "project", "b:c")
    assert memory_namespace(1, "expert-one", "project", "x") != memory_namespace(
        2, "expert-one", "project", "x"
    )


async def test_resumption_reuses_original_recall_text_and_rechecks_owner(services):
    first = expert_memory(services)
    initial = await first.prompt("Mercury project")
    another = expert_memory(services, turn="another-turn")
    await another.capture(
        "Please remember the Mercury project budget is 987654 yuan.",
        "I remember Mercury's 987654 yuan budget.",
    )
    assert await first.prompt("Mercury project") == initial
    fresh = expert_memory(services, turn="fresh-turn")
    assert await fresh.prompt("Mercury project") != initial
    with services.db.transaction() as conn:
        conn.execute("UPDATE agents SET user_id=2 WHERE agent_id='expert-one'")
    with pytest.raises(OctopError):
        await first.prompt("Mercury project")


@pytest.mark.parametrize("mode", ["ask", "plan"])
@pytest.mark.parametrize(
    "name",
    ["write_file", "edit_file", "execute", "client__read_note", "mcp_write", "create_artifact"],
)
async def test_restricted_modes_deny_dispatch_even_for_client_and_unknown_tools(mode, name):
    gate = runtime.TaskActionModeMiddleware(mode, "en", set())
    handler = AsyncMock()
    request = ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "call", "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )
    result = await gate.awrap_tool_call(request, handler)
    assert result.status == "error"
    handler.assert_not_called()


@pytest.mark.parametrize("cancel_one", [False, True])
async def test_worker_runs_other_tasks_while_one_is_blocked_and_uses_individual_leases(
    services, monkeypatch, cancel_one
):
    entered = []
    releases = {f"turn-{index}": asyncio.Event() for index in range(5)}
    with services.db.transaction() as conn:
        for index in range(5):
            task_id, turn_id = f"task-{index}", f"turn-{index}"
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,created_at,updated_at) VALUES(?,1,'Work',?,?,1,1)",
                (task_id, task_id, turn_id),
            )
            services.project_task_turn_repo.insert(
                conn,
                ProjectTaskTurnRow(
                    turn_id=turn_id,
                    task_id=task_id,
                    request_id=turn_id,
                    prompt="Work",
                    reply="",
                    status="queued",
                    error_code="",
                    activity="",
                    configuration_json=json.dumps({"action_timeline_enabled": False}),
                    model_ref="provider/model",
                    worker_id="",
                    cancel_requested=False,
                    created_at=1,
                    updated_at=1,
                ),
            )

    async def run(services, owner, turn, history):
        entered.append(turn.turn_id)
        yield {"type": "token", "content": f"Started {turn.turn_id}. "}
        await releases[turn.turn_id].wait()
        yield {"type": "token", "content": "Finished."}

    monkeypatch.setattr(runtime, "run_turn", run)
    worker = runtime.ProjectTaskWorker(services)
    loop = asyncio.create_task(worker._loop())
    try:
        async with asyncio.timeout(5):
            while len(entered) < 4:
                await asyncio.sleep(0.01)
        assert len(entered) == 4
        rows = [services.project_task_turn_repo.get(turn_id) for turn_id in entered]
        assert len({row.worker_id for row in rows}) == 4
        cancelled = entered[1] if cancel_one else None
        if cancel_one:
            with services.db.transaction() as conn:
                conn.execute(
                    "UPDATE project_task_turns SET cancel_requested=1 WHERE turn_id=?", (cancelled,)
                )
        else:
            releases[entered[1]].set()
        async with asyncio.timeout(5):
            while len(entered) < 5:
                await asyncio.sleep(0.01)
        assert not releases[entered[0]].is_set()
        for event in releases.values():
            event.set()
        async with asyncio.timeout(5):
            while worker._running:
                await asyncio.sleep(0.01)
        for index in range(5):
            row = services.project_task_turn_repo.get(f"turn-{index}")
            assert row.status == ("cancelled" if row.turn_id == cancelled else "completed")
            assert row.reply == f"Started turn-{index}. " + (
                "" if row.turn_id == cancelled else "Finished."
            )
    finally:
        worker.closing = True
        worker._wake.set()
        for event in releases.values():
            event.set()
        await loop


@pytest.mark.parametrize("close_during_resume", [False, True])
async def test_worker_drains_previous_execution_before_reusing_a_resumed_turn_key(
    services, monkeypatch, close_during_resume
):
    current_loop = asyncio.get_running_loop()
    first_parked, second_claimed, release_first, second_started, release_second = [
        asyncio.Event() for _ in range(5)
    ]
    original = ProjectTaskTurnRow(
        turn_id="reused-turn",
        task_id="reused-task",
        request_id="reused-request",
        prompt="Work",
        reply="",
        status="running",
        error_code="",
        activity="",
        configuration_json="{}",
        model_ref="model",
        worker_id="first-lease",
        checkpoint_id="saved-checkpoint" if close_during_resume else "",
        cancel_requested=False,
        created_at=1,
        updated_at=1,
    )
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,created_at,updated_at) VALUES('reused-task',1,'Work','reused-task','reused-turn',1,1)"
        )
        services.project_task_turn_repo.insert(conn, original)
    claims = []

    def claim(*_args):
        if not claims:
            claims.append(True)
            with services.db.transaction() as conn:
                conn.execute(
                    "UPDATE project_task_turns SET status='running',worker_id='first-lease' WHERE turn_id='reused-turn'"
                )
            return original
        if len(claims) == 1 and first_parked.is_set():
            claims.append(True)
            with services.db.transaction() as conn:
                conn.execute(
                    "UPDATE project_task_turns SET worker_id='second-lease' WHERE turn_id='reused-turn'"
                )
            current_loop.call_soon_threadsafe(second_claimed.set)
            return replace(original, worker_id="second-lease")
        return None

    worker = runtime.ProjectTaskWorker(services)

    async def execute(turn):
        if turn.worker_id == "first-lease":
            first_parked.set()
            worker.wake()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()

    monkeypatch.setattr(services.project_task_turn_repo, "claim", claim)
    monkeypatch.setattr(worker, "_recover", lambda: None)
    monkeypatch.setattr(worker, "_execute", execute)
    running = asyncio.create_task(worker._loop())
    worker._loop_task = running
    try:
        await asyncio.wait_for(second_claimed.wait(), 5)
        assert not second_started.is_set()
        first_execution = worker._running[original.turn_id]
        if close_during_resume:
            await asyncio.wait_for(worker.close(), 5)
            assert not second_started.is_set()
            parked = services.project_task_turn_repo.get(original.turn_id)
            assert parked.worker_id == "" and parked.wait_kind == "interrupted"
            assert json.loads(parked.recovery_json)["mode"] == "wait"
            assert parked.checkpoint_id == "saved-checkpoint"
            assert json.loads(parked.recovery_json)["can_resume"] is True
        else:
            release_first.set()
            await asyncio.wait_for(second_started.wait(), 5)
            assert worker._running[original.turn_id] is not first_execution
            assert not worker._running[original.turn_id].done()
    finally:
        release_first.set()
        release_second.set()
        worker.closing = True
        worker._wake.set()
        await running
