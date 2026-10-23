"""CLI adapters use real Task/source persistence without a legacy expert graph."""
# ruff: noqa: F811

from __future__ import annotations

import json
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from octop.cli.main import cli
from octop.cli.repl.task_runtime import CliChatSession
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import cli_sessions, ingress, interactions, source_routes, tasks
from octop.infra.projects.interaction_models import QuestionPayload
from tests.unit.projects.test_conversation_migrations import source  # noqa: F401
from tests.unit.projects.test_expert_execution import services  # noqa: F401
from tests.unit.projects.test_unified_ingress import message


def runtime(services):
    return SimpleNamespace(
        services=services, project_task_worker=None, conversation_migration_worker=None
    )


def complete(services, task_id, turn_id):
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='completed',reply='Answer' WHERE turn_id=?",
            (turn_id,),
        )
        services.project_task_repo.finish(conn, task_id, turn_id, source_routes.now_ms())


@pytest.fixture
def terminal_observer(services, monkeypatch):
    message(services)

    async def observe(_services, owner, receipt, *, worker=None):
        complete(services, receipt.task_id, receipt.turn_id)
        yield tasks.detail(services, owner, receipt.task_id).turns[-1]

    monkeypatch.setattr(ingress, "observe", observe)


async def send(session, text="hello", event="event-one", **kwargs):
    return await session.run_turn(
        None,
        text,
        session_key="cli-session",
        thread_id=None,
        model="ingress/model",
        on_chunk=lambda _chunk: None,
        request_id=event,
        **kwargs,
    )


async def test_cli_general_multiturn_and_request_replay(services, terminal_observer):
    session = CliChatSession(runtime(services), user_id=1)
    first = await send(session)
    second = await send(session, "again", "event-two")
    replay = await send(session, "again", "event-two")
    assert first.task_id == second.task_id == replay.task_id
    assert second.turn_id == replay.turn_id != first.turn_id
    task = tasks.detail(services, 1, first.task_id)
    assert task.selected_expert_id is None and len(task.turns) == 2
    with pytest.raises(OctopError) as caught:
        await send(session, "again", "event-two", no_expert=True)
    assert caught.value.code == ErrorCode.TASK_REQUEST_CONFLICT
    session.close()
    assert tasks.detail(services, 1, first.task_id).turns[-1].status == "completed"


async def test_new_is_empty_draft_and_retry_cannot_replace_later_draft(services, terminal_observer):
    session = CliChatSession(runtime(services), user_id=1)
    first = await send(session)
    await send(session, "/new", "new-request")
    generation = cli_sessions.preferences(services, 1, "cli-session")["generation"]
    await send(session, "/new", "new-request")
    assert cli_sessions.preferences(services, 1, "cli-session")["generation"] == generation
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 1
    second = await send(session, "new conversation", "event-next")
    assert second.task_id != first.task_id
    replay = await send(session, "hello", "event-one")
    assert replay.task_id == first.task_id and replay.turn_id == first.turn_id
    assert tasks.detail(services, 1, first.task_id).turns[0].prompt == "hello"


async def test_explicit_task_binding_is_owned_and_does_not_clone(services, terminal_observer):
    first = await send(CliChatSession(runtime(services), user_id=1))
    result = await send(
        CliChatSession(runtime(services), user_id=1),
        "direct",
        "direct-request",
        task_id=first.task_id,
    )
    assert result.task_id == first.task_id
    with pytest.raises(OctopError) as caught:
        await send(CliChatSession(runtime(services), user_id=2), task_id=first.task_id)
    assert caught.value.code == ErrorCode.NOT_FOUND


async def test_answer_uses_exact_interaction_and_replay_skips_new_pending_lookup(
    services, monkeypatch
):
    body = message(services)
    source = cli_sessions.envelope(
        services, 1, session_key="cli-session", request_id="first-event", text="hello"
    )
    receipt = await ingress.submit(services, 1, source, body)
    turn = services.project_task_turn_repo.get(receipt.turn_id)
    interaction = interactions.new(turn, QuestionPayload(question="Which library?"), "ask-one")
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='running',wait_kind='answer',checkpoint_id='checkpoint',interactions_json=? WHERE turn_id=?",
            (interactions.dump([interaction]), turn.turn_id),
        )
        services.project_task_repo.wait(
            conn, receipt.task_id, turn.turn_id, "answer", source_routes.now_ms()
        )

    async def observe(_services, owner, accepted, *, worker=None):
        yield tasks.detail(services, owner, accepted.task_id).turns[-1]

    monkeypatch.setattr(ingress, "observe", observe)
    session = CliChatSession(runtime(services), user_id=1)
    with pytest.raises(OctopError) as caught:
        await send(session, "/approve " + interaction.interaction_id, "wrong-kind")
    assert caught.value.code == ErrorCode.TASK_CONTROL_INVALID
    text = "/answer " + interaction.interaction_id + " SQLite"
    first = await send(session, text, "answer-event")
    again = await send(session, text, "answer-event")
    saved = interactions.load(services.project_task_turn_repo.get(turn.turn_id))[0]
    assert first.turn_id == again.turn_id == turn.turn_id
    assert saved.resolution.answer == "SQLite" and saved.revision == 2
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_task_turns").fetchone()[0] == 1


async def test_cli_crud_is_task_based_without_starting_runtime(
    services, terminal_observer, monkeypatch, tmp_path
):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    first = await send(CliChatSession(runtime(services), user_id=1))
    from octop.cli.support import db

    monkeypatch.setattr(db, "open_cli_services", lambda: nullcontext(services))
    runner = CliRunner()
    listed = runner.invoke(cli, ["--json", "--user", "owner", "chats", "list"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["task_id"] == first.task_id
    updated = runner.invoke(
        cli, ["--user", "owner", "chats", "update", first.task_id, "--title", "Renamed", "--pinned"]
    )
    assert updated.exit_code == 0, updated.output
    result = runner.invoke(cli, ["--user", "owner", "chats", "get", first.task_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["title"] == "Renamed"
    assert json.loads(result.output)["pinned"]
    result = runner.invoke(cli, ["--user", "owner", "chats", "delete", first.task_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert services.project_task_repo.get(first.task_id, 1) is None


def test_create_requires_first_message_and_supports_task_options():
    runner = CliRunner()
    result = runner.invoke(cli, ["chats", "create"])
    assert result.exit_code == 2 and "PROMPT" in result.output
    result = runner.invoke(cli, ["chats", "send", "--help"])
    for option in ("--expert", "--no-expert", "--project", "--task-id", "--request-id"):
        assert option in result.output


def test_send_failed_task_returns_nonzero_with_machine_readable_receipt(monkeypatch):
    from octop.cli.repl import runtime as public_runtime
    from octop.cli.repl.turn import ChatTurnResult

    async def failed(*_args, **_kwargs):
        return ChatTurnResult(task_id="failed-task", turn_id="failed-turn", status="failed")

    monkeypatch.setattr(public_runtime, "run_chat_turn_async", failed)
    result = CliRunner().invoke(cli, ["--json", "chats", "send", "Run", "--user", "owner"])
    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "failed"
    assert json.loads(result.output)["turn_id"] == "failed-turn"


async def test_old_thread_continues_migrated_task_without_old_writes(source, monkeypatch):
    from octop.infra.history.trajectory.types import TrajectoryEvent
    from octop.infra.projects import branches, input_attachments

    services, _workspace, _messages = source
    message(services)
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES('cli-project',1,'CLI Project',1,1)"
        )
    services.trajectory_event_repo.append(
        TrajectoryEvent(
            "legacy-event",
            "thread",
            "expert",
            1,
            123.0,
            "assistant",
            "legacy-turn",
            None,
            False,
            "Original event",
            {"content": "Original public event"},
        )
    )
    services.agent_repo.create(
        agent_id="replacement",
        user_id=1,
        name="Replacement",
        config_json=json.dumps({"workspace_dir": str(services.paths.root / "replacement")}),
    )
    original = services.thread_message_repo.range_rows("thread", 0, 100)

    async def observe(_services, owner, receipt, *, worker=None):
        complete(services, receipt.task_id, receipt.turn_id)
        yield tasks.detail(services, owner, receipt.task_id).turns[-1]

    monkeypatch.setattr(ingress, "observe", observe)
    session = CliChatSession(runtime(services), user_id=1)
    result = await session.run_turn(
        "replacement",
        "Continue here",
        session_key="old-cli",
        thread_id="thread",
        model="ingress/model",
        request_id="continue-request",
        project_id="cli-project",
        on_chunk=lambda _chunk: None,
    )
    task = tasks.detail(services, 1, result.task_id)
    assert len(task.turns) == 2
    assert task.turns[0].history_only and task.turns[0].source_thread_id == "thread"
    assert task.turns[1].prompt == "Continue here" and not task.turns[1].history_only
    assert task.selected_expert_id == "replacement"
    assert task.project_id == "cli-project"
    assert task.turns[0].source_agent_id == "expert"
    assert services.thread_message_repo.range_rows("thread", 0, 100) == original
    assert session.task_id == result.task_id
    branch = branches.create(
        services, 1, result.task_id, branches.TaskFork(request_id="cli-fork-request")
    )
    copied = tasks.detail(services, 1, branch.task_id)
    assert copied.turns[0].source_trajectory[0].source_event_id == "legacy-event"
    attachment = copied.turns[0].attachments[0]
    tasks.delete(services, 1, result.task_id)
    assert tasks.detail(services, 1, branch.task_id).turns[0].source_trajectory[0].history_only
    assert (
        input_attachments.download(services, 1, attachment.attachment_id, task_id=branch.task_id)[1]
        == b"quarter,revenue\nQ1,12"
    )


async def test_cli_cancel_is_explicit_and_session_close_keeps_accepted_work(services):
    body = message(services)
    source = cli_sessions.envelope(
        services, 1, session_key="cli-session", request_id="first-event", text="hello"
    )
    receipt = await ingress.submit(services, 1, source, body)
    session = CliChatSession(runtime(services), user_id=1)
    session.close()
    assert not services.project_task_turn_repo.get(receipt.turn_id).cancel_requested
    result = await send(session, "/cancel " + receipt.turn_id, "cancel-event")
    assert result.status == "cancelled" and result.turn_id == receipt.turn_id
    assert services.project_task_turn_repo.get(receipt.turn_id).cancel_requested


def test_repl_reuses_task_and_request_id_only_for_first_turn(services, monkeypatch, tmp_path):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    message(services)
    import prompt_toolkit

    from octop.cli.repl import embedded_session, task_runtime

    @asynccontextmanager
    async def embedded():
        yield runtime(services)

    async def observe(_services, owner, receipt, *, worker=None):
        complete(services, receipt.task_id, receipt.turn_id)
        yield tasks.detail(services, owner, receipt.task_id).turns[-1]

    inputs = iter(["First", "Second", "/new", "Third", "/exit"])
    monkeypatch.setattr(
        prompt_toolkit,
        "PromptSession",
        lambda **_kwargs: SimpleNamespace(prompt=lambda _prompt: next(inputs)),
    )
    monkeypatch.setattr(embedded_session, "embedded_task_runtime", embedded)
    monkeypatch.setattr(ingress, "observe", observe)
    monkeypatch.setattr(task_runtime, "resolve_embedded_user_id", lambda *_args, **_kwargs: 1)
    result = CliRunner().invoke(
        cli,
        [
            "chats",
            "repl",
            "--user",
            "owner",
            "--model",
            "ingress/model",
            "--request-id",
            "repl-first-event",
            "--plain",
        ],
    )
    assert result.exit_code == 0, result.output
    with services.db.connect() as conn:
        turns = conn.execute("SELECT task_id,prompt FROM project_task_turns").fetchall()
        native = conn.execute("SELECT event_id FROM unified_source_inbox").fetchall()
    by_prompt = {row["prompt"]: row["task_id"] for row in turns}
    assert by_prompt["First"] == by_prompt["Second"] != by_prompt["Third"]
    assert sum(row["event_id"] == "repl-first-event" for row in native) == 1


async def test_task_embedded_runtime_never_starts_global_server(services, monkeypatch):
    from octop.cli.repl import embedded_session
    from octop.cli.support import db
    from octop.infra.projects import conversation_migrations
    from octop.infra.projects import runtime as task_workers

    lifecycle = []

    class Worker:
        def __init__(self, *_args, **_kwargs):
            lifecycle.append("construct")

        async def start(self):
            lifecycle.append("start")

        async def close(self):
            lifecycle.append("close")

    class Migration(Worker):
        def start(self):
            lifecycle.append("migration_start")

    async def forbidden(_server):
        raise AssertionError("chats must not start the full server")

    monkeypatch.setattr(db, "open_cli_services", lambda: nullcontext(services))
    monkeypatch.setattr(embedded_session.OctopServer, "start", forbidden)
    monkeypatch.setattr(task_workers, "ProjectTaskWorker", Worker)
    monkeypatch.setattr(ingress, "TaskIngressWorker", Worker)
    monkeypatch.setattr(conversation_migrations, "ConversationMigrationWorker", Migration)
    async with embedded_session.embedded_task_runtime() as instance:
        assert instance.services is services
    assert lifecycle.count("close") == 3
    assert lifecycle.count("start") == 2 and "migration_start" in lifecycle
