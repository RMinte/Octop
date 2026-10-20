"""Private professional instances created from versioned expert templates."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response

from octop.api.deps import current_user, get_server
from octop.infra.agents.resident_profiles import (
    ResidentProfile,
    ResidentProfileAdopt,
    ResidentProfileArchive,
    ResidentProfileCreate,
    ResidentProfileList,
    ResidentTemplateDiff,
    adopt_template,
    archive_profile,
    create_profile,
    get_profile,
    list_profiles,
    template_diff,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(prefix="/agents/resident-profiles")


def _services(server: OctopServer) -> SharedServices:
    if server.services is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    return server.services


@router.get(
    "",
    response_model=ResidentProfileList,
    summary="List the current user's private professional instances",
)
async def profiles(
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentProfileList:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(list_profiles, _services(server), user.id)


@router.post(
    "",
    response_model=ResidentProfile,
    status_code=201,
    summary="Create an independent private professional Agent and conversation",
    description="Idempotent by request_id and input. Copies template persona/method text only; never grants tools or replaces the primary assistant.",
)
async def create(
    body: ResidentProfileCreate,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentProfile:
    response.headers["Cache-Control"] = "no-store"
    return await create_profile(_services(server), server.expert_catalog, user.id, body)


@router.get(
    "/{profile_id}",
    response_model=ResidentProfile,
    summary="Read one private professional instance and its template provenance",
)
async def profile(
    profile_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentProfile:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(get_profile, _services(server), user.id, profile_id)


@router.get(
    "/{profile_id}/template-diff",
    response_model=ResidentTemplateDiff,
    summary="Preview exact template changes without modifying the private instance",
    description="Returns the available content hash and current profile revision needed for explicit adoption.",
)
async def diff(
    profile_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentTemplateDiff:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        template_diff, _services(server), server.expert_catalog, user.id, profile_id
    )


@router.post(
    "/{profile_id}/adopt-template",
    response_model=ResidentProfile,
    summary="Explicitly adopt the exact reviewed template version",
    description="Requires the diff's profile revision and content hash. New turns use the adopted prompt; existing accepted turns, history, memory and permissions are preserved.",
)
async def adopt(
    profile_id: str,
    body: ResidentProfileAdopt,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentProfile:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        adopt_template, _services(server), server.expert_catalog, user.id, profile_id, body
    )


@router.patch(
    "/{profile_id}/archive",
    response_model=ResidentProfile,
    summary="Archive or restore a private professional instance without deleting its data",
    description="Archiving requires an idle conversation and cancels follow-up authorizations. Restoration never revives those follow-ups; history, memory and delegated tasks are preserved.",
)
async def archive(
    profile_id: str,
    body: ResidentProfileArchive,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentProfile:
    response.headers["Cache-Control"] = "no-store"
    runtime = server.app_runtime.resident_conversations if server.app_runtime else None
    if runtime is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    services = _services(server)
    profile = await asyncio.to_thread(get_profile, services, user.id, profile_id)
    async with runtime.thread_scope(profile.thread_id, require_idle=True):
        operation = asyncio.create_task(
            asyncio.to_thread(archive_profile, services, user.id, profile_id, body)
        )
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
            operation.exception()
            raise asyncio.CancelledError
        return operation.result()
