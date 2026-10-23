"""Resolve an expert's identity and capabilities without starting its runtime.

Only selected profile content is copied into a turn snapshot. The expert's
workspace and backend never become the task's execution workspace.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness.backends import resolve_backend
from octop_harness.backends.workspace import BackendWorkspace
from octop_harness.plugins import PluginRegistry

from octop.i18n import tr
from octop.infra.agents.plugins.manager import PluginManager
from octop.infra.agents.plugins.plugin_tool_defaults import agent_plugin_enabled
from octop.infra.agents.settings.tool_catalog import effective_tools_disabled
from octop.infra.agents.workspace.dir import (
    harness_workspace_path,
    system_files_path_from_config,
    workspace_dir_from_config,
)
from octop.infra.backend.compat import adapt_backend_protocol
from octop.infra.backend.resolver import (
    default_agent_backend_spec,
    resolve_agent_backend_spec,
    windows_neutralize_host_root,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.skills.skill_package_store import SkillPackageStore


def _array(value: Any) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    return [str(item) for item in value] if isinstance(value, list) else []


def expert_configuration(
    services: SharedServices, owner_id: int, expert_id: str
) -> tuple[Any, dict[str, Any]]:
    row = services.agent_repo.get(expert_id)
    if (
        row is None
        or (row.user_id != owner_id and not (row.kind == "expert" and row.is_shared))
        or row.kind not in {"expert", "team"}
        or (row.kind == "team" and row.is_shared)
        or not row.enabled
        or services.resident_conversation_repo.get_by_agent(expert_id) is not None
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    config = json.loads(row.config_json or "{}")
    if not isinstance(config, dict):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    return row, config


def owned_expert_configuration(
    services: SharedServices, owner_id: int, expert_id: str
) -> tuple[Any, dict[str, Any]]:
    """Mutating expert configuration always requires ownership, even when shared."""
    row, config = expert_configuration(services, owner_id, expert_id)
    if row.user_id != owner_id:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    return row, config


def assert_profile_access(services: SharedServices, owner_id: int, profile: dict[str, Any]) -> None:
    """A frozen persona never preserves a revoked permission to use its source."""
    for selected in [profile, *((profile.get("team") or {}).get("members") or [])]:
        row, _ = expert_configuration(services, owner_id, str(selected["expert_id"]))
        if ("owner_id" in selected and selected["owner_id"] != row.user_id) or (
            "kind" in selected and selected["kind"] != row.kind
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)


class ExpertAccessMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, services: SharedServices, owner_id: int, profile: dict[str, Any]) -> None:
        self.services, self.owner_id, self.profile = services, owner_id, profile

    async def _check(self) -> None:
        await asyncio.to_thread(assert_profile_access, self.services, self.owner_id, self.profile)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        await self._check()
        return await handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        await self._check()
        return await handler(request)


def profile_prompt(profile: dict[str, Any], locale: str) -> str:
    """Render only this frozen expert's public identity, without memory recall."""
    content = (
        str(profile.get("system_prompt") or "")
        + "\n\n"
        + "\n\n".join(
            f"[{item['path']}]\n{item['content']}" for item in profile.get("profile_files", [])
        )
    )
    return tr(
        "projects.expert_profile_prompt", locale, name=profile.get("name", ""), content=content
    )


def expert_profile_supported(config: dict[str, Any], *, backend_spec: Any = None) -> bool:
    """Pure eligibility check; unresolved named backends require resolution first."""
    if config.get("skills"):
        return False
    pending = [backend_spec if backend_spec is not None else config.get("backend")]
    while pending:
        part = pending.pop()
        if isinstance(part, str):
            if part in {"opensandbox", "docker"} or part.startswith("named:"):
                return False
        elif isinstance(part, dict):
            if part.get("type") in {"opensandbox", "docker", "named"}:
                return False
            if part.get("type") == "composite":
                pending.extend([part.get("default"), *(part.get("routes") or {}).values()])
    return True


def plugin_configuration_digest(configuration: Any) -> str:
    return hashlib.sha256(
        json.dumps(configuration or {}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def runtime_plugin_configuration(
    services: SharedServices, owner_id: int, profile: dict[str, Any]
) -> dict[str, Any]:
    """Resolve sensitive tool settings without persisting them in turn snapshots."""
    row, config = expert_configuration(services, owner_id, str(profile["expert_id"]))
    if row.user_id != owner_id:
        return {}
    plugins = config.get("plugins") or {}
    if not isinstance(plugins, dict):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    expected = profile.get("capabilities", {}).get("plugin_configuration_digest")
    if (expected is None and plugins) or (
        expected is not None and plugin_configuration_digest(plugins) != expected
    ):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    return plugins


def validate_expert_workspace_skills(
    profile: dict[str, Any] | None, skills: list[Any], *, inherit_workspace: bool
) -> None:
    """Prevent a project/package selection from silently shadowing expert files."""
    if profile is None or not inherit_workspace:
        return
    defaults = {str(skill["slug"]) for skill in profile.get("workspace_skills", [])}
    if defaults & {str(skill.slug) for skill in skills}:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)


def _workspace(services: SharedServices, row: Any, config: dict[str, Any]) -> BackendWorkspace:
    host = workspace_dir_from_config(
        config, paths=services.paths, agent_id=row.agent_id, ensure=False
    )
    workspace = (
        harness_workspace_path(config["workspace_dir"], config)
        if config.get("workspace_dir")
        else host
    )
    spec = config.get("backend")
    spec = (
        resolve_agent_backend_spec(spec, repo=services.storage_backend_repo)
        if spec is not None
        else default_agent_backend_spec(host)
    )
    # A profile read must never start a container or ephemeral sandbox, even
    # when nested inside a composite backend or behind a named reference.
    if not expert_profile_supported(config, backend_spec=spec):
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    spec = windows_neutralize_host_root(spec, workspace_dir=host)
    return BackendWorkspace(
        adapt_backend_protocol(resolve_backend(spec, workspace_dir=workspace)),
        workspace,
        system_files_path=system_files_path_from_config(config),
    )


def _skill_snapshot(workspace: BackendWorkspace, path: str) -> list[dict[str, str]]:
    """Read workspace-only skill content through the content I/O boundary."""
    result: list[dict[str, str]] = []
    pending = [path]
    directories = 0
    total = 0
    while pending:
        directory = pending.pop()
        directories += 1
        if directories > 256:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        entries = workspace.list_dir(directory)
        if entries is None:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        for entry in entries:
            child = str(entry.get("path", ""))
            relative = PurePosixPath(child).relative_to(PurePosixPath(path))
            if ".." in relative.parts:
                raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
            if entry.get("is_dir"):
                pending.append(child)
                continue
            data = workspace.download_bytes(child)
            if data is None:
                raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
            total += len(data)
            if total > 8 * 1024 * 1024 or len(result) >= 512:
                raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
            result.append(
                {
                    "path": relative.as_posix(),
                    "data": base64.b64encode(data).decode(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return sorted(result, key=lambda item: item["path"])


def resolve_expert_profile(
    services: SharedServices, owner_id: int, selected_expert_id: str | None
) -> dict[str, Any] | None:
    if selected_expert_id is None:
        return None
    row, config = expert_configuration(services, owner_id, selected_expert_id)
    shared_viewer = row.user_id != owner_id
    workspace = _workspace(services, row, config)
    paths = ["SOUL.md", "AGENTS.md"] if shared_viewer else ["SOUL.md", "USER.md", "AGENTS.md"]
    configured = config.get("memory_files")
    if not shared_viewer and isinstance(configured, list):
        paths.extend(str(path) for path in configured)
    profile_files = []
    for path in dict.fromkeys(paths):
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        text = workspace.read_text(path, limit=131073)
        if text is None:
            continue
        if len(text) > 131072:
            raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
        profile_files.append(
            {"path": path, "content": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        )
    disabled = set(_array(config.get("skills_disabled")))
    store = SkillPackageStore(
        repo=services.skill_package_repo, root=services.paths.skill_packages_dir
    )
    skills = []
    mounted: set[str] = set()
    installed_roots: dict[str, list[Path]] = {}
    for package_id in _array(row.skill_package_ids or config.get("skill_package_ids")):
        for summary in store.list_skill_summaries(package_id):
            slug = str(summary["slug"])
            if slug not in disabled:
                skills.append({"package_id": package_id, "slug": slug})
                mounted.add(slug)
                installed_roots.setdefault(slug, []).append(
                    store.package_skills_dir(package_id) / slug
                )
    workspace_skills = []
    workspace_signatures: dict[str, list[tuple[str, str]]] = {}
    for root in dict.fromkeys([workspace.system_rel("skills"), "skills"]):
        for entry in workspace.list_dir(root) or []:
            path = str(entry.get("path", ""))
            slug = PurePosixPath(path).name
            if (
                entry.get("is_dir")
                and slug not in disabled
                and workspace.exists(f"{path}/SKILL.md")
            ):
                entries = _skill_snapshot(workspace, path)
                signature = sorted((item["path"], item["sha256"]) for item in entries)
                if slug in installed_roots:
                    for installed_root in installed_roots[slug]:
                        installed_signature = sorted(
                            (
                                item.relative_to(installed_root).as_posix(),
                                hashlib.sha256(item.read_bytes()).hexdigest(),
                            )
                            for item in installed_root.rglob("*")
                            if item.is_file()
                        )
                        if signature != installed_signature:
                            raise OctopError.localized(
                                ErrorCode.PROJECT_RESOURCE_INVALID, status=422
                            )
                    continue
                if slug in workspace_signatures:
                    if signature != workspace_signatures[slug]:
                        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
                    continue
                workspace_signatures[slug] = signature
                workspace_skills.append({"slug": slug, "files": entries})
                mounted.add(slug)
                if len(mounted) > 64:
                    raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    if config.get("skills"):
        # Arbitrary external skills roots cannot be safely reinterpreted in a
        # project workspace. Reject explicitly instead of silently dropping them.
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    plugin_map = config.get("plugins") or {}
    registry = PluginRegistry()
    global_plugins = PluginManager(
        plugins_dir=services.paths.plugins_dir, config_path=services.paths.config
    ).global_enabled_map()
    plugins = [
        plugin.manifest.id
        for plugin in registry.list_plugins()
        if global_plugins.get(plugin.manifest.id, True)
        and agent_plugin_enabled(plugin_map, plugin.manifest.id)
    ]
    disabled_tools = effective_tools_disabled(
        config,
        registered_plugin_tools=[(tool.plugin_id, tool.name) for tool in registry.all_tools()],
        global_plugins=global_plugins,
    )
    memory = config.get("memory") or {}
    memory = memory if isinstance(memory, dict) else {}
    default_model = next(
        (
            value.strip()
            for value in (row.default_model, config.get("default_model"))
            if isinstance(value, str) and value.strip() and value.strip().lower() != "auto"
        ),
        None,
    )
    mcp_servers = _array(row.mcp_servers)
    knowledge_base_ids = _array(row.knowledge_base_ids)
    if shared_viewer:
        from octop.infra.projects.service import resources

        visible = resources(services, owner_id, include_skills=False)
        available_mcp = {item.server_name for item in visible.mcp_servers}
        mcp_servers = [name for name in mcp_servers if name in available_mcp]
        knowledge = {base.id for base in services.knowledge_repo.list_visible(owner_id)}
        knowledge_base_ids = [base_id for base_id in knowledge_base_ids if base_id in knowledge]
    snapshot = {
        "expert_id": row.agent_id,
        "kind": row.kind,
        "is_shared": bool(row.is_shared),
        "owner_id": row.user_id,
        "name": row.name,
        "description": row.description,
        "config_revision": getattr(row, "config_revision", row.updated_at),
        "system_prompt": row.system_prompt or "",
        "profile_files": profile_files,
        "workspace_skills": workspace_skills,
        "default_model": default_model,
        "capabilities": {
            "skills": skills,
            "mcp_servers": mcp_servers,
            "knowledge_base_ids": knowledge_base_ids,
            "plugins": plugins,
            "plugin_configuration_digest": plugin_configuration_digest(
                {} if shared_viewer else plugin_map
            ),
            "subagents": _array(config.get("subagents")),
            "tools_disabled": sorted(disabled_tools),
        },
        "memory": {
            "enabled": memory.get("memory_enabled", True) is not False,
            "capture_enabled": memory.get("capture_enabled", True) is not False,
            "auto_extract_enabled": memory.get("auto_extract_enabled", True) is not False
            and memory.get("extract_on_session_end", True) is not False,
            "general_promotion_enabled": memory.get("general_promotion_enabled", True) is not False,
            "legacy_recall_enabled": not shared_viewer
            and memory.get("legacy_recall_enabled", False) is True,
        },
    }
    if row.kind == "team":
        from octop.infra.projects.expert_teams import snapshot as team_snapshot

        snapshot["team"] = team_snapshot(services, owner_id, row.agent_id, workspace)
    snapshot["profile_digest"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return snapshot
