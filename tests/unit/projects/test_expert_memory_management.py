"""Human memory maintenance respects identity, scope and idempotent writes."""

from __future__ import annotations

import json

import pytest
from octop_memory import MemoryService
from octop_memory.core import Memory
from pydantic import ValidationError
from tests.unit.projects.test_expert_execution import services  # noqa: F401

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.expert_memory_management import (
    ExpertMemoryPolicyPatch,
    ExpertMemoryQuery,
    ExpertMemorySave,
    memory_policy,
    query_memory,
    save_memory,
    update_memory_policy,
)
from octop.infra.projects.expert_profiles import resolve_expert_profile


@pytest.fixture
def memory_tasks(services):  # noqa: F811
    services.agent_repo.create(
        agent_id="expert-two",
        user_id=1,
        name="Other expert",
        config_json=json.dumps({"workspace_dir": str(services.paths.root / "expert-two")}),
    )
    with services.db.transaction() as conn:
        for project in ("project-one", "project-two"):
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES(?,1,?,1,1)",
                (project, project),
            )
        for task, project, expert in (
            ("task-one", "project-one", "expert-one"),
            ("task-same-project", "project-one", "expert-one"),
            ("task-other-project", "project-two", "expert-one"),
            ("task-other-expert", "project-one", "expert-two"),
            ("task-alone-one", None, "expert-one"),
            ("task-alone-two", None, "expert-one"),
        ):
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,selected_expert_id,created_at,updated_at) VALUES(?,1,?,'Memory',?,?,1,1)",
                (task, project, task, expert),
            )
    return services


def save(
    bundle,
    *,
    source="general",
    request="save-general-one",
    task="task-one",
    content="I prefer blueberry financial examples.",
    **fields,
):
    return save_memory(
        bundle,
        1,
        task,
        ExpertMemorySave(source=source, request_id=request, content=content, **fields),
    )


def query(bundle, task="task-one", source="general", **fields):
    return query_memory(bundle, 1, task, ExpertMemoryQuery(source=source, **fields))


async def test_explicit_general_fact_is_canonical_and_recalled_across_projects(memory_tasks):
    result = save(memory_tasks)
    assert result.status == "completed" and result.replayed is False
    assert result.entry.path.startswith("atom/")
    entries = query(memory_tasks, "task-other-project", query="blueberry").entries
    assert any("blueberry" in item.content for item in entries)
    assert query(memory_tasks, source="scope").entries == []
    full = query(memory_tasks, "task-other-project", path=result.entry.path)
    assert "blueberry" in full.entries[0].content
    profile = resolve_expert_profile(memory_tasks, 1, "expert-one")
    memory = ExpertMemory(
        memory_tasks,
        1,
        "task-other-project",
        "recall-human-fact",
        "project-two",
        profile,
        enabled=None,
    )
    assert "blueberry" in await memory.prompt("blueberry examples")


def test_scope_facts_do_not_escape_project_or_standalone_task(memory_tasks):
    project_fact = save(
        memory_tasks,
        source="scope",
        request="save-project-one",
        content="Mercury project budget is 123456 yuan.",
    )
    assert query(memory_tasks, "task-same-project", "scope", query="Mercury").entries
    assert query(memory_tasks, "task-other-project", "scope", query="Mercury").entries == []
    with pytest.raises(OctopError) as wrong_scope:
        query(memory_tasks, "task-other-project", "scope", path=project_fact.entry.path)
    assert wrong_scope.value.code == ErrorCode.NOT_FOUND
    save(
        memory_tasks,
        source="scope",
        request="save-alone-one",
        task="task-alone-one",
        content="Neptune work budget is 987654 yuan.",
    )
    assert query(memory_tasks, "task-alone-one", "scope", query="Neptune").entries
    assert query(memory_tasks, "task-alone-two", "scope", query="Neptune").entries == []


def test_owner_and_expert_isolation_apply_to_management_and_known_paths(memory_tasks):
    saved = save(memory_tasks)
    assert query(memory_tasks, "task-other-expert", query="blueberry").entries == []
    with pytest.raises(OctopError) as unknown_path:
        query(memory_tasks, "task-other-expert", path=saved.entry.path)
    assert unknown_path.value.code == ErrorCode.NOT_FOUND
    for operation in (
        lambda: query_memory(memory_tasks, 2, "task-one", ExpertMemoryQuery()),
        lambda: save_memory(
            memory_tasks,
            2,
            "task-one",
            ExpertMemorySave(
                source="general", request_id="other-user-save", content="Do not store this."
            ),
        ),
        lambda: memory_policy(memory_tasks, 2, "task-one"),
    ):
        with pytest.raises(OctopError) as denied:
            operation()
        assert denied.value.code == ErrorCode.NOT_FOUND


def test_manual_save_request_retries_replay_result_and_reject_different_content(memory_tasks):
    first = save(memory_tasks)
    second = save(memory_tasks)
    assert second.replayed is True and second.entry == first.entry
    assert len([entry for entry in query(memory_tasks).entries if entry.layer == "atom"]) == 1
    with pytest.raises(OctopError) as conflict:
        save(memory_tasks, content="A different assertion under the same request.")
    assert conflict.value.code == ErrorCode.TASK_REQUEST_CONFLICT


def test_unknown_save_outcome_is_not_automatically_replayed(memory_tasks, monkeypatch):
    original = MemoryService.save
    attempts = []

    def lost_receipt(service, *args, **kwargs):
        attempts.append(True)
        original(service, *args, **kwargs)
        raise ConnectionError("simulated lost response after committed save")

    monkeypatch.setattr(MemoryService, "save", lost_receipt)
    first = save(memory_tasks)
    second = save(memory_tasks)
    assert first.status == second.status == "uncertain"
    assert first.replayed is False and second.replayed is True
    assert len(attempts) == 1
    assert len([entry for entry in query(memory_tasks).entries if entry.layer == "atom"]) == 1


def test_legacy_requires_explicit_policy_and_is_always_read_only(memory_tasks):
    original = Memory(
        "agent_expert-one",
        backend_config={"db_path": str(memory_tasks.paths.root / "expert" / "memory.sqlite")},
    )
    MemoryService(original).save("Legacy violet preference.")
    original.backend.close()
    policy = memory_policy(memory_tasks, 1, "task-one")
    assert policy.legacy_recall_enabled is False
    with pytest.raises(OctopError) as denied:
        query(memory_tasks, source="legacy")
    assert denied.value.code == ErrorCode.TASK_RESOURCE_UNAVAILABLE
    updated = update_memory_policy(
        memory_tasks,
        1,
        "task-one",
        ExpertMemoryPolicyPatch(
            expected_revision=policy.config_revision, legacy_recall_enabled=True
        ),
    )
    assert updated.legacy_recall_enabled is True
    assert updated.config_revision == policy.config_revision + 1
    assert query(memory_tasks, source="legacy", query="violet").entries
    with pytest.raises(ValidationError):
        ExpertMemorySave(
            source="legacy", request_id="legacy-denied-write", content="Must not write legacy."
        )
    assert query(memory_tasks, source="general", query="violet").entries == []
    with pytest.raises(OctopError) as stale:
        update_memory_policy(
            memory_tasks,
            1,
            "task-one",
            ExpertMemoryPolicyPatch(
                expected_revision=policy.config_revision, legacy_recall_enabled=False
            ),
        )
    assert stale.value.code == ErrorCode.TASK_REVISION_CONFLICT


def test_policy_capture_switch_preserves_configuration_and_stale_panel_cannot_write(memory_tasks):
    before = memory_tasks.agent_repo.get("expert-one")
    policy = memory_policy(memory_tasks, 1, "task-one")
    updated = update_memory_policy(
        memory_tasks,
        1,
        "task-one",
        ExpertMemoryPolicyPatch(
            expected_revision=policy.config_revision,
            legacy_recall_enabled=False,
            capture_enabled=False,
            expected_expert_id="expert-one",
        ),
    )
    assert updated.capture_enabled is False
    after = memory_tasks.agent_repo.get("expert-one")
    assert after.system_prompt == before.system_prompt
    assert (
        json.loads(after.config_json)["workspace_dir"]
        == json.loads(before.config_json)["workspace_dir"]
    )
    with pytest.raises(OctopError) as stale:
        save(memory_tasks, expected_expert_id="expert-two")
    assert stale.value.code == ErrorCode.TASK_REVISION_CONFLICT
