"""Private resident identity and the text-only runtime boundary."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage, ToolMessage
from octop_harness import HarnessAgentConfig

from octop.i18n import tr
from octop.infra.db.repos.resident_conversations import ResidentConversationRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import normalize_locale
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop.infra.agents.manager import AgentManager
    from octop.infra.db.repos.agents import AgentRow

_EXECUTION_SCOPE: ContextVar[tuple[int, str] | None] = ContextVar(
    "resident_execution", default=None
)
_MEMORY_CONTEXT: ContextVar[str] = ContextVar("resident_memory_context", default="")
_PROMPT_CONTEXT: ContextVar[str | None] = ContextVar("resident_prompt_context", default=None)


@contextmanager
def resident_prompt_scope(text: str | None) -> Iterator[None]:
    """Freeze the professional template prompt at durable message acceptance."""
    token = _PROMPT_CONTEXT.set(text)
    try:
        yield
    finally:
        _PROMPT_CONTEXT.reset(token)


@contextmanager
def resident_memory_scope(text: str) -> Iterator[None]:
    token = _MEMORY_CONTEXT.set(text)
    try:
        yield
    finally:
        _MEMORY_CONTEXT.reset(token)


@contextmanager
def resident_execution_scope(owner_id: int, thread_id: str) -> Iterator[None]:
    token = _EXECUTION_SCOPE.set((owner_id, thread_id))
    try:
        yield
    finally:
        _EXECUTION_SCOPE.reset(token)


def resident_execution_allowed(owner_id: int, thread_id: str) -> bool:
    return bool(thread_id) and _EXECUTION_SCOPE.get() == (owner_id, thread_id)


class ResidentTextOnlyMiddleware(AgentMiddleware[Any, Any]):
    """Remove every tool from requests and refuse even unsolicited tool calls."""

    def __init__(self, locale: str, owner_id: int, thread_id: str) -> None:
        self.locale = locale
        self.owner_id, self.thread_id = owner_id, thread_id

    def _require_scope(self) -> None:
        if not resident_execution_allowed(self.owner_id, self.thread_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        self._require_scope()
        from octop.infra.agents.resident_tools import resident_tool_definitions

        tools = resident_tool_definitions(self.owner_id, self.thread_id, self.locale)
        prompt = _PROMPT_CONTEXT.get()
        memory = _MEMORY_CONTEXT.get()
        if prompt is None and not memory and not tools:
            return request.override(tools=tools)
        existing = (
            SystemMessage(content=prompt)
            if prompt is not None
            else getattr(request, "system_message", None)
        )
        if not memory and not tools:
            return request.override(tools=tools, system_message=existing)
        hints = []
        if memory:
            hints.append(tr("agents.resident.memory_context", self.locale, facts=memory))
        if tools:
            hints.append(tr("agents.resident.turn_capability_policy", self.locale))
        content: str | list[Any] = existing.content if existing is not None else ""
        for hint in hints:
            content = (
                f"{content}\n\n{hint}".strip()
                if isinstance(content, str)
                else [*content, {"type": "text", "text": hint}]
            )
        return request.override(tools=tools, system_message=SystemMessage(content=content))

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        return handler(self._prepare(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        return await handler(self._prepare(request))

    def wrap_tool_call(self, request: Any, handler: Any) -> ToolMessage:
        from octop.infra.agents.resident_tools import execute_resident_tool

        return execute_resident_tool(
            owner_id=self.owner_id,
            thread_id=self.thread_id,
            tool_call=request.tool_call,
            locale=self.locale,
        )

    async def awrap_tool_call(self, request: Any, handler: Any) -> ToolMessage:
        from octop.infra.agents.resident_tools import aexecute_resident_tool

        return await aexecute_resident_tool(
            owner_id=self.owner_id,
            thread_id=self.thread_id,
            tool_call=request.tool_call,
            locale=self.locale,
        )


def resident_harness_config(
    row: AgentRow,
    *,
    thread_id: str,
    timezone: str,
    locale: str,
    default_model: str | None,
    memory_capture_enabled: bool = False,
    memory_backend: str | dict[str, Any] = "sqlite",
    memory_backend_config: dict[str, Any] | None = None,
) -> HarnessAgentConfig:
    config = json.loads(row.config_json or "{}")
    workspace = str(config["workspace_dir"])
    return HarnessAgentConfig(
        name=f"agent_{row.agent_id}",
        language=normalize_locale(locale),
        workspace_dir=workspace,
        system_files_path=".octop",
        default_model=default_model,
        system_prompt=row.system_prompt,
        backend={"type": "filesystem", "root_dir": workspace, "virtual_mode": True},
        default_timezone=timezone,
        bootstrap_enabled=False,
        memory=(),
        memory_enabled=memory_capture_enabled,
        memory_capture_enabled=memory_capture_enabled,
        memory_aux_model_enabled=False,
        memory_extract_on_session_end=False,
        memory_jsonl_enabled=False,
        memory_recall_inject_enabled=False,
        memory_namespace=f"agent_{row.agent_id}",
        memory_backend=memory_backend,
        memory_backend_config=memory_backend_config,
        subagents=[],
        subagents_auto_load=False,
        team_enabled=False,
        skills_dir=[],
        mcp_server_configs={},
        tools=[],
        web_search_tools=False,
        acp_runners={},
        acp_delegate_enabled=False,
        middleware=[ResidentTextOnlyMiddleware(locale, int(row.user_id or 0), thread_id)],
    )


class _ReservationLost(Exception):
    pass


def reserve_resident_in_transaction(
    services: SharedServices,
    conn: Any,
    *,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    name: str,
    system_prompt: str,
    now: int,
    role: str = "primary",
) -> str:
    """Allocate a private Agent and its immutable conversation in one transaction."""
    created = now // 1000
    workspace = services.paths.agents_dir / agent_id
    config = json.dumps(
        {
            "workspace_dir": str(workspace),
            "system_files_path": ".octop",
            "backend": {"type": "filesystem", "root_dir": str(workspace), "virtual_mode": True},
            "memory": {"memory_enabled": False},
            "heartbeat": {"enabled": False},
            "plugins": {},
        }
    )
    if services.agent_repo.name_exists_in_transaction(conn, owner_id, name):
        name = f"{name} · {agent_id[-8:]}"
    services.agent_repo.insert_identity_in_transaction(
        conn,
        agent_id=agent_id,
        user_id=owner_id,
        name=name,
        system_prompt=system_prompt,
        config_json=config,
        now=created,
    )
    services.thread_repo.insert_in_transaction(
        conn,
        thread_id=thread_id,
        agent_id=agent_id,
        user_id=owner_id,
        channel_type="resident",
        session_key=f"{agent_id}:resident:{owner_id}:dm",
        title=name,
        now=created,
    )
    if not services.resident_conversation_repo.reserve_in_transaction(
        conn, owner_id, agent_id, thread_id, now, role
    ):
        raise _ReservationLost
    services.proactive_care_config_repo.disable_in_transaction(conn, agent_id, created)
    return name


def get_resident_identity(
    services: SharedServices, owner_id: int
) -> ResidentConversationRow | None:
    return services.resident_conversation_repo.get(owner_id)


def _reserve(services: SharedServices, owner_id: int) -> ResidentConversationRow:
    existing = services.resident_conversation_repo.get(owner_id)
    if existing is not None:
        return existing
    user = services.user_repo.get(owner_id)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    agent_id, thread_id = new_ulid(), f"thr_{new_ulid()}"
    now = int(time.time() * 1000)
    try:
        with services.db.transaction() as conn:
            if not services.user_repo.lock_identity_owner(conn, owner_id):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            existing = services.resident_conversation_repo.get_in_transaction(conn, owner_id)
            if existing is not None:
                return existing
            reserve_resident_in_transaction(
                services,
                conn,
                owner_id=owner_id,
                agent_id=agent_id,
                thread_id=thread_id,
                name=tr("agents.resident.name", user.locale),
                system_prompt=tr("agents.resident.system_prompt", user.locale),
                now=now,
            )
    except _ReservationLost:
        pass
    binding = services.resident_conversation_repo.get(owner_id)
    assert binding is not None
    return binding


async def ensure_resident_identity(
    services: SharedServices, agent_manager: AgentManager, owner_id: int
) -> ResidentConversationRow:
    binding = await asyncio.to_thread(_reserve, services, owner_id)
    return await initialize_resident_binding(services, binding)


async def initialize_resident_binding(
    services: SharedServices, binding: ResidentConversationRow
) -> ResidentConversationRow:
    owner_id = binding.owner_id
    row = await asyncio.to_thread(services.agent_repo.get, binding.agent_id)
    thread = await asyncio.to_thread(services.thread_repo.get, binding.thread_id)
    if (
        row is None
        or row.user_id != owner_id
        or row.is_shared
        or row.kind != "expert"
        or thread is None
        or thread.agent_id != binding.agent_id
        or thread.user_id != owner_id
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    if binding.status == "ready":
        return binding
    try:
        # No bootstrap, model call, template download or user filesystem inspection.
        await asyncio.to_thread(services.paths.ensure_agent_workspace, binding.agent_id)
    except OSError:
        return await asyncio.to_thread(
            services.resident_conversation_repo.finish_initialization,
            owner_id,
            "failed",
            ErrorCode.AGENT_FAILED.value,
            int(time.time() * 1000),
            binding.role,
        )
    return await asyncio.to_thread(
        services.resident_conversation_repo.finish_initialization,
        owner_id,
        "ready",
        "",
        int(time.time() * 1000),
        binding.role,
    )
