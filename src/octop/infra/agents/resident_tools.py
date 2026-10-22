"""Narrow host tools bound to one accepted private-conversation turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    field_validator,
)

from octop.i18n import error_message, tr
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import Reference
from octop.infra.projects.tasks import TaskExecutionOptions, TaskMessage

if TYPE_CHECKING:
    from octop.infra.db.services import SharedServices

logger = logging.getLogger(__name__)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResidentTaskContext(_Model):
    project_id: Reference | None = Field(
        description="The user's explicitly selected project, or explicit null."
    )
    message: TaskMessage = Field(
        description="Task instructions, attachments and execution options fixed by the host or explicitly selected by the user. Never populated by the assistant model."
    )


def default_task_context(
    request_id: str, prompt: str, model_ref: str | None
) -> ResidentTaskContext:
    """Freeze ordinary task scope for a new steward turn without inheriting resources."""
    return ResidentTaskContext(
        project_id=None,
        message=TaskMessage(
            request_id="resident_intent_" + hashlib.sha256(request_id.encode()).hexdigest(),
            prompt=prompt,
            attachment_ids=[],
            options=TaskExecutionOptions(
                model_ref=model_ref,
                skills=[],
                mcp_servers=[],
                plugins=[],
                subagents=[],
                knowledge_base_ids=[],
                memory_enabled=False,
                team_enabled=False,
                client_tools=[],
            ),
        ),
    )


class ResidentToolInput(_Model):
    read_tools: StrictBool = False
    task_context: ResidentTaskContext | None = None
    knowledge_base_ids: list[Reference] = Field(default_factory=list, max_length=20)

    @field_validator("knowledge_base_ids")
    @classmethod
    def normalized_knowledge_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("knowledge base IDs must not be blank")
        return list(dict.fromkeys(normalized))


class ReadMemoryInput(_Model):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    limit: int = Field(default=5, ge=1, le=10)


class ReadTasksInput(_Model):
    delegation_id: Reference | None = None
    limit: int = Field(default=5, ge=1, le=10)


class CreateTaskInput(_Model):
    brief: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class SearchKnowledgeInput(_Model):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    k: int = Field(default=5, ge=1, le=8)


@dataclass(frozen=True)
class _Scope:
    services: SharedServices
    owner_id: int
    agent_id: str
    thread_id: str
    turn_id: str
    read_tools: bool
    task_context: ResidentTaskContext | None
    knowledge_base_ids: tuple[str, ...]


_SCOPE: ContextVar[_Scope | None] = ContextVar("resident_host_tools", default=None)


@contextmanager
def resident_tools_scope(
    *,
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    turn_id: str,
    read_tools: bool = False,
    task_context: ResidentTaskContext | None = None,
    knowledge_base_ids: list[str] | None = None,
) -> Iterator[None]:
    # Copy the caller-owned Pydantic object so mutable request objects cannot broaden an in-flight scope.
    token = _SCOPE.set(
        _Scope(
            services,
            owner_id,
            agent_id,
            thread_id,
            turn_id,
            read_tools,
            task_context.model_copy(deep=True) if task_context is not None else None,
            tuple(knowledge_base_ids or []),
        )
    )
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _scope(owner_id: int, thread_id: str) -> _Scope | None:
    from octop.infra.agents.resident_identity import resident_execution_allowed

    context = _SCOPE.get()
    if (
        context is None
        or context.owner_id != owner_id
        or context.thread_id != thread_id
        or not resident_execution_allowed(owner_id, thread_id)
    ):
        return None
    return context


def resident_tool_definitions(
    owner_id: int, thread_id: str, locale: str
) -> list[BaseTool | dict[str, Any]]:
    context = _scope(owner_id, thread_id)
    if context is None:
        return []
    tools: list[BaseTool | dict[str, Any]] = []
    specifications: list[tuple[str, str, type[BaseModel]]] = []
    if context.read_tools:
        specifications.append(("resident_read_memory", "tool_memory_description", ReadMemoryInput))
    if context.read_tools or context.task_context is not None:
        specifications.append(("resident_read_tasks", "tool_tasks_description", ReadTasksInput))
    if context.task_context is not None:
        specifications.append(("resident_create_task", "tool_create_description", CreateTaskInput))
    if context.knowledge_base_ids:
        specifications.append(
            ("resident_search_knowledge", "tool_knowledge_description", SearchKnowledgeInput)
        )
    for name, description, model in specifications:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tr(f"agents.resident.{description}", locale),
                    "parameters": model.model_json_schema(),
                },
            }
        )
    return tools


def _require_live(context: _Scope) -> None:
    from octop.infra.agents.resident_history import require_resident_binding
    from octop.infra.projects.tasks import now_ms

    require_resident_binding(
        context.services, context.owner_id, context.agent_id, context.thread_id
    )
    user = context.services.user_repo.get(context.owner_id)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    source = context.services.resident_request_repo.get_by_turn(context.turn_id, context.owner_id)
    if (
        source is None
        or source.thread_id != context.thread_id
        or source.status != "running"
        or source.cancel_requested
        or source.lease_until <= now_ms()
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN)


def _bounded(items: list[dict[str, Any]]) -> dict[str, Any]:
    retained: list[dict[str, Any]] = []
    for item in items:
        if len(json.dumps([*retained, item], ensure_ascii=False).encode()) > 16384:
            break
        retained.append(item)
    return {"items": retained, "truncated": len(retained) != len(items)}


def _read_memory(context: _Scope, arguments: ReadMemoryInput) -> dict[str, Any]:
    from octop.infra.agents.resident_memory import _memory, _view

    with _memory(context.services, context.owner_id, context.agent_id, context.thread_id) as memory:
        atoms = (
            memory.search_atoms(arguments.query, include_deprecated=False, limit=arguments.limit)
            if arguments.query
            else memory.list_atoms(limit=arguments.limit, include_deprecated=False)
        )
        return _bounded(
            [
                {"atom_id": atom.id, "assertion": atom.assertion, "kind": _view(memory, atom).kind}
                for atom in atoms
                if atom.deprecated_at is None and atom.superseded_by is None
            ]
        )


def _read_tasks(context: _Scope, arguments: ReadTasksInput) -> dict[str, Any]:
    from octop.infra.agents.resident_delegations import get_delegation, list_delegations

    items = (
        [
            get_delegation(
                context.services,
                context.owner_id,
                context.agent_id,
                context.thread_id,
                arguments.delegation_id,
            )
        ]
        if arguments.delegation_id is not None
        else list_delegations(
            context.services, context.owner_id, context.agent_id, context.thread_id, arguments.limit
        ).items
    )
    return _bounded(
        [
            {
                "delegation_id": item.delegation_id,
                "task_id": item.task_id,
                "task_exists": item.task_exists,
                "status": item.status,
                "latest_task_turn_id": item.latest_task_turn_id,
                "reports": [
                    {
                        "task_turn_id": report.task_turn_id,
                        "status": report.status,
                        "text": report.text[:1000],
                    }
                    for report in item.reports[-3:]
                ],
            }
            for item in items
        ]
    )


def _knowledge_arguments(context: _Scope, raw: Any) -> SearchKnowledgeInput:
    if not context.knowledge_base_ids:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    arguments = SearchKnowledgeInput.model_validate(raw)
    visible = context.services.knowledge_repo.list_visible(context.owner_id)
    allowed = {base.id for base in visible}
    if any(base_id not in allowed for base_id in context.knowledge_base_ids):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    return arguments


def _search_knowledge(context: _Scope, raw: Any) -> dict[str, Any]:
    from octop.infra.knowledge.retrieve import _retrieve_context_sync

    arguments = _knowledge_arguments(context, raw)
    return {
        "context": _retrieve_context_sync(
            context.services,
            user_id=context.owner_id,
            is_admin=False,
            query=arguments.query,
            knowledge_base_ids=context.knowledge_base_ids,
            k=arguments.k,
            char_budget=6000,
            locale=_locale(context),
            visible_bases=None,
        )
    }


def _create_task(context: _Scope, arguments: CreateTaskInput) -> dict[str, Any]:
    from octop.infra.agents.resident_delegations import ResidentDelegationCreate, create_delegation

    authorization = context.task_context
    if authorization is None:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    slot = hashlib.sha256(
        f"{context.owner_id}:{context.thread_id}:{context.turn_id}".encode()
    ).hexdigest()
    prompt = tr(
        "agents.resident.delegated_brief",
        _locale(context),
        instruction=authorization.message.prompt,
        brief=arguments.brief,
    )
    raw = authorization.message.model_dump(mode="json")
    raw["request_id"], raw["prompt"] = f"resident_task_{slot}", prompt
    body = ResidentDelegationCreate(
        request_id=f"resident_delegate_{slot}",
        source_turn_id=context.turn_id,
        project_id=authorization.project_id,
        message=TaskMessage.model_validate(raw),
    )
    result = create_delegation(
        context.services,
        context.owner_id,
        context.agent_id,
        context.thread_id,
        body,
        require_live_source=True,
    )
    return {
        "delegation_id": result.delegation_id,
        "task_id": result.task_id,
        "initial_turn_id": result.initial_turn_id,
        "status": result.status,
    }


def _locale(context: _Scope) -> str:
    user = context.services.user_repo.get(context.owner_id)
    return user.locale if user is not None else "en"


def execute_resident_tool(
    *,
    owner_id: int,
    thread_id: str,
    tool_call: dict[str, Any],
    locale: str,
) -> ToolMessage:
    name = str(tool_call.get("name") or "")
    call_id = str(tool_call.get("id") or "invalid")
    try:
        if not isinstance(tool_call.get("id"), str) or not tool_call["id"].strip():
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        context = _scope(owner_id, thread_id)
        if context is None:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        _require_live(context)
        arguments = tool_call.get("args") or {}
        if name == "resident_read_memory" and context.read_tools:
            result = _read_memory(context, ReadMemoryInput.model_validate(arguments))
        elif name == "resident_read_tasks" and (
            context.read_tools or context.task_context is not None
        ):
            result = _read_tasks(context, ReadTasksInput.model_validate(arguments))
        elif name == "resident_create_task" and context.task_context is not None:
            result = _create_task(context, CreateTaskInput.model_validate(arguments))
        elif name == "resident_search_knowledge" and context.knowledge_base_ids:
            result = _search_knowledge(context, arguments)
        else:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False), name=name, tool_call_id=call_id
        )
    except ValidationError:
        code = ErrorCode.RESIDENT_TOOL_INPUT_INVALID
    except OctopError as error:
        code = error.code
    except Exception:
        logger.warning("Resident host tool failed tool=%s", name)
        code = ErrorCode.INTERNAL_ERROR
    return ToolMessage(
        content=json.dumps(
            {"error_code": code.value, "message": error_message(code, locale)}, ensure_ascii=False
        ),
        name=name,
        tool_call_id=call_id,
        status="error",
    )


async def aexecute_resident_tool(
    *,
    owner_id: int,
    thread_id: str,
    tool_call: dict[str, Any],
    locale: str,
) -> ToolMessage:
    operation = asyncio.create_task(
        _aexecute_knowledge(
            owner_id=owner_id, thread_id=thread_id, tool_call=tool_call, locale=locale
        )
        if tool_call.get("name") == "resident_search_knowledge"
        else asyncio.to_thread(
            execute_resident_tool,
            owner_id=owner_id,
            thread_id=thread_id,
            tool_call=tool_call,
            locale=locale,
        )
    )
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            if operation.cancelled():
                raise
            cancelled = True
    result = operation.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _aexecute_knowledge(
    *,
    owner_id: int,
    thread_id: str,
    tool_call: dict[str, Any],
    locale: str,
) -> ToolMessage:
    from octop.infra.knowledge.retrieve import retrieve_context

    name, call_id = "resident_search_knowledge", str(tool_call.get("id") or "invalid")
    try:
        if not isinstance(tool_call.get("id"), str) or not tool_call["id"].strip():
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        context = _scope(owner_id, thread_id)
        if context is None:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        await asyncio.to_thread(_require_live, context)
        arguments = await asyncio.to_thread(
            _knowledge_arguments, context, tool_call.get("args") or {}
        )
        text = await retrieve_context(
            context.services,
            user_id=context.owner_id,
            is_admin=False,
            query=arguments.query,
            knowledge_base_ids=context.knowledge_base_ids,
            k=arguments.k,
            char_budget=6000,
            locale=locale,
            visible_bases=None,
        )
        return ToolMessage(
            content=json.dumps({"context": text}, ensure_ascii=False),
            name=name,
            tool_call_id=call_id,
        )
    except ValidationError:
        code = ErrorCode.RESIDENT_TOOL_INPUT_INVALID
    except OctopError as error:
        code = error.code
    except Exception:
        logger.warning("Resident knowledge tool failed")
        code = ErrorCode.INTERNAL_ERROR
    return ToolMessage(
        content=json.dumps(
            {"error_code": code.value, "message": error_message(code, locale)}, ensure_ascii=False
        ),
        name=name,
        tool_call_id=call_id,
        status="error",
    )
