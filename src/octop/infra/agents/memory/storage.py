"""Read-only storage diagnostics and eligibility for single-agent maintenance."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.workspace.dir import workspace_dir_from_config_json


def memory_namespace(agent_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", f"agent_{agent_id}".lower())


def workspace_for_agent_row(row: Any, *, paths: Any) -> Path:
    """Resolve only server-owned memory paths without creating a workspace."""
    return workspace_dir_from_config_json(
        row.config_json, paths=paths, agent_id=row.agent_id, ensure=False
    )


def sqlite_store_is_shared(registry: Any, agent_id: str, memory: Any) -> bool:
    """Include stopped agents: a file operation cannot be namespace-scoped."""
    path = Path(memory.backend._db_path).expanduser().resolve()
    for row in registry._repos.agent_repo.list_all(include_disabled=True):
        if row.agent_id == agent_id:
            continue
        parsed = json.loads(row.config_json or "{}")
        config = parsed if isinstance(parsed, dict) else {}
        _ns, backend, cfg = open_memory_kwargs(
            agent_id=row.agent_id,
            cfg=config,
            octop_config=registry._config,
            workspace_dir=workspace_for_agent_row(row, paths=registry._paths),
        )
        if backend == "sqlite" and cfg and Path(cfg["db_path"]).expanduser().resolve() == path:
            return True
    return False


def inspect_storage(memory: Any, *, blocked_reason: str | None) -> dict[str, Any]:
    if type(memory.backend).__name__ == "PostgresMemoryBackend":
        from octop.infra.agents.memory.postgres_storage import inspect_postgres_storage

        return inspect_postgres_storage(memory.backend, blocked_reason=blocked_reason)
    from octop.infra.agents.memory.sqlite_storage import inspect_sqlite_storage

    report = inspect_sqlite_storage(memory.backend, shared=blocked_reason == "shared_sqlite")
    if not report["checkpoints_available"]:
        blocked_reason = "shared_sqlite"
    report.update(
        backend="sqlite",
        scope="shared_database" if blocked_reason in {"postgres", "shared_sqlite"} else "agent",
        maintenance_supported=blocked_reason is None,
        blocked_reason=blocked_reason,
        checked_at=time.time(),
    )
    return report
