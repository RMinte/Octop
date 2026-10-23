"""Project-owned capability references; no expert configuration or installation."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from deepagents import SubAgent
from octop_harness.plugins import PluginRegistry, build_plugin_tools

from octop.infra.agents.plugins.manager import PluginManager
from octop.infra.agents.plugins.plugin_tool_names import sanitize_plugin_tool_names
from octop.infra.agents.subagents.catalog import SubagentCatalog
from octop.infra.db.services import SharedServices
from octop.infra.projects.models import PluginOption, ProjectFields, SubagentOption
from octop.infra.utils.locale import normalize_locale

CAPABILITY_FIELDS = {"plugins", "subagents", "memory_enabled", "team_enabled"}


@lru_cache(maxsize=1)
def subagent_catalog() -> SubagentCatalog:
    catalog = SubagentCatalog(Path(__file__).parents[1] / "agents" / "subagents")
    catalog.refresh()
    return catalog


def capability_options(
    services: SharedServices, owner_id: int
) -> tuple[list[PluginOption], list[SubagentOption]]:
    manager = PluginManager(
        plugins_dir=services.paths.plugins_dir, config_path=services.paths.config
    )
    plugins = [
        PluginOption(
            plugin_id=str(p["id"]),
            name=str(p.get("name") or p["id"]),
            description=str(p.get("description") or ""),
            available=bool(p.get("enabled") and p.get("loaded")),
        )
        for p in manager.list_installed()
        if not p.get("error")
    ]
    user = services.user_repo.get(owner_id)
    locale = normalize_locale(user.locale if user else "zh")
    roles = [
        SubagentOption(slug=s.slug, name=s.name, description=s.description)
        for s in subagent_catalog().list_summaries(locale=locale)
    ]
    return plugins, roles


def plugin_capabilities(
    fields: ProjectFields,
    *,
    reserved: set[str],
    include_skills: bool = True,
    agent_plugins: dict[str, Any] | None = None,
) -> tuple[list[Any], list[Any], list[tuple[str, list[tuple[str, bytes]]]]]:
    """Load current plugin code; only the attached skill files can come from a turn snapshot."""
    registry = PluginRegistry()
    enabled = {p.manifest.id: p.manifest.id in fields.plugins for p in registry.list_plugins()}
    if not set(fields.plugins) <= enabled.keys():
        raise ValueError("selected plugin unavailable")
    tools = (
        build_plugin_tools(global_plugins=enabled, agent_plugins=agent_plugins)
        if fields.plugins
        else []
    )
    sanitize_plugin_tool_names(tools, reserved=reserved)
    middleware = registry.build_middleware_chain(global_enabled=enabled) if fields.plugins else []
    skills: list[tuple[str, list[tuple[str, bytes]]]] = []
    if not include_skills:
        return tools, middleware, skills
    for plugin_id in fields.plugins:
        plugin = registry.get(plugin_id)
        if plugin is None:
            raise ValueError("selected plugin unavailable")
        root = plugin.skills_dir
        if root is not None:
            for skill in sorted(root.iterdir()):
                if skill.is_dir() and (skill / "SKILL.md").is_file():
                    skills.append(
                        (
                            skill.name,
                            [
                                (p.relative_to(skill).as_posix(), p.read_bytes())
                                for p in skill.rglob("*")
                                if p.is_file()
                            ],
                        )
                    )
    return tools, middleware, skills


def subagent_specs(fields: ProjectFields, locale: str) -> list[SubAgent]:
    result: list[SubAgent] = []
    for slug in fields.subagents:
        definition = subagent_catalog().get(slug)
        if definition is None or not definition.content_for(locale):
            raise ValueError("selected subagent unavailable")
        result.append(
            {
                "name": slug,
                "description": definition.description_for(locale),
                "system_prompt": definition.content_for(locale),
            }
        )
    return result
