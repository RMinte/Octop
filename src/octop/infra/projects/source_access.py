"""Recheck the transport's delegated scope before model and tool dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from octop.infra.db.repos.unified_sources import UnifiedSourceRouteRepo
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError


def assert_source_access(
    services: SharedServices, owner: int, task_id: str, origin: dict[str, Any]
) -> None:
    user = services.user_repo.get(owner)
    with services.db.connect() as conn:
        route = UnifiedSourceRouteRepo.get(conn, owner, str(origin["route_id"]))
    if (
        user is None
        or user.disabled
        or route is None
        or route.state != "task"
        or route.task_id != task_id
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    source = origin["source"]
    if source["source_kind"] == "im":
        channel = services.channel_repo.get(source["source_instance"])
        if channel is None or channel.user_id != owner or not channel.enabled:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
    authority = source.get("authority") or {}
    if source["source_kind"] == "cron" and authority.get("cron_id"):
        cron = services.cron_repo.get(authority["cron_id"])
        if (
            cron is None
            or cron.user_id != owner
            or (authority.get("automatic") and not cron.enabled)
        ):
            raise OctopError.localized(ErrorCode.FORBIDDEN)


class SourceAccessMiddleware(AgentMiddleware[Any, Any]):
    def __init__(
        self, services: SharedServices, owner: int, task_id: str, origin: dict[str, Any]
    ) -> None:
        self.services, self.owner, self.task_id, self.origin = services, owner, task_id, origin

    def _check(self) -> None:
        assert_source_access(self.services, self.owner, self.task_id, self.origin)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        self._check()
        return handler(request)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        await asyncio.to_thread(self._check)
        return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        self._check()
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        await asyncio.to_thread(self._check)
        return await handler(request)


def external_unverified(configuration: dict[str, Any]) -> bool:
    source = (configuration.get("source_origin") or {}).get("source") or {}
    # No IM-to-login identity mapping exists in this release. Channel ownership
    # authorizes a transport, never grants its sender the owner's private scope.
    return source.get("source_kind") == "im"


def restrict_external_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    """Freeze public persona/capabilities while excluding owner-private memories."""
    import copy

    result = copy.deepcopy(configuration)
    if not external_unverified(result):
        return result
    if result.get("execution_engine", {}).get("engine_id") == "acp":
        raise OctopError.localized(
            ErrorCode.TASK_SETTINGS_INVALID,
            status=409,
            setting="engine",
            details={"setting": "engine", "reason": "external_unverified"},
        )
    result["expert_memory_enabled"] = False
    result["knowledge_base_ids"] = []
    result["knowledge_base_catalog"] = []
    result["external_owner_approval_required"] = True

    def public_profile(profile: dict[str, Any]) -> None:
        profile["profile_files"] = [
            item
            for item in profile.get("profile_files", [])
            if item.get("path") in {"SOUL.md", "AGENTS.md"}
        ]
        profile["memory"] = {
            **profile.get("memory", {}),
            "enabled": False,
            "capture_enabled": False,
            "legacy_recall_enabled": False,
            "auto_extract_enabled": False,
            "general_promotion_enabled": False,
        }
        for member in (profile.get("team") or {}).get("members", []):
            public_profile(member)

    if isinstance(result.get("expert_profile"), dict):
        public_profile(result["expert_profile"])
    return result


def external_requires_human(tool_name: str, readonly: set[str] | None = None) -> bool:
    """Only known reads and Task-local reporting are safe without owner approval."""
    return tool_name not in (readonly or set()) and tool_name not in {
        "ls",
        "glob",
        "grep",
        "read_file",
        "read_task_image",
        "search_knowledge",
        "ask_user_question",
        "planning",
        "notify_user",
        "agent_list",
        "ask_agent",
        "task",
        "create_artifact",
        "publish_workspace_file",
        "publish_workspace_archive",
    }
