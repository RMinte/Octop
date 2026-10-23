"""Unified Task conversation CRUD, streaming and interactive REPL."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from typing import Any

import click

from octop.cli.support.ctx import json_output_enabled
from octop.i18n import tr
from octop.infra.errors import OctopError
from octop.infra.projects import cli_sessions, tasks


def _expert(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    ctx = click.get_current_context(silent=True)
    return (ctx.find_root().obj or {}).get("agent_id") if ctx else None


def _owner(services: Any, username: str | None, agent_id: str | None = None) -> int:
    from octop.cli.support.ctx import resolve_user
    from octop.cli.support.state import default_state_path, load
    from octop.infra.users.acting import resolve_acting_user_id

    return resolve_acting_user_id(
        services,
        as_username=resolve_user(username),
        pinned_username=load(default_state_path()).default_user,
        agent_id=agent_id,
    )


def _options(function: Any) -> Any:
    for decorator in reversed(
        [
            click.option("--expert", "--agent", "agent_id", default=None),
            click.option("--no-expert", is_flag=True, default=False),
            click.option("--user", "as_user", default=None),
            click.option("--project", "project_id", default=None),
            click.option("--task-id", default=None),
            click.option(
                "--thread-id",
                default=None,
                help="Resolve an old thread through its migration route.",
            ),
            click.option("--request-id", default=None),
            click.option("--session-key", default="default"),
            click.option("--model", default=None),
            click.option(
                "--mode",
                "conversation_mode",
                type=click.Choice(["ask", "plan", "craft"]),
                default=None,
            ),
            click.option("--plain", is_flag=True),
        ]
    ):
        function = decorator(function)
    return function


def _run_turn(
    agent_id: str | None,
    prompt: str,
    *,
    session_key: str,
    thread_id: str | None,
    model: str | None = None,
    conversation_mode: str | None = None,
    plain: bool = False,
    as_user: str | None = None,
    task_id: str | None = None,
    request_id: str | None = None,
    project_id: str | None = None,
    no_expert: bool = False,
) -> Any:
    from octop.cli.repl.render import ChunkRenderer
    from octop.cli.repl.runtime import run_chat_turn_async

    renderer = None if json_output_enabled() else ChunkRenderer(plain=plain)
    if renderer:
        renderer.begin_turn()
    try:
        result = asyncio.run(
            run_chat_turn_async(
                agent_id,
                prompt,
                as_user=as_user,
                session_key=session_key,
                thread_id=thread_id,
                model=model,
                conversation_mode=conversation_mode,
                on_chunk=renderer.feed if renderer else lambda _chunk: None,
                task_id=task_id,
                request_id=request_id,
                project_id=project_id,
                no_expert=no_expert,
            )
        )
    except (OctopError, ValueError) as exc:
        raise click.ClickException(
            exc.message if isinstance(exc, OctopError) else str(exc)
        ) from exc
    finally:
        if renderer:
            renderer.finish_turn()
    if json_output_enabled():
        click.echo(json.dumps(asdict(result), ensure_ascii=False))
    if result.status in {"failed", "interrupted"}:
        raise click.exceptions.Exit(1)
    return result


@click.group("chats")
def chats() -> None:
    """Task conversation list/history, message streaming and REPL."""


@chats.command("list")
@click.option("--expert", "--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
@click.option("--project", "project_id", default=None)
@click.option("--limit", default=50, type=click.IntRange(1, 100))
def list_chats(
    agent_id: str | None, as_user: str | None, project_id: str | None, limit: int
) -> None:
    from octop.cli.support.db import open_cli_services

    with open_cli_services() as services:
        try:
            rows = cli_sessions.listing(
                services,
                _owner(services, as_user, _expert(agent_id)),
                limit=limit,
                expert_id=_expert(agent_id),
                project_id=project_id,
            )
        except (OctopError, ValueError) as exc:
            raise click.ClickException(
                exc.message if isinstance(exc, OctopError) else str(exc)
            ) from exc
    click.echo(json.dumps(rows, indent=2, ensure_ascii=False))


@chats.command("get")
@click.argument("identifier")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
@click.option("--limit", default=50, type=click.IntRange(1, 100))
def get_chat(identifier: str, agent_id: str | None, as_user: str | None, limit: int) -> None:
    from octop.cli.support.db import open_cli_services

    with open_cli_services() as services:
        try:
            owner_id = _owner(services, as_user, _expert(agent_id))
            task_id = cli_sessions.resolve_task(services, owner_id, identifier)
            result = tasks.detail(services, owner_id, task_id, limit=limit)
        except (OctopError, ValueError) as exc:
            raise click.ClickException(
                exc.message if isinstance(exc, OctopError) else str(exc)
            ) from exc
        click.echo(result.model_dump_json(indent=2))


@chats.command("create")
@click.argument("prompt")
@_options
def create_chat(prompt: str, **options: Any) -> None:
    """Create a Task with its first actual user message."""
    if options["task_id"] or options["thread_id"]:
        raise click.UsageError("create: --task-id / --thread-id")
    event_id = options["request_id"] or uuid.uuid4().hex
    options["session_key"] = "create:" + event_id
    options["request_id"] = event_id
    agent_id = _expert(options.pop("agent_id"))
    _run_turn(agent_id, prompt, **options)


@chats.command("update")
@click.argument("identifier")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
@click.option("--title", default=None)
@click.option("--pinned/--unpinned", default=None)
def update_chat(
    identifier: str,
    agent_id: str | None,
    as_user: str | None,
    title: str | None,
    pinned: bool | None,
) -> None:
    from octop.cli.support.db import open_cli_services

    if title is None and pinned is None:
        raise click.UsageError("--title / --pinned / --unpinned")
    with open_cli_services() as services:
        try:
            owner_id = _owner(services, as_user, _expert(agent_id))
            task_id = cli_sessions.resolve_task(services, owner_id, identifier)
            current = tasks.detail(services, owner_id, task_id)
            changes: dict[str, Any] = {}
            if title is not None:
                changes["title"] = title
            if pinned is not None:
                changes["pinned"] = pinned
            result = tasks.patch(
                services,
                owner_id,
                task_id,
                tasks.TaskPatch(expected_revision=current.list_revision, **changes),
            )
        except (OctopError, ValueError) as exc:
            raise click.ClickException(
                exc.message if isinstance(exc, OctopError) else str(exc)
            ) from exc
        click.echo(result.model_dump_json(indent=2))


@chats.command("delete")
@click.argument("identifier")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
@click.option("--yes", is_flag=True)
def delete_chat(identifier: str, agent_id: str | None, as_user: str | None, yes: bool) -> None:
    from octop.cli.support.db import open_cli_services

    if not yes:
        click.confirm(identifier, abort=True)
    with open_cli_services() as services:
        try:
            owner_id = _owner(services, as_user, _expert(agent_id))
            tasks.delete(
                services, owner_id, cli_sessions.resolve_task(services, owner_id, identifier)
            )
        except (OctopError, ValueError) as exc:
            raise click.ClickException(
                exc.message if isinstance(exc, OctopError) else str(exc)
            ) from exc
    click.echo(json.dumps({"deleted": identifier}))


@chats.command("send")
@click.argument("prompt")
@_options
def send_chat(prompt: str, **options: Any) -> None:
    """Send or continue a Task, with an optional expert and project."""
    agent_id = _expert(options.pop("agent_id"))
    _run_turn(agent_id, prompt, **options)


@chats.command("repl")
@_options
def repl(
    agent_id: str | None,
    no_expert: bool,
    as_user: str | None,
    project_id: str | None,
    task_id: str | None,
    thread_id: str | None,
    request_id: str | None,
    session_key: str,
    model: str | None,
    conversation_mode: str | None,
    plain: bool,
) -> None:
    """Keep a Task conversation across interactive turns."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    from octop.cli.repl.embedded_session import embedded_task_runtime
    from octop.cli.repl.render import ChunkRenderer, print_welcome
    from octop.cli.repl.runtime import CliChatSession, resolve_embedded_user_id
    from octop.infra.utils.paths import PathLayout

    chosen_expert = _expert(agent_id)
    history_path = PathLayout.from_env().root / "repl_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))
    print_welcome(
        agent_id=chosen_expert or tr("cli.unified_tasks.general", "zh"),
        model=model or "default",
        session_key=session_key,
        thread_id=task_id or thread_id,
    )

    async def loop() -> None:
        async with embedded_task_runtime() as server:
            user_id = resolve_embedded_user_id(server, agent_id=chosen_expert, as_user=as_user)
            chat = CliChatSession(server, user_id=user_id)
            current_model, current_mode, current_expert = model, conversation_mode, chosen_expert
            current_no_expert = no_expert
            current_task, old_thread = task_id, thread_id
            next_request_id = request_id
            try:
                while True:
                    try:
                        text = (await asyncio.to_thread(prompt_session.prompt, "> ")).strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not text:
                        continue
                    if text in {"/exit", "/quit"}:
                        break
                    if text == "/clear":
                        click.echo()
                        continue
                    renderer = ChunkRenderer(plain=plain)
                    renderer.begin_turn()
                    try:
                        result = await chat.run_turn(
                            current_expert,
                            text,
                            session_key=session_key,
                            thread_id=old_thread,
                            model=current_model,
                            conversation_mode=current_mode,
                            on_chunk=renderer.feed,
                            task_id=current_task,
                            request_id=next_request_id or uuid.uuid4().hex,
                            project_id=project_id,
                            no_expert=current_no_expert,
                        )
                        next_request_id = None
                    except (OctopError, ValueError) as exc:
                        click.echo(
                            exc.message if isinstance(exc, OctopError) else str(exc), err=True
                        )
                        continue
                    finally:
                        renderer.finish_turn()
                    for action in result.actions:
                        if action["action"] == "set_model":
                            current_model = action["model"]
                        elif action["action"] == "clear_model":
                            current_model = None
                        elif action["action"] == "set_conversation_mode":
                            current_mode = action["mode"]
                        elif action["action"] == "switch_agent":
                            current_expert = action.get("agent_id")
                            current_no_expert = current_expert is None
                        elif action["action"] == "new_chat":
                            current_task, old_thread = None, None
                    click.echo()
            finally:
                chat.close()

    asyncio.run(loop())
