"""AgentComputer discovery for the shared independent project/task service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from octop import __version__
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.db.services import SharedServices
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.tasks import resolve_model_ref


class ProjectTaskFeatures(ProjectModel):
    projectListSync: Literal[True] = True
    taskListPagination: Literal[True] = True
    taskListSync: Literal[True] = True
    projects: Literal[True] = True
    taskConversations: Literal[True] = True
    incrementalReplies: Literal[True] = True
    cancellation: Literal[True] = True
    events: Literal[False] = False
    files: Literal[False] = False
    approvals: Literal[False] = False
    questions: Literal[False] = False
    commandReceipts: Literal[False] = False
    runs: Literal[False] = False


class ProjectTaskCapabilities(ProjectModel):
    protocolVersion: Literal[1] = 1
    contract: Literal["independent-project-tasks"] = "independent-project-tasks"
    contractVersion: Literal[2] = 2
    boxID: UUID
    restoreEpoch: int
    octopVersion: str
    serverTime: datetime
    timezone: str
    capabilities: ProjectTaskFeatures
    executionAvailable: bool
    unavailableReason: Literal["runtimeUnavailable", "modelUnavailable"] | None


def get_capabilities(services: SharedServices, *, worker_ready: bool) -> ProjectTaskCapabilities:
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    model_ready = resolve_model_ref(services) is not None
    return ProjectTaskCapabilities(
        boxID=identity.boxID,
        restoreEpoch=identity.restoreEpoch,
        octopVersion=__version__,
        serverTime=datetime.now(UTC),
        timezone=services.config.default_timezone,
        capabilities=ProjectTaskFeatures(),
        executionAvailable=worker_ready and model_ready,
        unavailableReason=(
            "runtimeUnavailable"
            if not worker_ready
            else "modelUnavailable"
            if not model_ready
            else None
        ),
    )
