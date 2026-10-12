"""Typed command and immutable acceptance records for the control-plane kernel.

Turn submission is internal only until project provisioning and engine ingress
are implemented. Defining its types does not enable an HTTP execution route.
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

    @field_validator("accepted", mode="before", check_fields=False)
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
    commandID: UUID
    idempotencyKey: Annotated[StrictStr, Field(min_length=1)]
    expectedRestoreEpoch: PositiveRevision
    contextMode: Literal["newMessage"]
    userMessageID: UUID
    expectedProjectRevision: PositiveRevision
    prompt: Annotated[StrictStr, Field(min_length=1)]
    inputArtifactIDs: tuple[UUID, ...] = ()
    selectedSkillIDs: tuple[StrictStr, ...] = ()
    permissionMode: PermissionMode


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
