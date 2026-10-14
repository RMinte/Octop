"""Authenticated Project document import and reads for AgentComputer."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from octop.api.agentcomputer import (
    ProtocolErrorEnvelope,
    admission_error_response,
    protocol_error_response,
)
from octop.api.deps import current_bearer_user, get_server, require_database
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.project_documents import (
    InvalidProjectDocument,
    ProjectDocumentTooLarge,
    create_project_document,
    get_project_document,
    list_project_documents,
)
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
logger = logging.getLogger(__name__)


class DocumentImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: StrictStr = Field(description="UTF-8 text content, at most 16384 encoded bytes.")


class ProjectDocument(BaseModel):
    filename: str = Field(description="Single .md or .txt filename under documents/.")
    content: str = Field(description="Verified UTF-8 text content.")


class ProjectDocumentList(BaseModel):
    items: list[str] = Field(description="Sorted eligible filenames under documents/.")


def _document_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, CommandAdmissionError):
        return admission_error_response(exc)
    if isinstance(exc, InvalidProjectDocument):
        return protocol_error_response("invalidRequest", 422)
    if isinstance(exc, ProjectDocumentTooLarge):
        return protocol_error_response("payloadLimitExceeded", 413)
    if isinstance(exc, IdentityError):
        logger.error("AgentComputer identity is invalid during document access")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    if isinstance(exc, StorageDataError):
        logger.error("AgentComputer project document binding is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="projectInvalid")
    logger.error("AgentComputer project document storage is unavailable")
    return protocol_error_response("storageUnavailable", 503, retryable=True)


@router.put(
    "/projects/{projectID}/documents/{filename}",
    status_code=201,
    response_model=ProjectDocument,
    summary="Import one bounded text document into an owned Project",
    description=(
        "Requires a user Bearer token and a verified, stopped managed Project workspace. "
        "Creates a single UTF-8 .md or .txt file under documents/ and never overwrites it. "
        "Filename is 1–128 characters using letters, numbers, dot, underscore, or hyphen; "
        "it cannot begin with a dot or contain directories. Content is limited to 16384 UTF-8 "
        "bytes. Missing and other users' Projects return the same 404. This does not start a Run."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Project owned by this user"},
        409: {
            "model": ProtocolErrorEnvelope,
            "description": "Document already exists or import in progress",
        },
        413: {
            "model": ProtocolErrorEnvelope,
            "description": "Document exceeds the UTF-8 size limit",
        },
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid filename or JSON body"},
        503: {"model": ProtocolErrorEnvelope, "description": "Workspace or storage unavailable"},
    },
)
async def import_document(
    body: DocumentImport,
    response: Response,
    project_id: UUID = Path(alias="projectID", description="Owned public Project UUID."),
    filename: str = Path(description="Single .md or .txt filename under documents/."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectDocument | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    if server.app_runtime is None:
        return protocol_error_response("runtimeUnavailable", 503, reason="runtimeNotReady")
    try:
        created = await server.agentcomputer_http_operations.run_thread(
            create_project_document,
            server.services,
            server.app_runtime.agent_registry,
            user.id,
            project_id,
            filename,
            body.content,
        )
    except (
        CommandAdmissionError,
        InvalidProjectDocument,
        ProjectDocumentTooLarge,
        IdentityError,
        StorageDataError,
        StorageUnavailableError,
    ) as exc:
        return _document_error(exc)
    if not created:
        return protocol_error_response("resourceNotFound", 404)
    return ProjectDocument(filename=filename, content=body.content)


@router.get(
    "/projects/{projectID}/documents",
    response_model=ProjectDocumentList,
    summary="List an owned Project's text documents",
    description=(
        "Returns sorted eligible .md and .txt filenames under the verified managed workspace's "
        "documents/ directory. Missing and other users' Projects return the same 404. "
        "Host paths and internal agent IDs are omitted."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Project owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Project UUID"},
        503: {"model": ProtocolErrorEnvelope, "description": "Workspace or storage unavailable"},
    },
)
async def document_list(
    response: Response,
    project_id: UUID = Path(alias="projectID", description="Owned public Project UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectDocumentList | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    if server.app_runtime is None:
        return protocol_error_response("runtimeUnavailable", 503, reason="runtimeNotReady")
    try:
        items = await server.agentcomputer_http_operations.run_thread(
            list_project_documents,
            server.services,
            server.app_runtime.agent_registry,
            user.id,
            project_id,
        )
    except (
        CommandAdmissionError,
        IdentityError,
        StorageDataError,
        StorageUnavailableError,
    ) as exc:
        return _document_error(exc)
    if items is None:
        return protocol_error_response("resourceNotFound", 404)
    return ProjectDocumentList(items=items)


@router.get(
    "/projects/{projectID}/documents/{filename}",
    response_model=ProjectDocument,
    summary="Read one owned Project text document",
    description=(
        "Reads at most 16384 UTF-8 bytes from a single .md or .txt file under documents/. "
        "Missing files, Projects, and other users' Projects return the same 404. "
        "This does not start a Run or invoke a model."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "Document or Project not found"},
        413: {
            "model": ProtocolErrorEnvelope,
            "description": "Document exceeds the UTF-8 size limit",
        },
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid filename or Project UUID"},
        503: {"model": ProtocolErrorEnvelope, "description": "Document or storage unavailable"},
    },
)
async def document_read(
    response: Response,
    project_id: UUID = Path(alias="projectID", description="Owned public Project UUID."),
    filename: str = Path(description="Single .md or .txt filename under documents/."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectDocument | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    if server.app_runtime is None:
        return protocol_error_response("runtimeUnavailable", 503, reason="runtimeNotReady")
    try:
        content = await server.agentcomputer_http_operations.run_thread(
            get_project_document,
            server.services,
            server.app_runtime.agent_registry,
            user.id,
            project_id,
            filename,
        )
    except (
        CommandAdmissionError,
        InvalidProjectDocument,
        ProjectDocumentTooLarge,
        IdentityError,
        StorageDataError,
        StorageUnavailableError,
    ) as exc:
        return _document_error(exc)
    if content is None:
        return protocol_error_response("resourceNotFound", 404)
    return ProjectDocument(filename=filename, content=content)
