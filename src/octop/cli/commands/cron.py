"""octop cron commands."""

from __future__ import annotations

import json as _json
import sys

import click

from octop.cli.support.ctx import json_output_enabled
from octop.cli.support.errors import fail_octop
from octop.infra.errors import OctopError


def _expert(value: str | None) -> str | None:
    if value is not None:
        return value
    root = click.get_current_context().find_root()
    return (root.obj or {}).get("agent_id")


@click.group()
def cron() -> None:
    """Cron job management commands."""


@cron.command("list")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
def list_jobs(agent_id: str | None, as_user: str | None) -> None:
    """List cron jobs for an agent."""
    from rich.console import Console
    from rich.table import Table

    from octop.cli.support.offline_ops import list_cron_offline, resolve_cron_user_id

    aid = _expert(agent_id)
    try:
        uid = resolve_cron_user_id(aid, as_user)
        rows = list_cron_offline(aid, user_id=uid)
    except OctopError as exc:
        fail_octop(exc)
    if json_output_enabled():
        click.echo(_json.dumps(rows, indent=2))
        return
    table = Table(title="Cron jobs")
    for col in ("id", "trigger", "task_type", "enabled"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row.get("id", ""),
            row.get("trigger", ""),
            row.get("task_type", ""),
            str(bool(row.get("enabled", True))),
        )
    Console(file=sys.stdout).print(table)


@cron.command("create")
@click.option("--expert", "--agent", "agent_id", default=None)
@click.option("--no-expert", is_flag=True)
@click.option("--user", "as_user", default=None)
@click.option("--trigger", required=True)
@click.option("--prompt", required=True)
@click.option("--fresh-thread", is_flag=True, default=False)
@click.option("--task-type", default="agent")
@click.option(
    "--conversation-policy",
    type=click.Choice(["continue_target", "per_run", "per_job", "task"]),
    default="per_job",
)
@click.option("--task-id", "target_task_id", default=None)
@click.option("--project", "project_id", default=None)
@click.option("--model", default=None)
def create_job(
    agent_id: str | None,
    as_user: str | None,
    trigger: str,
    prompt: str,
    fresh_thread: bool,
    task_type: str,
    no_expert: bool,
    conversation_policy: str,
    target_task_id: str | None,
    project_id: str | None,
    model: str | None,
) -> None:
    """Create a cron job."""
    from octop.cli.support.offline_ops import create_cron_offline, resolve_cron_user_id

    aid = None if no_expert else _expert(agent_id)
    try:
        uid = resolve_cron_user_id(aid, as_user)
        row = create_cron_offline(
            agent_id=aid,
            user_id=uid,
            trigger=trigger,
            prompt=prompt,
            fresh_thread=fresh_thread,
            task_type=task_type,
            conversation_policy="per_run" if fresh_thread else conversation_policy,
            target_task_id=target_task_id,
            project_id=project_id,
            model=model,
        )
    except OctopError as exc:
        fail_octop(exc)
    click.echo(_json.dumps(row, indent=2))
    if not json_output_enabled():
        click.echo("hint: restart `octop run` to load new cron jobs into the running server.")


@cron.command("delete")
@click.argument("cron_id")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
def delete_job(agent_id: str | None, cron_id: str, as_user: str | None) -> None:
    """Delete a cron job."""
    from octop.cli.support.offline_ops import delete_cron_offline, resolve_cron_user_id

    aid = _expert(agent_id)
    try:
        uid = resolve_cron_user_id(aid, as_user)
        delete_cron_offline(aid, cron_id, user_id=uid)
    except OctopError as exc:
        fail_octop(exc)
    click.echo("deleted")


@cron.command("run-now")
@click.argument("cron_id")
@click.option("--agent", "agent_id", default=None)
@click.option("--user", "as_user", default=None)
def run_now(agent_id: str | None, cron_id: str, as_user: str | None) -> None:
    """Run a cron job to completion (embedded server)."""
    from octop.cli.support.embedded_ops import cron_run_now
    from octop.cli.support.offline_ops import resolve_cron_user_id

    aid = _expert(agent_id)
    try:
        uid = resolve_cron_user_id(aid, as_user)
        cron_run_now(aid, cron_id, user_id=uid)
    except OctopError as exc:
        fail_octop(exc)
    except Exception as exc:
        raise click.ClickException(str(exc) or type(exc).__name__) from exc
    click.echo("ok")
