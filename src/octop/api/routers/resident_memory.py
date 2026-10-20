"""Explicit memory management for the authenticated private resident Agent."""

from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, Depends, Query, Response

from octop.api.deps import current_user, get_server
from octop.infra.agents.resident_history import require_resident_binding
from octop.infra.agents.resident_memory import (
    MemoryCandidateStatus,
    ResidentMemoryCandidate,
    ResidentMemoryCandidatePage,
    ResidentMemoryDeprecate,
    ResidentMemoryExtraction,
    ResidentMemoryItem,
    ResidentMemoryPage,
    ResidentMemoryPolicy,
    ResidentMemoryPolicyUpdate,
    ResidentMemoryReject,
    ResidentMemoryReplace,
    ResidentMemorySources,
    ResidentMemoryWrite,
    create_resident_memory,
    decide_resident_memory_candidate,
    deprecate_resident_memory,
    extract_resident_candidates,
    list_resident_memories,
    replace_resident_memory,
    resident_memory_candidate,
    resident_memory_candidates,
    resident_memory_policy,
    resident_memory_sources,
    update_resident_memory_policy,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
_MEMORY = "/agents/{agent_id}/threads/{thread_id}/memory"


def _services(server: OctopServer) -> SharedServices:
    if server.services is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    return server.services


@router.get(
    _MEMORY,
    response_model=ResidentMemoryPage,
    summary="List the private resident Agent's most recent explicit memories",
    description="Uses the existing Agent memory store without starting a model. Deprecated or superseded facts are excluded by default.",
)
async def memories(
    agent_id: str,
    thread_id: str,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    include_deprecated: bool = False,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        list_resident_memories,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        include_deprecated,
    )


@router.post(
    _MEMORY,
    response_model=ResidentMemoryItem,
    status_code=201,
    summary="Explicitly save one private long-term fact",
    description="Records the user as actor in the existing memory audit trail. Conversation capture and automatic inference remain disabled.",
)
async def create(
    agent_id: str,
    thread_id: str,
    body: ResidentMemoryWrite,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryItem:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        create_resident_memory, _services(server), user.id, agent_id, thread_id, body
    )


@router.get(
    _MEMORY + "/policy",
    response_model=ResidentMemoryPolicy,
    summary="Read this private instance's explicit memory capture policy",
    description="Disabled by default. Raw capture does not infer or automatically promote long-term facts.",
)
async def policy(
    agent_id: str,
    thread_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryPolicy:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_memory_policy, _services(server), user.id, agent_id, thread_id
    )


@router.put(
    _MEMORY + "/policy",
    response_model=ResidentMemoryPolicy,
    summary="Explicitly enable or disable raw conversation capture for future turns",
    description="Uses an optimistic revision. Does not enable memory tools, model extraction, or automatic fact promotion.",
)
async def update_policy(
    agent_id: str,
    thread_id: str,
    body: ResidentMemoryPolicyUpdate,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryPolicy:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        update_resident_memory_policy, _services(server), user.id, agent_id, thread_id, body
    )


@router.get(
    _MEMORY + "/candidates",
    response_model=ResidentMemoryCandidatePage,
    summary="List memory candidates in this private instance's existing store",
)
async def candidates(
    agent_id: str,
    thread_id: str,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    status: MemoryCandidateStatus | None = None,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryCandidatePage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_memory_candidates, _services(server), user.id, agent_id, thread_id, limit, status
    )


@router.post(
    _MEMORY + "/candidates:extract",
    response_model=ResidentMemoryExtraction,
    summary="Explicitly distill recent captured user messages into reviewable candidates",
    description="Uses the server-selected conversation model. Processes at most 200 recent user observations and 64 KiB per call; durable window receipts avoid repeating successful extraction. Never promotes facts, extracts diary episodes, or changes history.",
)
async def extract_candidates(
    agent_id: str,
    thread_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryExtraction:
    response.headers["Cache-Control"] = "no-store"
    runtime = server.app_runtime.resident_conversations if server.app_runtime else None
    if runtime is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    await asyncio.to_thread(
        require_resident_binding, runtime.services, user.id, agent_id, thread_id
    )
    async with runtime.thread_scope(thread_id, require_idle=True):
        configuration = await asyncio.to_thread(runtime._configuration, user.id, agent_id)
        operation = asyncio.create_task(
            extract_resident_candidates(
                runtime.services,
                runtime.registry,
                user.id,
                agent_id,
                thread_id,
                str(configuration.get("model_ref") or ""),
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Keep the checkpoint/extraction boundary locked until the model work settles.
            await operation
            raise


@router.get(
    _MEMORY + "/candidates/{candidate_id}",
    response_model=ResidentMemoryCandidate,
    summary="Read one candidate and its decision in this private instance",
)
async def candidate(
    agent_id: str,
    thread_id: str,
    candidate_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryCandidate:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_memory_candidate, _services(server), user.id, agent_id, thread_id, candidate_id
    )


@router.post(
    _MEMORY + "/candidates/{candidate_id}:approve",
    response_model=ResidentMemoryCandidate,
    summary="Approve one candidate as the user without invoking a model",
    description="Reuses canonical memory promotion. Repeating the same decision is safe; the opposite terminal decision conflicts.",
)
async def approve_candidate(
    agent_id: str,
    thread_id: str,
    candidate_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryCandidate:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        partial(
            decide_resident_memory_candidate,
            _services(server),
            user.id,
            agent_id,
            thread_id,
            candidate_id,
            approve=True,
        )
    )


@router.post(
    _MEMORY + "/candidates/{candidate_id}:reject",
    response_model=ResidentMemoryCandidate,
    summary="Reject one candidate and preserve the user's audit decision",
)
async def reject_candidate(
    agent_id: str,
    thread_id: str,
    candidate_id: str,
    body: ResidentMemoryReject,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryCandidate:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        partial(
            decide_resident_memory_candidate,
            _services(server),
            user.id,
            agent_id,
            thread_id,
            candidate_id,
            approve=False,
            reason=body.reason,
        )
    )


@router.get(
    _MEMORY + "/candidates/{candidate_id}/sources",
    response_model=ResidentMemorySources,
    summary="Read bounded evidence and verified history anchors for one candidate",
)
async def candidate_sources(
    agent_id: str,
    thread_id: str,
    candidate_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemorySources:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        partial(
            resident_memory_sources,
            _services(server),
            user.id,
            agent_id,
            thread_id,
            candidate_id,
            candidate=True,
        )
    )


@router.get(
    _MEMORY + "/{atom_id}/sources",
    response_model=ResidentMemorySources,
    summary="Read bounded evidence and verified history anchors for one private fact",
)
async def sources(
    agent_id: str,
    thread_id: str,
    atom_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemorySources:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_memory_sources, _services(server), user.id, agent_id, thread_id, atom_id
    )


@router.post(
    _MEMORY + "/{atom_id}:replace",
    response_model=ResidentMemoryItem,
    summary="Correct an active private memory and preserve its predecessor",
    description="The returned atom_id may differ: the old fact is superseded and excluded from subsequent recall.",
)
async def replace(
    agent_id: str,
    thread_id: str,
    atom_id: str,
    body: ResidentMemoryReplace,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryItem:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        replace_resident_memory, _services(server), user.id, agent_id, thread_id, atom_id, body
    )


@router.post(
    _MEMORY + "/{atom_id}:deprecate",
    response_model=ResidentMemoryItem,
    summary="Deprecate one private memory and stop recalling it",
    description="Logical removal preserves the audit trail. Repeating deprecation is safe; this does not erase historical conversations.",
)
async def deprecate(
    agent_id: str,
    thread_id: str,
    atom_id: str,
    body: ResidentMemoryDeprecate,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentMemoryItem:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        deprecate_resident_memory, _services(server), user.id, agent_id, thread_id, atom_id, body
    )
