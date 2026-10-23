"""Read project files for a composer draft without creating a Task or workspace."""

from __future__ import annotations

import asyncio
import os

from octop_harness.backends import resolve_backend
from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import workbench
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.projects.service import _allowed_root, get_project
from octop.infra.utils.host_dirs import assert_safe_host_path


def _scope(
    services: SharedServices, owner_id: int, project_id: str, expected_project_revision: int
) -> workbench.WorkbenchScope:
    user = services.user_repo.get(owner_id)
    if user is None or user.disabled:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    project = get_project(services, owner_id, project_id)
    if project.archived:
        raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
    if project.revision != expected_project_revision:
        raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT)
    with services.db.transaction() as conn:
        assert_project_writable(services, conn, owner_id, project_id)
    if project.directory is None:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    try:
        directory = assert_safe_host_path(
            project.directory,
            restrict_to_home=False,
            restrict_to_root=_allowed_root(services, owner_id),
        )
        if not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
            raise ValueError("directory unavailable")
    except (OSError, ValueError) as exc:
        raise OctopError.localized(ErrorCode.PROJECT_DIRECTORY_INVALID, status=422) from exc
    workspace = BackendWorkspace(
        resolve_backend({"type": "filesystem", "root_dir": directory, "virtual_mode": True}),
        directory,
    )
    return workbench.WorkbenchScope(
        view=workbench.WorkbenchView(
            task_id="",
            scope_revision=str(project.revision),
            server_directory=str(directory),
            client_directory=None,
            capabilities=workbench.WorkbenchCapabilities(
                browser=False, terminal=False, recording=False
            ),
            read_only=True,
        ),
        workspace=workspace,
        runtime_dir=services.paths.root,
    )


async def project_tree(
    services: SharedServices,
    owner_id: int,
    project_id: str,
    expected_project_revision: int,
    path: str = ".",
) -> list[workbench.WorkspaceEntry]:
    scope = await asyncio.to_thread(
        _scope, services, owner_id, project_id, expected_project_revision
    )
    return await workbench.tree(scope, path)
