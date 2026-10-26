"""Owned task-scoped expert memory inspection and explicit human saves."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Annotated, Any, Literal

from octop_memory import MemoryService
from octop_memory.adapters.bridge.handlers import Bridge
from octop_memory.application.path_projection import atom_to_path, raw_to_path
from octop_memory.core import Memory
from pydantic import Field, StrictBool, StrictInt, StringConstraints

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.approval_models import RequestID
from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.expert_profiles import expert_configuration
from octop.infra.projects.models import ProjectModel, Reference

MemorySource = Literal["general", "scope", "legacy"]


class ExpertMemoryEntry(ProjectModel):
    path: str
    content: str
    layer: str
    occurred_at: datetime | None = None
    source_id: str | None = None


class ExpertMemoryExtractionState(ProjectModel):
    job_id: str
    turn_id: str
    status: str
    phase: str
    error_code: str


class ExpertMemoryEntries(ProjectModel):
    expert_id: str
    source: MemorySource
    entries: list[ExpertMemoryEntry]
    enabled: bool
    extraction_jobs: list[ExpertMemoryExtractionState] = Field(default_factory=list)


class ExpertMemoryQuery(ProjectModel):
    source: MemorySource = "scope"
    query: str = Field(default="", max_length=4096)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=20, ge=1, le=100)


class ExpertMemorySave(ProjectModel):
    source: Literal["general", "scope"]
    request_id: RequestID
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32768)
    ]
    topic: str | None = Field(default=None, min_length=1, max_length=256)
    expected_expert_id: Reference | None = None


class ExpertMemorySaveResult(ProjectModel):
    expert_id: str
    source: Literal["general", "scope"]
    status: Literal["completed", "uncertain"]
    entry: ExpertMemoryEntry | None = None
    replayed: bool


class ExpertMemoryPolicy(ProjectModel):
    expert_id: str
    expert_name: str
    config_revision: int
    enabled: bool
    capture_enabled: bool
    legacy_recall_enabled: bool
    auto_extract_enabled: bool = True
    general_promotion_enabled: bool = True
    policy_editable: bool = True


class ExpertMemoryPolicyPatch(ProjectModel):
    expected_revision: StrictInt = Field(ge=1)
    legacy_recall_enabled: StrictBool
    capture_enabled: StrictBool | None = None
    auto_extract_enabled: StrictBool | None = None
    general_promotion_enabled: StrictBool | None = None
    expected_expert_id: Reference | None = None


def _resolve(
    services: SharedServices, owner_id: int, task_id: str, expected_expert_id: str | None = None
) -> tuple[Any, Any, dict[str, Any], ExpertMemory]:
    task = services.project_task_repo.get(task_id, owner_id)
    if task is None or task.selected_expert_id is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if expected_expert_id is not None and expected_expert_id != task.selected_expert_id:
        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
    row, config = expert_configuration(services, owner_id, task.selected_expert_id)
    policy = config.get("memory") or {}
    policy = policy if isinstance(policy, dict) else {}
    memory = ExpertMemory(
        services,
        owner_id,
        task_id,
        "management",
        task.project_id,
        {
            "expert_id": row.agent_id,
            "owner_id": row.user_id,
            "memory": {
                "enabled": policy.get("memory_enabled", True) is not False,
                "capture_enabled": policy.get("capture_enabled", True) is not False,
                "legacy_recall_enabled": row.user_id == owner_id
                and policy.get("legacy_recall_enabled", False) is True,
            },
        },
        enabled=None,
    )
    return task, row, config, memory


def _policy_view(row: Any, config: dict[str, Any], owner_id: int) -> ExpertMemoryPolicy:
    policy = config.get("memory") or {}
    policy = policy if isinstance(policy, dict) else {}
    return ExpertMemoryPolicy(
        expert_id=row.agent_id,
        expert_name=row.name,
        config_revision=row.config_revision,
        enabled=policy.get("memory_enabled", True) is not False,
        capture_enabled=policy.get("capture_enabled", True) is not False,
        legacy_recall_enabled=row.user_id == owner_id
        and policy.get("legacy_recall_enabled", False) is True,
        auto_extract_enabled=policy.get("auto_extract_enabled", True) is not False
        and policy.get("extract_on_session_end", True) is not False,
        general_promotion_enabled=policy.get("general_promotion_enabled", True) is not False,
        policy_editable=row.user_id == owner_id,
    )


def memory_policy(services: SharedServices, owner_id: int, task_id: str) -> ExpertMemoryPolicy:
    _, row, config, _ = _resolve(services, owner_id, task_id)
    return _policy_view(row, config, owner_id)


def update_memory_policy(
    services: SharedServices, owner_id: int, task_id: str, body: ExpertMemoryPolicyPatch
) -> ExpertMemoryPolicy:
    _, row, config, _ = _resolve(services, owner_id, task_id, body.expected_expert_id)
    if row.user_id != owner_id:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    policy = config.get("memory") or {}
    policy = dict(policy) if isinstance(policy, dict) else {}
    policy["legacy_recall_enabled"] = body.legacy_recall_enabled
    if body.capture_enabled is not None:
        policy["capture_enabled"] = body.capture_enabled
    if body.auto_extract_enabled is not None:
        policy["auto_extract_enabled"] = body.auto_extract_enabled
        policy["extract_on_session_end"] = body.auto_extract_enabled
    if body.general_promotion_enabled is not None:
        policy["general_promotion_enabled"] = body.general_promotion_enabled
    config["memory"] = policy
    if not services.agent_repo.update_memory_policy(
        row.agent_id, owner_id, body.expected_revision, json.dumps(config, ensure_ascii=False)
    ):
        raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
    refreshed, latest = expert_configuration(services, owner_id, row.agent_id)
    return _policy_view(refreshed, latest, owner_id)


def _get(memory: Memory, path: str) -> ExpertMemoryEntry:
    response = Bridge(memory).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "memory_get", "params": {"path": path}}
    )
    if "error" in response:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    result = response["result"]
    return ExpertMemoryEntry(path=result["path"], content=result["excerpt"], layer=result["kind"])


def _query(memory: Memory, body: ExpertMemoryQuery) -> list[ExpertMemoryEntry]:
    if body.path is not None:
        return [_get(memory, body.path)]
    if body.query.strip():
        hits = MemoryService(memory, host="octop-memory-management").search(
            body.query, max_results=body.limit
        )["hits"]
        return [
            ExpertMemoryEntry(
                path=hit["path"],
                content=hit["snippet"],
                layer=hit["layer"],
                occurred_at=hit.get("occurred_at"),
                source_id=hit.get("source_id"),
            )
            for hit in hits
        ]
    entries = [
        ExpertMemoryEntry(
            path=atom_to_path(atom),
            content=atom.assertion,
            layer="atom",
            occurred_at=atom.occurred_at,
            source_id=atom.id,
        )
        for atom in memory.list_atoms(limit=body.limit)
    ]
    if len(entries) < body.limit:
        entries.extend(
            ExpertMemoryEntry(
                path=raw_to_path(event),
                content=event.content,
                layer="raw",
                occurred_at=event.timestamp,
                source_id=event.id,
            )
            for event in memory.list_raw(limit=body.limit - len(entries))
        )
    return entries


def query_memory(
    services: SharedServices, owner_id: int, task_id: str, body: ExpertMemoryQuery
) -> ExpertMemoryEntries:
    _, row, _, memory = _resolve(services, owner_id, task_id)
    if body.source not in memory.namespaces:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    entries = (
        memory.management_operation(body.source, lambda store: _query(store, body))
        if memory.enabled
        else []
    )
    return ExpertMemoryEntries(
        expert_id=row.agent_id,
        source=body.source,
        entries=entries,
        enabled=memory.enabled,
        extraction_jobs=[
            ExpertMemoryExtractionState(
                job_id=job.job_id,
                turn_id=job.turn_id,
                status=job.status,
                phase=job.phase,
                error_code=job.error_code,
            )
            for job in services.expert_memory_job_repo.list_for_task(task_id, owner_id)
            if job.expert_id == row.agent_id
        ],
    )


def save_memory(
    services: SharedServices, owner_id: int, task_id: str, body: ExpertMemorySave
) -> ExpertMemorySaveResult:
    _, row, _, memory = _resolve(services, owner_id, task_id, body.expected_expert_id)
    if not memory.enabled:
        raise OctopError.localized(ErrorCode.TASK_RESOURCE_UNAVAILABLE)
    namespace = memory.namespaces[body.source]
    digest = hashlib.sha256(
        json.dumps([namespace, body.source, body.content, body.topic], ensure_ascii=False).encode()
    ).hexdigest()
    request_key = hashlib.sha256(body.request_id.encode()).hexdigest()
    path = services.paths.root / "expert-memory-requests" / namespace / f"{request_key}.json"

    def save(store: Memory) -> ExpertMemorySaveResult:
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous["digest"] != digest:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
            previous_result = previous.get("result")
            if previous_result is not None:
                return ExpertMemorySaveResult.model_validate({**previous_result, "replayed": True})
            return ExpertMemorySaveResult(
                expert_id=row.agent_id, source=body.source, status="uncertain", replayed=True
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "digest": digest,
            "status": "writing",
            "owner_id": owner_id,
            "expert_id": row.agent_id,
            "namespace": namespace,
            "request_id": body.request_id,
            "task_id": task_id,
        }

        def persist() -> None:
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)

        persist()
        try:
            node = MemoryService(
                store,
                host="octop-memory-management",
                manual_documents_provider=memory.current_manual_documents,
            ).save(
                body.content,
                topic=body.topic,
                metadata={
                    "owner_id": owner_id,
                    "expert_id": row.agent_id,
                    "source_task_id": task_id,
                    "request_id": body.request_id,
                    "source": body.source,
                },
            )
            result = ExpertMemorySaveResult(
                expert_id=row.agent_id,
                source=body.source,
                status="completed",
                entry=ExpertMemoryEntry(
                    path=f"atom/{node.atom_id}.md",
                    content=node.content,
                    layer="atom",
                    occurred_at=node.created_at,
                    source_id=node.atom_id,
                ),
                replayed=False,
            )
            record["status"] = "completed"
            record["result"] = result.model_dump(mode="json")
            persist()
            return result
        except Exception:
            record["status"] = "uncertain"
            persist()
            return ExpertMemorySaveResult(
                expert_id=row.agent_id, source=body.source, status="uncertain", replayed=False
            )

    return memory.management_operation(body.source, save)  # type: ignore[no-any-return]
