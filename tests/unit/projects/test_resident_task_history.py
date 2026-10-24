"""Existing private conversation history is imported before native Task admission."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, message_to_dict

from octop.config import OctopConfig
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.history.projection import message_inputs
from octop.infra.history.service import HistoryArchive
from octop.infra.history.store import HistoryStore
from octop.infra.projects import resident_tasks
from octop.infra.utils.paths import PathLayout


@pytest.fixture
async def private_history(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    owner = services.user_repo.create(username="owner", password_hash="unused", role="user")
    binding = await ensure_resident_identity(services, owner)
    messages = []
    for index in range(13):
        messages.extend(
            [
                HumanMessage(content=f"Previous question {index}", id=f"user-{index}"),
                AIMessage(content=f"Previous answer {index}", id=f"assistant-{index}"),
            ]
        )
    services.thread_message_repo.append_legacy_interval(binding.thread_id, message_inputs(messages))
    services.thread_message_repo.mark_projection(binding.thread_id, "ready")
    try:
        yield services, binding, messages
    finally:
        db.close()


def _source_rows(services):
    with services.db.read_snapshot() as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("agents", "threads", "thread_messages", "resident_requests")
        }


@pytest.mark.parametrize("source", ["projection", "archive"])
async def test_ensure_task_seeds_existing_history_atomically_without_changing_source(
    private_history, source
):
    services, binding, messages = private_history
    if source == "archive":
        archive = HistoryArchive(
            HistoryStore(
                services.paths.root / "history_v2.sqlite", identity=str(services.paths.db.resolve())
            ),
            services.thread_message_repo,
            services.trajectory_event_repo,
            enabled=True,
        )
        turn = archive.begin(binding.agent_id, binding.thread_id)
        recent = [
            HumanMessage(content="Archived question", id="archived-user"),
            AIMessage(content="Archived answer", id="archived-assistant"),
        ]
        archive.save_messages(turn, [message_to_dict(message) for message in recent])
        archive.finish(turn["id"], "completed")
        archive.store.close()
        messages.extend(recent)
    before = _source_rows(services)
    identifier = await resident_tasks.ensure_task(
        services, binding.owner_id, binding.thread_id, "new-send"
    )
    seed = services.project_task_turn_repo.list(identifier)
    assert len(seed) == 1 and seed[0].status == "completed"
    assert json.loads(seed[0].configuration_json)["execution_kind"] == "resident_history"
    context = services.project_task_turn_repo.context(identifier)
    assert context is not None
    assert json.loads(context[1]) == [message_to_dict(message) for message in messages]
    assert _source_rows(services) == before
    assert services.project_task_repo.get(identifier, binding.owner_id) is None
    assert (
        services.project_task_repo.get_internal(identifier, binding.owner_id).internal_kind
        == "resident"
    )
    assert (
        await resident_tasks.ensure_task(
            services, binding.owner_id, binding.thread_id, "another-send"
        )
        == identifier
    )
    assert len(services.project_task_turn_repo.list(identifier)) == 1
    with services.db.transaction() as conn:
        assert not services.project_task_turn_repo.seed_imported_context_in_transaction(
            conn, identifier, seed[0].turn_id, "[]"
        )
    assert services.project_task_turn_repo.context(identifier) == context
    assert [
        message.id
        for message in resident_tasks.history_messages(
            services, binding.owner_id, binding.thread_id
        )
    ] == [message.id for message in messages]


async def test_admit_first_native_turn_keeps_all_imported_context_and_reuses_receipt(
    private_history,
):
    services, binding, messages = private_history
    receipt, created = services.resident_request_repo.create(
        binding.owner_id,
        binding.thread_id,
        "new-send",
        "native-turn",
        "New private question",
        100,
        configuration_json=json.dumps({"model_ref": "test/model"}),
    )
    assert created
    assert services.resident_request_repo.mark_running(
        receipt.turn_id, "resident-worker", 100, 30000
    )
    identifier = await resident_tasks.admit(services, receipt)
    assert await resident_tasks.admit(services, receipt) == identifier
    turns = services.project_task_turn_repo.list(identifier)
    assert [turn.status for turn in turns] == ["completed", "queued"]
    assert turns[1].turn_id == receipt.turn_id and turns[1].request_id == receipt.request_id
    assert turns[1].prompt == receipt.prompt
    context = services.project_task_turn_repo.context(identifier)
    assert context is not None and json.loads(context[1]) == [
        message_to_dict(message) for message in messages
    ]
    assert (
        services.project_task_repo.get_internal(identifier, binding.owner_id).active_turn_id
        == receipt.turn_id
    )
    assert (
        services.resident_request_repo.get(binding.owner_id, receipt.request_id).status == "running"
    )


@pytest.mark.parametrize(
    "status,configuration", [("running", {"imported_history": True}), ("completed", {})]
)
async def test_context_seed_rejects_unfinished_or_nonimported_turns(
    private_history, status, configuration
):
    services, binding, _ = private_history
    identifier = await resident_tasks.ensure_task(
        services, binding.owner_id, binding.thread_id, "new-send"
    )
    seed = services.project_task_turn_repo.list(identifier)[0]
    with services.db.transaction() as conn:
        conn.execute("DELETE FROM project_task_contexts WHERE task_id=?", (identifier,))
        conn.execute(
            "UPDATE project_task_turns SET status=?,configuration_json=? WHERE turn_id=?",
            (status, json.dumps(configuration), seed.turn_id),
        )
        assert not services.project_task_turn_repo.seed_imported_context_in_transaction(
            conn, identifier, seed.turn_id, "[]"
        )
    assert services.project_task_turn_repo.context(identifier) is None
