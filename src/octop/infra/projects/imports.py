"""Copy explicit external projects and configuration into Octop without activation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from octop.infra.db.repos.external_imports import ImportedConfigurationRow
from octop.infra.db.repos.projects import ProjectRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import service
from octop.infra.projects.import_models import (
    ConfigurationCategory,
    ConfigurationDetail,
    ConfigurationImport,
    ConfigurationImportResult,
    ConfigurationView,
    ImportSource,
    ProjectImport,
    ProjectImportResult,
)
from octop.infra.utils.ulid import new_ulid


def import_project(
    services: SharedServices, owner_id: int, body: ProjectImport
) -> ProjectImportResult:
    repo = services.project_repo
    imports = services.external_import_repo
    action: Literal["imported", "linked"]
    with services.db.transaction() as conn:
        if not repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        mapped = imports.project(conn, owner_id, body.source, body.external_project_id)
        if mapped is not None:
            if body.project_id is not None and body.project_id != mapped:
                raise OctopError.localized(ErrorCode.PROJECT_REQUEST_CONFLICT, status=409)
            row = repo.get_in_transaction(conn, mapped, owner_id)
            if row is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            return ProjectImportResult(project=service._view(row), action="unchanged")
        if body.project_id is not None:
            row = repo.get_in_transaction(conn, body.project_id, owner_id)
            if row is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            action = "linked"
        else:
            names = {row.name for row in repo.list_in_transaction(conn, owner_id)}
            name = body.name
            identity = hashlib.sha256(body.external_project_id.encode()).hexdigest()[:8]
            counter = 1
            while name in names:
                extra = "" if counter == 1 else f"-{counter}"
                suffix = f" ({body.source}-{identity}{extra})"
                name = body.name[: 128 - len(suffix)] + suffix
                counter += 1
            now = int(datetime.now(UTC).timestamp() * 1000)
            row = repo.create(
                conn,
                ProjectRow(
                    project_id="proj_" + new_ulid(),
                    owner_id=owner_id,
                    name=name,
                    description="",
                    directory=None,
                    client_directory=body.client_directory,
                    instructions="",
                    skills_json="[]",
                    mcp_servers_json="[]",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                ),
            )
            action = "imported"
        imports.save_project(conn, owner_id, body.source, body.external_project_id, row.project_id)
        return ProjectImportResult(project=service._view(row), action=action)


def _configuration_view(row: ImportedConfigurationRow) -> ConfigurationView:
    return ConfigurationView.model_validate(
        {
            "configuration_id": row.configuration_id,
            "source": row.source,
            "category": row.category,
            "name": row.name,
            "source_path": row.source_path,
            "digest": row.digest,
            "byte_count": row.byte_count,
            "file_count": row.file_count,
            "created_at": datetime.fromtimestamp(row.created_at / 1000, UTC),
        }
    )


def import_configuration(
    services: SharedServices, owner_id: int, body: ConfigurationImport
) -> ConfigurationImportResult:
    files = sorted((file.model_dump() for file in body.files), key=lambda f: f["relative_path"])
    content = {"category": body.category, "name": body.name, "files": files}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    imports = services.external_import_repo
    with services.db.transaction() as conn:
        if not services.project_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        previous = imports.configuration_by_digest(conn, owner_id, body.source, digest)
        if previous is not None:
            return ConfigurationImportResult(
                configuration=_configuration_view(previous), action="unchanged"
            )
        row = ImportedConfigurationRow(
            configuration_id="importcfg_" + new_ulid(),
            owner_id=owner_id,
            source=body.source,
            category=body.category,
            name=body.name,
            source_path=body.source_path,
            digest=digest,
            byte_count=sum(file.byte_count() for file in body.files),
            file_count=len(files),
            files_json=json.dumps(files, sort_keys=True, separators=(",", ":")),
            created_at=int(datetime.now(UTC).timestamp() * 1000),
        )
        imports.save_configuration(conn, row)
        return ConfigurationImportResult(configuration=_configuration_view(row), action="imported")


def list_configurations(
    services: SharedServices,
    owner_id: int,
    *,
    limit: int,
    offset: int,
    source: ImportSource | None,
    category: ConfigurationCategory | None,
) -> list[ConfigurationView]:
    with services.db.connect() as conn:
        rows = services.external_import_repo.configurations(
            conn, owner_id, limit=limit, offset=offset, source=source, category=category
        )
    return [_configuration_view(row) for row in rows]


def get_configuration(
    services: SharedServices, owner_id: int, configuration_id: str
) -> ConfigurationDetail:
    with services.db.connect() as conn:
        row = services.external_import_repo.configuration(conn, owner_id, configuration_id)
    if row is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return ConfigurationDetail(
        **_configuration_view(row).model_dump(), files=json.loads(row.files_json)
    )
