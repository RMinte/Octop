"""Typed command and immutable acceptance records for the control-plane kernel.

Turn registration and execution availability are separate facts. The HTTP
ingress remains gated until managed engine dispatch is implemented.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

PositiveRevision = Annotated[StrictInt, Field(ge=1)]
PermissionMode = Literal["askForApproval", "approveForMe", "fullAccess"]


class ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator(
        "schemaVersion", "protocolVersion", "receiptRevision", mode="before", check_fields=False
    )
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value

    @field_validator("accepted", "executionReady", mode="before", check_fields=False)
    @classmethod
    def boolean_acceptance(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("accepted must be a boolean")
        return value


class ProjectConfiguration(ControlModel):
    """Small fixed configuration used by the initial admission kernel."""

    schemaVersion: Literal[1] = 1
    instructions: Annotated[StrictStr, Field(max_length=8192)]
    modelRef: Annotated[StrictStr, Field(min_length=1, max_length=256)]


class NewMessageCommand(ControlModel):
    commandID: UUID = Field(description="Stable command identity for response-loss retries.")
    idempotencyKey: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    expectedRestoreEpoch: PositiveRevision
    contextMode: Literal["newMessage"]
    userMessageID: UUID
    expectedProjectRevision: PositiveRevision
    prompt: Annotated[StrictStr, Field(min_length=1)] = Field(
        description="Exact user text; the command body is limited to 65,536 UTF-8 bytes."
    )
    inputArtifactIDs: tuple[UUID, ...] = ()
    selectedSkillIDs: tuple[StrictStr, ...] = ()
    permissionMode: PermissionMode


class NewMessageRequestSnapshot(ControlModel):
    """All original business fields are required in a persisted snapshot."""

    expectedRestoreEpoch: PositiveRevision
    contextMode: Literal["newMessage"]
    userMessageID: UUID
    expectedProjectRevision: PositiveRevision
    prompt: Annotated[StrictStr, Field(min_length=1)]
    inputArtifactIDs: tuple[UUID, ...]
    selectedSkillIDs: tuple[StrictStr, ...]
    permissionMode: PermissionMode


class TurnSnapshot(ControlModel):
    """Original input/configuration facts, never filled from current state."""

    schemaVersion: Literal[1]
    projectID: UUID
    projectRevision: PositiveRevision
    workspaceID: UUID
    agentID: StrictStr
    octopThreadID: StrictStr
    projectConfiguration: ProjectConfiguration
    request: NewMessageRequestSnapshot

    @field_validator("projectConfiguration")
    @classmethod
    def complete_configuration(cls, value: ProjectConfiguration) -> ProjectConfiguration:
        if value.model_fields_set != set(ProjectConfiguration.model_fields):
            raise ValueError("incomplete original configuration")
        return value


class CommandReceipt(ControlModel):
    """The committed acceptance result, not a live Run status projection."""

    protocolVersion: Literal[1] = Field(default=1, description="Acceptance protocol version.")
    boxID: UUID = Field(description="Control-plane identity that accepted the command.")
    commandID: UUID = Field(description="Original command ID; retries can return this earlier ID.")
    receiptRevision: Literal[1] = Field(
        default=1, description="Immutable acceptance schema revision."
    )
    accepted: Literal[True] = Field(default=True, description="Registration committed durably.")
    replayed: StrictBool = Field(
        default=False, description="This response reused an existing receipt."
    )
    acceptedAt: datetime = Field(description="Original UTC acceptance time; unchanged by querying.")
    restoreEpoch: PositiveRevision = Field(
        description="Epoch when accepted; historical facts retain it."
    )
    taskID: UUID = Field(description="Task owning the registered Turn and Run.")
    turnID: UUID = Field(description="Durably registered Turn identity.")
    runID: UUID = Field(description="Durably registered Run identity.")
    userMessageID: UUID = Field(
        description="Stable user message identity registered once per Task."
    )
    assistantMessageID: UUID = Field(description="Reserved first assistant message identity.")
    runState: Literal["queued"] = Field(
        default="queued", description="Run state at acceptance only."
    )
    taskRevision: PositiveRevision = Field(description="Task revision committed with acceptance.")
    acceptedSequence: PositiveRevision = Field(
        description="Task event sequence committed with acceptance."
    )

    @field_validator("acceptedAt")
    @classmethod
    def utc_acceptance_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("acceptedAt must be UTC")
        return value


class ProjectCreateCommand(ControlModel):
    commandID: UUID = Field(description="Persisted command identity for retries.")
    idempotencyKey: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    expectedRestoreEpoch: PositiveRevision
    projectID: UUID = Field(description="Client's stable project UUID.")
    name: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    configuration: ProjectConfiguration

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("name must contain printable text")
        return value


class ProjectCreateReceipt(ControlModel):
    protocolVersion: Literal[1] = Field(default=1, description="Acceptance protocol version.")
    operation: Literal["project.create"] = "project.create"
    boxID: UUID
    commandID: UUID
    receiptRevision: Literal[1] = 1
    accepted: Literal[True] = Field(default=True, description="Project intent committed durably.")
    replayed: StrictBool = False
    acceptedAt: datetime = Field(description="Original UTC registration time.")
    restoreEpoch: PositiveRevision
    projectID: UUID
    workspaceID: UUID
    preparationID: UUID
    projectRevision: PositiveRevision
    preparationStateAtAcceptance: Literal["pending"] = Field(
        default="pending", description="Immutable original state; query the project for progress."
    )

    @field_validator("acceptedAt")
    @classmethod
    def utc_acceptance_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("acceptedAt must be UTC")
        return value


class ProjectPreparationIntent(ControlModel):
    schemaVersion: Literal[1]
    boxID: UUID
    projectID: UUID
    workspaceID: UUID
    ownerID: PositiveRevision
    plannedAgentID: StrictStr
    serverRoot: StrictStr
    configurationDigest: StrictStr


class PreparationSteps(ControlModel):
    agent: Literal["pending", "verified"]
    workspace: Literal["pending", "verified"]


class ProjectView(ControlModel):
    projectID: UUID
    workspaceID: UUID
    preparationID: UUID
    name: str
    projectRevision: PositiveRevision
    configDigest: str
    preparationRevision: PositiveRevision
    preparationState: Literal["pending", "preparing", "ready", "failed"]
    steps: PreparationSteps
    reasonCode: str | None
    retryable: StrictBool
    executionReady: Literal[False] = Field(
        default=False, description="Engine ingress and execution validation remain unavailable."
    )


class TaskCreateCommand(ControlModel):
    """Internal mapping registration; no HTTP creation or engine ingress yet."""

    commandID: UUID
    idempotencyKey: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    expectedRestoreEpoch: PositiveRevision
    expectedProjectRevision: PositiveRevision
    projectID: UUID
    taskID: UUID
    title: Annotated[StrictStr, Field(min_length=1, max_length=256)]

    @field_validator("title")
    @classmethod
    def plain_title(cls, value: str) -> str:
        if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("title must contain printable text")
        return value


class TaskCreateReceipt(ControlModel):
    """Immutable completed database mapping, without a model execution claim."""

    protocolVersion: Literal[1] = 1
    operation: Literal["task.create"] = "task.create"
    boxID: UUID
    commandID: UUID
    receiptRevision: Literal[1] = 1
    accepted: Literal[True] = True
    replayed: StrictBool = False
    acceptedAt: datetime
    restoreEpoch: PositiveRevision
    projectID: UUID
    taskID: UUID
    sessionID: UUID = Field(description="Server-assigned public session UUID; never a file path.")
    taskRevision: PositiveRevision
    preparationStateAtAcceptance: Literal["ready"] = Field(
        default="ready", description="Task/thread/session mapping was committed atomically."
    )
    executionReady: Literal[False] = Field(
        default=False, description="A ready mapping does not enable engine execution."
    )

    @field_validator("acceptedAt")
    @classmethod
    def utc_acceptance_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("acceptedAt must be UTC")
        return value


class TaskView(ControlModel):
    taskID: UUID
    projectID: UUID
    sessionID: UUID
    title: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    taskRevision: PositiveRevision
    preparationState: Literal["pending", "preparing", "ready", "failed"]
    executionReady: Literal[False] = Field(
        default=False, description="Engine ingress remains unavailable."
    )


class RunView(ControlModel):
    """Current persisted Run state, separate from its immutable queued receipt."""

    runID: UUID
    turnID: UUID
    taskID: UUID
    projectID: UUID
    state: Literal[
        "queued",
        "running",
        "waitingApproval",
        "waitingInput",
        "succeeded",
        "failed",
        "stopped",
        "interrupted",
    ] = Field(description="Current persisted state; acceptance alone does not start execution.")
    dispatchPhase: Literal["notDispatched", "claimed", "dispatching", "engineAccepted"] = Field(
        description="Persisted dispatch fact; notDispatched means no engine handoff."
    )
    executionEpoch: Annotated[StrictInt, Field(ge=0)]
    restoreEpoch: PositiveRevision
    revision: PositiveRevision
    reconciliationRequired: StrictBool
    createdAt: datetime
    updatedAt: datetime

    @field_validator("createdAt", "updatedAt")
    @classmethod
    def utc_run_time(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("Run timestamps must be UTC")
        return value
