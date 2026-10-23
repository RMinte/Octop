"""Ephemeral team roster for a task; never registers or modifies experts."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from deepagents import SubAgent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness import HarnessAgent, HarnessAgentConfig
from octop_harness.middleware.filesystem_guard import FilesystemGuardMiddleware
from octop_harness.middleware.tool_guard import ToolGuardMiddleware
from octop_harness.middleware.tools_filter import ToolsFilterMiddleware
from octop_harness.registry import AgentEntry, AgentRegistry
from octop_harness.teams.team_manager import TeamManager

from octop.i18n import tr
from octop.infra.projects.hitl import TaskHumanInTheLoopMiddleware


class SubagentCommandGate(AgentMiddleware[Any, Any]):
    """Nested command interrupts cannot borrow a root task's approval receipt."""

    def __init__(self, locale: str) -> None:
        self.locale = locale

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if request.tool_call["name"] == "execute":
            return ToolMessage(
                content=tr("projects.approval_not_granted", self.locale),
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        return await handler(request)


def guard_subagents(cfg: HarnessAgentConfig) -> None:
    """Carry task tool and filesystem restrictions into delegated graphs."""
    specs: list[SubAgent] = [
        cast(SubAgent, dict(GENERAL_PURPOSE_SUBAGENT)),
        *(cast(SubAgent, dict(spec)) for spec in cfg.subagents or []),
    ]
    for spec in specs:
        spec["system_prompt"] = f"{cfg.system_prompt}\n\n{spec['system_prompt']}"
        spec["skills"] = [f"/{cfg.system_files_path}/skills"]
        chain: list[Any] = [
            *(item for item in cfg.middleware or [] if not isinstance(item, SubagentCommandGate)),
            FilesystemGuardMiddleware(
                list(cfg.permissions or []),
                root_dir=str(cfg.workspace_dir),
                workspace_dir=cfg.workspace_dir,
            ),
            ToolsFilterMiddleware(
                config=replace(cfg, tools_disabled=cfg.tools_disabled | {"browser_use"})
            ),
        ]
        if cfg.tool_guard_enabled:
            chain.insert(
                0,
                ToolGuardMiddleware(
                    enabled=True, mode=cfg.tool_guard_mode, rules_dir=cfg.tool_guard_rules_dir
                ),
            )
        chain.insert(0, SubagentCommandGate(cfg.language))
        spec["middleware"] = chain
        spec["interrupt_on"] = {
            name: value for name, value in (cfg.interrupt_on or {}).items() if name != "execute"
        }
    cfg.subagents = specs


class TaskTeam:
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.registry = AgentRegistry()
        self.manager = TeamManager(self.registry)
        self.profiles: dict[str, dict[str, Any]] = {}

    def register(self, agent_id: str, agent: HarnessAgent, description: str = "") -> None:
        self.registry.add(
            AgentEntry(
                agent_id=agent_id,
                agent=agent,
                config=agent.config,
                metadata={"user_id": self.owner_id, "description": description},
                tags=[],
                created_at=datetime.now(UTC),
            )
        )

    async def load(
        self,
        factory: Callable[..., HarnessAgent],
        cfg: HarnessAgentConfig,
        specs: list[SubAgent],
        task_id: str,
        runtime_dir: Path,
        tools: list[Any],
        prepare: Callable[[HarnessAgent], Any],
    ) -> None:
        for spec in specs:
            slug = str(spec["name"])
            member_cfg = replace(
                cfg,
                name=slug,
                system_prompt=f"{cfg.system_prompt}\n\n{spec['system_prompt']}",
                system_files_path=f"{cfg.system_files_path}/team/{slug}",
                memory_namespace=f"{task_id}:{slug}",
                memory_backend_config={
                    "db_path": str(runtime_dir / "team" / slug / "memory.sqlite")
                },
                team_enabled=False,
                middleware=[
                    SubagentCommandGate(cfg.language),
                    *(
                        item
                        for item in cfg.middleware or []
                        if not isinstance(item, (TaskHumanInTheLoopMiddleware, SubagentCommandGate))
                    ),
                ],
                # Root task checkpoints own the durable client handoff. Peer
                # streams retain the existing broker until nested resume is wired.
                interrupt_on={
                    name: value
                    for name, value in (cfg.interrupt_on or {}).items()
                    if not name.startswith("client__") and name != "execute"
                },
                subagents=[],
                tools_disabled=cfg.tools_disabled | {"task", "browser_use"},
            )
            await asyncio.to_thread(
                (runtime_dir / "team" / slug).mkdir, parents=True, exist_ok=True
            )
            agent_id = f"{task_id}:{slug}"
            member = await asyncio.to_thread(factory, member_cfg, agent_id=agent_id)
            self.register(agent_id, member, str(spec["description"]))
            if tools:
                member.inject_mcp_tools(tools)
            await prepare(member)

    def speaker(self, agent_id: str) -> tuple[str | None, str | None]:
        profile = self.profiles.get(agent_id)
        if profile is None:
            return None, None
        return str(profile["expert_id"]), str(profile["name"])

    async def load_experts(
        self,
        factory: Callable[..., HarnessAgent],
        cfg: HarnessAgentConfig,
        profiles: list[dict[str, Any]],
        task_id: str,
        runtime_dir: Path,
        *,
        configure: Callable[[dict[str, Any], str], Awaitable[HarnessAgentConfig]],
        prepare: Callable[[HarnessAgent, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Load persisted members with their own abilities in the task directory."""
        for profile in profiles:
            token = hashlib.sha256(str(profile["expert_id"]).encode()).hexdigest()[:20]
            agent_id = f"{task_id}:expert:{token}"
            member_cfg = await configure(profile, agent_id)
            member_cfg = replace(
                member_cfg,
                workspace_dir=cfg.workspace_dir,
                backend=cfg.backend,
                permissions=cfg.permissions,
                system_files_path=f"{cfg.system_files_path}/team/{token}",
                memory=(),
                memory_enabled=False,
                memory_capture_enabled=False,
                memory_namespace=f"{task_id}:expert:{token}",
                memory_backend_config={
                    "db_path": str(runtime_dir / "team" / token / "memory.sqlite")
                },
                checkpointer=False,
                team_enabled=False,
                middleware=[
                    SubagentCommandGate(cfg.language),
                    *(
                        item
                        for item in member_cfg.middleware or []
                        if not isinstance(item, (TaskHumanInTheLoopMiddleware, SubagentCommandGate))
                    ),
                ],
                interrupt_on={
                    name: value
                    for name, value in (member_cfg.interrupt_on or {}).items()
                    if not name.startswith("client__") and name != "execute"
                },
            )
            await asyncio.to_thread(
                (runtime_dir / "team" / token).mkdir, parents=True, exist_ok=True
            )
            member = await asyncio.to_thread(factory, member_cfg, agent_id=agent_id)
            self.register(agent_id, member, str(profile.get("description") or profile["name"]))
            self.profiles[agent_id] = profile
            await prepare(member, profile)

    async def close(self) -> None:
        await self.manager.aclose()
        for entry in self.registry.values():
            await entry.agent.aclose()
