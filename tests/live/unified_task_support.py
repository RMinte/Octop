"""Opt-in real-model Task acceptance; source provider storage is always read-only."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LiveTaskEnvironment:
    client: Any = field(repr=False)
    server: Any = field(repr=False)
    auth: dict[str, str] = field(repr=False)
    owner_id: int
    home: Path
    model_ref: str


def source_provider() -> tuple[dict[str, Any], dict[str, Any]]:
    source = os.environ.get("OCTOP_LIVE_PROVIDER_DB", "")
    reference = os.environ.get("OCTOP_LIVE_MODEL_REF", "")
    if not source or "/" not in reference:
        raise ValueError("Explicit OCTOP_LIVE_PROVIDER_DB and OCTOP_LIVE_MODEL_REF are required")
    provider_name, _, model_id = reference.partition("/")
    with sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM providers WHERE name=?", (provider_name,)
        ).fetchone()
        if row is None or not row["enabled"]:
            raise ValueError("The requested live provider is unavailable")
        provider = dict(row)
    model = next(
        (
            item
            for item in json.loads(provider["models_json"] or "[]")
            if item.get("id") == model_id and item.get("enabled", True)
        ),
        None,
    )
    if model is None:
        raise ValueError("The requested live model is unavailable")
    # Bound only this isolated environment. Do not update the source provider.
    return provider, {**model, "max_output_tokens": 4096}


@asynccontextmanager
async def live_task_environment(home: Path) -> AsyncIterator[LiveTaskEnvironment]:
    from tests.support.app import octop_client, write_octop_config
    from tests.support.auth import auth_header, bootstrap_admin, resolve_user_id

    if any(key.startswith("OCTOP_DATABASE_") and value for key, value in os.environ.items()):
        raise ValueError("Live acceptance refuses inherited database overrides")
    provider, model = source_provider()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    profiles = home / "browser-profiles"
    profiles.mkdir(exist_ok=True)
    (profiles / "acceptance-isolated").touch()
    write_octop_config(
        home,
        database={"driver": "sqlite", "sqlite_path": "octop.db"},
        enable_api_docs=True,
        default_timezone="Asia/Shanghai",
    )
    async with octop_client(home, patch_llm=False) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        services = server.services
        services.provider_repo.create(
            name=provider["name"],
            kind=provider["kind"],
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            extra_json=provider["extra_json"],
            models_json=json.dumps([model]),
        )
        reference = f"{provider['name']}/{model['id']}"
        services.settings_repo.set_active_model(provider["name"], model["id"])
        await server.app_runtime.agent_registry.on_provider_changed()
        yield LiveTaskEnvironment(
            client, server, auth, await resolve_user_id(client, auth, "admin"), home, reference
        )


async def await_task(
    env: LiveTaskEnvironment, task_id: str, *, timeout: float = 240
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await env.client.get(f"/api/project-tasks/{task_id}", headers=env.auth)
        response.raise_for_status()
        task = response.json()
        if task["active_turn_id"] is None:
            return task
        await asyncio.sleep(0.25)
    raise TimeoutError(f"Live Task did not settle: {task_id}")


def live_options(env: LiveTaskEnvironment) -> dict[str, Any]:
    return {
        "model_ref": env.model_ref,
        "reasoning_mode": "disabled",
        "action_timeline_enabled": False,
    }
