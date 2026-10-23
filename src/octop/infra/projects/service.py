"""Project CRUD and account-scoped resource selection; never creates an expert."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from psycopg.errors import UniqueViolation

from octop.infra.connectors.service import ConnectorService
from octop.infra.db.repos.projects import ProjectRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.capabilities import CAPABILITY_FIELDS, capability_options
from octop.infra.projects.models import (
    McpOption,
    ProjectFields,
    ProjectPatch,
    ProjectResources,
    ProjectUpdate,
    ProjectView,
    SkillBinding,
    SkillOption,
)
from octop.infra.skills.skill_package_store import SkillPackageStore
from octop.infra.users.resource_policy import (
    POLICY_WORKSPACE_ROOT_DIR,
    effective_workspace_root_dir,
)
from octop.infra.utils.host_dirs import assert_safe_host_path, host_browse_roots
from octop.infra.utils.ulid import new_ulid


def _allowed_root(services: SharedServices, owner_id: int) -> str | None:
    return effective_workspace_root_dir(
        services.user_policy_repo.get(owner_id, POLICY_WORKSPACE_ROOT_DIR)
    )


def resources(
    services: SharedServices, owner_id: int, *, include_skills: bool = True
) -> ProjectResources:
    skills = []
    if include_skills:
        store = SkillPackageStore(
            repo=services.skill_package_repo, root=services.paths.skill_packages_dir
        )
        skills = [
            SkillOption(
                package_id=package.id,
                slug=str(skill["slug"]),
                name=str(skill.get("name") or skill["slug"]),
                description=str(skill.get("description") or ""),
            )
            for package in store.repo.list_all()
            for skill in store.list_skill_summaries(package.id)
            if not skill.get("corrupt")
        ]
    connectors = ConnectorService(
        repo=services.connector_repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    mcp_servers = [
        McpOption(
            server_name=str(row["mcp_server_name"]),
            name=str(row["display_name"]),
            state="disabled"
            if row.get("status") != "active"
            else "configured"
            if row.get("has_credentials")
            else "authorization_required",
        )
        for row in connectors.list_instances_for_api(owner_id)
    ]
    root = _allowed_root(services, owner_id)
    plugins, subagents = capability_options(services, owner_id)
    return ProjectResources(
        skills=skills,
        mcp_servers=mcp_servers,
        plugins=plugins,
        subagents=subagents,
        directory_roots=[root] if root else host_browse_roots(),
    )


def _validate(
    services: SharedServices,
    owner_id: int,
    fields: ProjectFields,
    *,
    cached_skills: bool = False,
    allow_expert_team: bool = False,
) -> str | None:
    available = (
        resources(services, owner_id, include_skills=False)
        if cached_skills
        else resources(services, owner_id)
    )
    if fields.team_enabled and not fields.subagents and not allow_expert_team:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
    if not set(fields.plugins) <= {
        p.plugin_id for p in available.plugins if p.available
    } or not set(fields.subagents) <= {s.slug for s in available.subagents}:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
    if (
        not cached_skills
        and not {(s.package_id, s.slug) for s in fields.skills if isinstance(s, SkillBinding)}
        <= {(s.package_id, s.slug) for s in available.skills}
    ) or not set(fields.mcp_servers) <= {m.server_name for m in available.mcp_servers}:
        raise OctopError.localized(ErrorCode.PROJECT_RESOURCE_INVALID, status=422)
    if fields.directory is None:
        return None
    try:
        if not os.path.isabs(os.path.expanduser(fields.directory)):
            raise ValueError("absolute directory required")
        path = assert_safe_host_path(
            fields.directory,
            restrict_to_home=False,
            restrict_to_root=_allowed_root(services, owner_id),
        )
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError("directory unavailable")
    except (ValueError, OSError) as exc:
        raise OctopError.localized(ErrorCode.PROJECT_DIRECTORY_INVALID, status=422) from exc
    return str(path)


def _view(row: ProjectRow) -> ProjectView:
    return ProjectView(
        project_id=row.project_id,
        archived=row.archived,
        name=row.name,
        description=row.description,
        directory=row.directory,
        client_directory=row.client_directory,
        instructions=row.instructions,
        skills=json.loads(row.skills_json),
        mcp_servers=json.loads(row.mcp_servers_json),
        **json.loads(row.capabilities_json),
        revision=row.revision,
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, UTC),
    )


def list_projects(services: SharedServices, owner_id: int) -> list[ProjectView]:
    return [_view(row) for row in services.project_repo.list_for_owner(owner_id)]


def get_project(services: SharedServices, owner_id: int, project_id: str) -> ProjectView:
    row = services.project_repo.get(project_id, owner_id)
    if row is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return _view(row)


def delete_project(services: SharedServices, owner_id: int, project_id: str) -> None:
    with services.db.transaction() as conn:
        repo = services.project_repo
        if not repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = repo.get_in_transaction(conn, project_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        tasks = services.project_task_repo.for_project(conn, project_id, owner_id)
        if any(task.active_turn_id for task in tasks):
            raise OctopError.localized(ErrorCode.PROJECT_TASKS_BUSY)
        if tasks:
            raise OctopError.localized(ErrorCode.PROJECT_HAS_TASKS)
        repo.delete(conn, row)


def archive_project(
    services: SharedServices, owner_id: int, project_id: str, expected_revision: int, archived: bool
) -> ProjectView:
    with services.db.transaction() as conn:
        repo = services.project_repo
        if not repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = repo.get_in_transaction(conn, project_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if row.revision != expected_revision:
            raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT)
        if row.archived == archived:
            return _view(row)
        if archived and any(
            task.active_turn_id
            for task in services.project_task_repo.for_project(conn, project_id, owner_id)
        ):
            raise OctopError.localized(ErrorCode.PROJECT_TASKS_BUSY)
        result = repo.set_archived(conn, row, archived, int(datetime.now(UTC).timestamp() * 1000))
        if result is None:
            raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT)
        return _view(result)


def create_project(
    services: SharedServices, owner_id: int, fields: ProjectFields, request_id: str
) -> ProjectView:
    fingerprint = hashlib.sha256(
        json.dumps(fields.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    repo = services.project_repo
    try:
        with services.db.transaction() as conn:
            if not repo.lock_owner(conn, owner_id):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            previous = repo.creation_request(conn, owner_id, request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise OctopError.localized(ErrorCode.PROJECT_REQUEST_CONFLICT, status=409)
                return ProjectView.model_validate_json(previous[1])
            directory = _validate(services, owner_id, fields)
            now = int(datetime.now(UTC).timestamp() * 1000)
            row = ProjectRow(
                project_id="proj_" + new_ulid(),
                owner_id=owner_id,
                name=fields.name,
                description=fields.description,
                directory=directory,
                client_directory=fields.client_directory,
                instructions=fields.instructions,
                skills_json=json.dumps([s.model_dump() for s in fields.skills]),
                mcp_servers_json=json.dumps(fields.mcp_servers),
                capabilities_json=json.dumps(fields.model_dump(include=CAPABILITY_FIELDS)),
                revision=1,
                created_at=now,
                updated_at=now,
            )
            result = _view(repo.create(conn, row))
            repo.record_creation(conn, row, request_id, fingerprint, result.model_dump_json())
            return result
    except (sqlite3.IntegrityError, UniqueViolation) as exc:
        raise OctopError.localized(ErrorCode.PROJECT_NAME_TAKEN, status=409) from exc


def patch_project(
    services: SharedServices, owner_id: int, project_id: str, patch: ProjectPatch
) -> ProjectView:
    current = get_project(services, owner_id, project_id)
    changes = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
    fields = ProjectUpdate.model_validate(
        {
            **current.model_dump(include=set(ProjectFields.model_fields)),
            **changes,
            "expected_revision": patch.expected_revision,
        }
    )
    return update_project(services, owner_id, project_id, fields)


def update_project(
    services: SharedServices, owner_id: int, project_id: str, fields: ProjectUpdate
) -> ProjectView:
    row = services.project_repo.get(project_id, owner_id)
    if row is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if row.revision != fields.expected_revision:
        raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT, status=409)
    directory = _validate(services, owner_id, fields)
    changed = replace(
        row,
        name=fields.name,
        description=fields.description,
        directory=directory,
        client_directory=fields.client_directory,
        instructions=fields.instructions,
        skills_json=json.dumps([s.model_dump() for s in fields.skills]),
        mcp_servers_json=json.dumps(fields.mcp_servers),
        capabilities_json=json.dumps(fields.model_dump(include=CAPABILITY_FIELDS)),
        updated_at=int(datetime.now(UTC).timestamp() * 1000),
    )
    try:
        result = services.project_repo.update(changed, fields.expected_revision)
    except (sqlite3.IntegrityError, UniqueViolation) as exc:
        raise OctopError.localized(ErrorCode.PROJECT_NAME_TAKEN, status=409) from exc
    if result is None:
        raise OctopError.localized(ErrorCode.PROJECT_REVISION_CONFLICT, status=409)
    return _view(result)
