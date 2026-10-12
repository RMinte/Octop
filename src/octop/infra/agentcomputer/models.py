"""Typed command and immutable acceptance records for the control-plane kernel.

Turn submission is internal only until project provisioning and engine ingress
are implemented. Defining its types does not enable an HTTP execution route.
"""

from __future__ import annotations

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
    instructions: StrictStr
    modelRef: Annotated[StrictStr, Field(min_length=1)]


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
