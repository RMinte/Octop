"""Owner-scoped text documents in an AgentComputer managed project workspace."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from octop_harness.backends.workspace import BackendWorkspace
from psycopg import Error as PostgresError

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.preparation_lock import preparation_lock
from octop.infra.agentcomputer.project_document_tool import (
    MAX_DOCUMENT_BYTES,
    document_workspace_key,
    document_workspace_path,
    read_project_document_content,
    validate_document_filename,
)
from octop.infra.agentcomputer.tasks import _project_agent
from octop.infra.agents.manager import AgentManager
from octop.infra.db.services import SharedServices
from octop.infra.errors import OctopError


class InvalidProjectDocument(ValueError):
    """The submitted filename or content does not fit the text-document contract."""


class ProjectDocumentTooLarge(ValueError):
    """A document exceeds the bounded UTF-8 tool input size."""


def _filename(value: object) -> str:
    try:
        return validate_document_filename(value)
    except CommandAdmissionError as exc:
        raise InvalidProjectDocument("invalid document filename") from exc


def _workspace(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    project_id: UUID,
) -> BackendWorkspace | None:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    with services.db.transaction() as conn:
        project = services.ac_project_repo.get_in_transaction(conn, str(project_id), owner_id)
        if project is None:
            return None
        preparation = services.ac_project_preparation_repo.get_in_transaction(
            conn, str(project_id), owner_id
        )
        if (
            project.box_id != str(identity.boxID)
            or preparation is None
            or preparation.restore_epoch != identity.restoreEpoch
        ):
            raise StorageDataError("project document identity binding mismatch")
        if preparation.workspace_state != "verified":
            raise CommandAdmissionError("runtimeUnavailable", reason="preparationIncomplete")
        agent_id = _project_agent(services, conn, owner_id, project_id, identity.boxID)
    try:
        return registry.workspace_for_project_preparation(agent_id, str(project_id))
    except OctopError as exc:
        raise StorageDataError("project document workspace binding mismatch") from exc


def _document_lock_path(services: SharedServices, project_id: UUID) -> Path:
    return services.paths.agentcomputer_identity.parent / "document-locks" / f"{project_id}.lock"


def create_project_document(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    project_id: UUID,
    filename: str,
    content: str,
) -> bool:
    """Create once; an OS lock prevents concurrent imports from overwriting."""
    name = _filename(filename)
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeError as exc:
        raise InvalidProjectDocument("invalid UTF-8 document content") from exc
    if len(content_bytes) > MAX_DOCUMENT_BYTES:
        raise ProjectDocumentTooLarge("document exceeds size limit")
    try:
        workspace = _workspace(services, registry, owner_id, project_id)
        if workspace is None:
            return False
        with preparation_lock(_document_lock_path(services, project_id)) as acquired:
            if not acquired:
                raise CommandAdmissionError("resourceConflict")
            document_workspace_path(workspace, name)
            if workspace.exists(document_workspace_key(workspace, name)):
                raise CommandAdmissionError("resourceConflict")
            workspace.mkdir("documents")
            document_workspace_path(workspace, name)
            workspace.write_text(f"documents/{name}", content, force=False)
            confirmed = read_project_document_content(workspace, name)
            if confirmed["status"] != "ok" or confirmed["content"] != content:
                raise StorageDataError("project document confirmation mismatch")
            return True
    except (sqlite3.DatabaseError, PostgresError, OSError) as exc:
        raise StorageUnavailableError("project document storage unavailable") from exc


def list_project_documents(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    project_id: UUID,
) -> list[str] | None:
    """List only eligible text files; never expose workspace host paths."""
    try:
        workspace = _workspace(services, registry, owner_id, project_id)
        if workspace is None:
            return None
        if (Path(workspace.workspace_dir) / "documents").is_symlink():
            raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed")
        entries = workspace.list_dir("documents")
        if entries is None:
            return []
        names: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("is_dir") is not False:
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path.startswith("documents/"):
                continue
            name = path.removeprefix("documents/")
            try:
                _filename(name)
                document_workspace_path(workspace, name)
            except (InvalidProjectDocument, CommandAdmissionError):
                continue
            names.append(name)
        return sorted(set(names))
    except (sqlite3.DatabaseError, PostgresError, OSError) as exc:
        raise StorageUnavailableError("project document storage unavailable") from exc


def get_project_document(
    services: SharedServices,
    registry: AgentManager,
    owner_id: int,
    project_id: UUID,
    filename: str,
) -> str | None:
    """Read one bounded UTF-8 document; missing project and file both yield 404."""
    name = _filename(filename)
    try:
        workspace = _workspace(services, registry, owner_id, project_id)
        if workspace is None:
            return None
        result = read_project_document_content(workspace, name)
        if result["status"] == "notFound":
            return None
        if result["status"] == "tooLarge":
            raise ProjectDocumentTooLarge("document exceeds size limit")
        if result["status"] != "ok" or not isinstance(result["content"], str):
            raise StorageDataError("project document is unreadable")
        return result["content"]
    except (sqlite3.DatabaseError, PostgresError, OSError) as exc:
        raise StorageUnavailableError("project document storage unavailable") from exc
