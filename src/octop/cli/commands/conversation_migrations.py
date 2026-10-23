"""Offline management of migration jobs; never starts a model or expert."""

from __future__ import annotations

import asyncio

import click

from octop.cli.support.db import open_cli_services, resolve_cli_user_id
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import conversation_migrations as domain
from octop.infra.projects.conversation_migrations import MigrationCreate


def _owner(services: object, username: str | None) -> int:
    from octop.infra.db.services import SharedServices

    assert isinstance(services, SharedServices)
    owner_id = resolve_cli_user_id(username, services=services)
    if owner_id is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return owner_id


@click.group("conversation-migrations")
def conversation_migrations() -> None:
    """Inspect and resume legacy conversation migration jobs."""


@conversation_migrations.command("preflight")
@click.option("--user", "username", default=None)
@click.option("--thread", "thread_ids", multiple=True)
def preflight(username: str | None, thread_ids: tuple[str, ...]) -> None:
    with open_cli_services() as services:
        result = domain.preflight(
            services,
            _owner(services, username),
            MigrationCreate(request_id="preflight", thread_ids=list(thread_ids)),
        )
        click.echo(result.model_dump_json(indent=2))


@conversation_migrations.command("create")
@click.option("--user", "username", default=None)
@click.option("--request-id", required=True)
@click.option("--thread", "thread_ids", multiple=True)
@click.option("--project", "project_id", default=None)
def create(
    username: str | None, request_id: str, thread_ids: tuple[str, ...], project_id: str | None
) -> None:
    with open_cli_services() as services:
        result = domain.create(
            services,
            _owner(services, username),
            MigrationCreate(
                request_id=request_id, thread_ids=list(thread_ids), project_id=project_id
            ),
        )
        click.echo(result.model_dump_json(indent=2))


@conversation_migrations.command("status")
@click.argument("batch_id")
@click.option("--user", "username", default=None)
def status(batch_id: str, username: str | None) -> None:
    with open_cli_services() as services:
        click.echo(
            domain.detail(services, _owner(services, username), batch_id).model_dump_json(indent=2)
        )


@conversation_migrations.command("control")
@click.argument("batch_id")
@click.argument("action", type=click.Choice(["pause", "resume"]))
@click.option("--user", "username", default=None)
def control(batch_id: str, action: str, username: str | None) -> None:
    with open_cli_services() as services:
        click.echo(
            domain.control(services, _owner(services, username), batch_id, action).model_dump_json(
                indent=2
            )
        )


@conversation_migrations.command("run")
@click.argument("batch_id")
@click.option("--user", "username", default=None)
def run(batch_id: str, username: str | None) -> None:
    """Drain this batch without booting OctopServer or invoking a provider."""
    with open_cli_services() as services:
        owner_id = _owner(services, username)
        domain.detail(services, owner_id, batch_id)
        worker = domain.ConversationMigrationWorker(services)

        async def drain() -> None:
            while await worker.run_once(batch_id):
                pass

        asyncio.run(drain())
        click.echo(domain.detail(services, owner_id, batch_id).model_dump_json(indent=2))


@conversation_migrations.command("resolve")
@click.argument("thread_id")
@click.option("--user", "username", default=None)
def resolve(thread_id: str, username: str | None) -> None:
    with open_cli_services() as services:
        click.echo(
            domain.legacy_link(services, _owner(services, username), thread_id).model_dump_json(
                indent=2
            )
        )


@conversation_migrations.command("automatic")
@click.argument("action", type=click.Choice(["enable", "disable", "status"]))
@click.option("--user", "username", default=None)
def automatic(action: str, username: str | None) -> None:
    with open_cli_services() as services:
        result = domain.automation(
            services, _owner(services, username), None if action == "status" else action == "enable"
        )
        if action == "enable":
            domain.ConversationMigrationWorker(services).enqueue_automatic()
        click.echo(result.model_dump_json(indent=2))
