"""Explicit private-conversation context inspection and serialized compaction."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, cast

from octop_harness import HarnessAgent
from octop_harness.compaction import CompactResult, display_offload_path
from pydantic import Field

from octop.infra.agents.resident_conversation import ResidentConversationRuntime, ResidentModel
from octop.infra.agents.resident_identity import resident_execution_scope
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.usage.model_calls import model_call_scope

logger = logging.getLogger(__name__)


class ResidentContextSegment(ResidentModel):
    key: str
    tokens: int = Field(ge=0)


class ResidentContextUsage(ResidentModel):
    max_tokens: int = Field(ge=0)
    used_tokens: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    source: Literal["model_request", "empty"]
    segments: list[ResidentContextSegment]


class ResidentContextCompact(ResidentModel):
    ok: bool
    reason: Literal["ok", "nothing_to_compact", "unavailable", "error"]
    summarized_count: int = Field(ge=0)
    preserved_count: int = Field(ge=0)
    removed_tokens: int = Field(ge=0)
    display_path: str | None = Field(
        description="Workspace-relative history location, never a host path."
    )


async def _agent(runtime: ResidentConversationRuntime, agent_id: str) -> HarnessAgent:
    try:
        return runtime.registry.get_agent(agent_id)
    except OctopError as error:
        if error.code not in {ErrorCode.AGENT_NOT_RUNNING, ErrorCode.AGENT_FAILED}:
            raise
    await runtime.registry.start(agent_id)
    return runtime.registry.get_agent(agent_id)


async def context_usage(
    runtime: ResidentConversationRuntime,
    owner_id: int,
    agent_id: str,
    thread_id: str,
) -> ResidentContextUsage:
    await asyncio.to_thread(runtime._require, owner_id, agent_id, thread_id)
    agent = await _agent(runtime, agent_id)
    cap = await asyncio.to_thread(runtime.registry.resolve_context_max_tokens, agent_id)
    with resident_execution_scope(owner_id, thread_id):
        usage = await agent.aget_context_usage(thread_id, max_tokens=cap)
    return ResidentContextUsage(
        max_tokens=usage.max_tokens,
        used_tokens=usage.used_tokens,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        source=usage.source,
        segments=[
            ResidentContextSegment(key=key, tokens=tokens)
            for key, tokens in usage.segments.items()
            if tokens > 0
        ],
    )


def _require_compactable(
    runtime: ResidentConversationRuntime,
    owner_id: int,
    agent_id: str,
    thread_id: str,
) -> None:
    services = runtime.services
    runtime._require(owner_id, agent_id, thread_id)
    with services.db.transaction() as conn:
        if not services.user_repo.lock_identity_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        profile = services.resident_profile_repo.get_by_agent_in_transaction(
            conn, owner_id, agent_id
        )
        if profile is not None and profile.archived:
            raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_ARCHIVED)
        if services.resident_request_repo.active_in_transaction(conn, owner_id, thread_id):
            raise OctopError.localized(ErrorCode.AGENT_BUSY)


async def _compact(
    runtime: ResidentConversationRuntime,
    owner_id: int,
    agent_id: str,
    thread_id: str,
) -> ResidentContextCompact:
    await asyncio.to_thread(_require_compactable, runtime, owner_id, agent_id, thread_id)
    configuration = await asyncio.to_thread(runtime._configuration, owner_id, agent_id)
    model = configuration.get("model_ref")
    if (
        not isinstance(model, str)
        or not model
        or not runtime.registry.providers.is_model_ref_usable(model)
    ):
        raise OctopError.localized(ErrorCode.RESIDENT_MODEL_UNAVAILABLE)
    agent = await _agent(runtime, agent_id)
    with (
        resident_execution_scope(owner_id, thread_id),
        model_call_scope(
            runtime.services,
            user_id=owner_id,
            purpose="compaction",
            agent_id=agent_id,
            thread_id=thread_id,
        ),
    ):
        result = cast(CompactResult, await agent.acompact_conversation(thread_id, model=model))
    reason = (
        result.reason
        if result.reason in {"ok", "nothing_to_compact", "unavailable", "error"}
        else "error"
    )
    return ResidentContextCompact(
        ok=result.ok,
        reason=cast(Literal["ok", "nothing_to_compact", "unavailable", "error"], reason),
        summarized_count=result.summarized_count,
        preserved_count=result.preserved_count,
        removed_tokens=result.removed_tokens,
        display_path=display_offload_path(result.display_path) or None,
    )


async def compact_context(
    runtime: ResidentConversationRuntime,
    owner_id: int,
    agent_id: str,
    thread_id: str,
) -> ResidentContextCompact:
    await asyncio.to_thread(runtime._require, owner_id, agent_id, thread_id)
    async with runtime.thread_scope(thread_id, require_idle=True):
        # A lost transport cannot release the thread lock while its checkpoint write is pending.
        operation = asyncio.create_task(_compact(runtime, owner_id, agent_id, thread_id))
        cancelled = False
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                cancelled = True
            except Exception:
                break
        if cancelled:
            try:
                operation.result()
            except Exception:
                logger.warning("Resident compaction ended after disconnect thread=%s", thread_id)
            raise asyncio.CancelledError
        try:
            return operation.result()
        except OctopError:
            raise
        except Exception as error:
            logger.warning("Resident compaction failed thread=%s", thread_id)
            raise OctopError.localized(ErrorCode.INTERNAL_ERROR) from error
