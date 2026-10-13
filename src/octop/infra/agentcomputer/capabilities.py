"""Truthful discovery for the AgentComputer protocol foundation."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from octop import __version__
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.limits import MAX_COMMAND_BYTES
from octop.infra.db.services import SharedServices

ADAPTER_VERSION = "0.8.0"
PROTOCOL_VERSION: Literal[1] = 1


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityStatus(ProtocolModel):
    supported: bool
    available: bool
    reasonCode: str | None = None

    @model_validator(mode="after")
    def _valid_status(self) -> Self:
        if self.available and not self.supported:
            raise ValueError("an available capability must be supported")
        if not self.available and (self.reasonCode is None or not self.reasonCode.strip()):
            raise ValueError("an unavailable capability requires a reason")
        if self.available and self.reasonCode is not None:
            raise ValueError("an available capability cannot have an unavailable reason")
        return self


class BusinessCapabilities(ProtocolModel):
    projects: CapabilityStatus = Field(
        description="Full project readiness; local intent registration alone is not execution readiness."
    )
    tasks: CapabilityStatus = Field(
        description="Full task workflow; metadata queries alone do not enable creation or execution."
    )
    commandReceipts: CapabilityStatus = Field(
        description="Queries for existing immutable receipts; this flag never grants execution."
    )
    runs: CapabilityStatus = Field(
        description="Full Run execution; persisted dispatch facts and app intake receipts do not enable production execution."
    )
    cancellation: CapabilityStatus
    snapshots: CapabilityStatus
    events: CapabilityStatus
    files: CapabilityStatus
    approvals: CapabilityStatus
    questions: CapabilityStatus
    artifacts: CapabilityStatus
    terminal: CapabilityStatus
    git: CapabilityStatus
    plans: CapabilityStatus
    schedules: CapabilityStatus
    preview: CapabilityStatus
    deviceBinding: CapabilityStatus
    recovery: CapabilityStatus


class ServiceLimits(ProtocolModel):
    maxCommandBytes: Annotated[int | None, Field(ge=1)] = None
    maxPromptCharacters: Annotated[int | None, Field(ge=1)] = None
    maxUploadBytes: Annotated[int | None, Field(ge=1)] = None
    maxArtifactBytes: Annotated[int | None, Field(ge=1)] = None
    maxArtifactsPerTurn: Annotated[int | None, Field(ge=1)] = None
    maxTurnArtifactBytes: Annotated[int | None, Field(ge=1)] = None
    maxEventPageSize: Annotated[int | None, Field(ge=1)] = None
    maxWorkspacePageSize: Annotated[int | None, Field(ge=1)] = None
    maxQueuedRuns: Annotated[int | None, Field(ge=1)] = None
    maxConcurrentRuns: Annotated[int | None, Field(ge=1)] = None


class ServerBuild(ProtocolModel):
    octopVersion: str
    adapterVersion: str
    imageDigest: str | None = None


class ExecutionPlatform(ProtocolModel):
    system: str
    architecture: str


class AgentComputerCapabilitiesResponse(ProtocolModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "boxID": "11111111-1111-4111-8111-111111111111",
                    "deviceID": None,
                    "authenticationMode": "octopUserSession",
                    "protocolVersion": 1,
                    "minimumClientProtocolVersion": 1,
                    "serverBuild": {
                        "octopVersion": __version__,
                        "adapterVersion": ADAPTER_VERSION,
                        "imageDigest": None,
                    },
                    "serverTime": "2026-10-12T00:00:00Z",
                    "timezone": "Asia/Shanghai",
                    "executionPlatform": {"system": "Darwin", "architecture": "arm64"},
                    "capabilityRevision": 8,
                    "restoreEpoch": 1,
                    "supportedPermissionModes": [],
                    "capabilities": {
                        name: (
                            {"supported": True, "available": True, "reasonCode": None}
                            if name == "commandReceipts"
                            else {
                                "supported": True,
                                "available": False,
                                "reasonCode": "executionUnavailable",
                            }
                            if name in {"projects", "tasks", "runs"}
                            else {
                                "supported": False,
                                "available": False,
                                "reasonCode": "notImplemented",
                            }
                        )
                        for name in BusinessCapabilities.model_fields
                    },
                    "limits": {
                        **dict.fromkeys(ServiceLimits.model_fields),
                        "maxCommandBytes": MAX_COMMAND_BYTES,
                    },
                }
            ]
        }
    )

    boxID: UUID
    deviceID: UUID | None = Field(
        default=None, description="Bound device ID; null for user sessions."
    )
    authenticationMode: Literal["octopUserSession"] = Field(
        default="octopUserSession", description="User authentication does not bind a client device."
    )
    protocolVersion: Literal[1] = PROTOCOL_VERSION
    minimumClientProtocolVersion: Literal[1] = PROTOCOL_VERSION
    serverBuild: ServerBuild
    serverTime: datetime
    timezone: str
    executionPlatform: ExecutionPlatform
    capabilityRevision: Literal[8] = 8
    restoreEpoch: Annotated[int, Field(strict=True, ge=1)] = Field(
        description="Persisted initial epoch; restore fencing remains unavailable."
    )
    supportedPermissionModes: list[Literal["askForApproval", "approveForMe", "fullAccess"]]
    capabilities: BusinessCapabilities
    limits: ServiceLimits = Field(description="Null limits are unconfigured, never unlimited.")


def get_capabilities(services: SharedServices) -> AgentComputerCapabilitiesResponse:
    """Read installation identity and advertise only implemented protocol abilities."""
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    unavailable = CapabilityStatus(supported=False, available=False, reasonCode="notImplemented")
    return AgentComputerCapabilitiesResponse(
        boxID=identity.boxID,
        serverBuild=ServerBuild(octopVersion=__version__, adapterVersion=ADAPTER_VERSION),
        serverTime=datetime.now(UTC),
        timezone=services.config.default_timezone,
        executionPlatform=ExecutionPlatform(
            system=platform.system(), architecture=platform.machine()
        ),
        restoreEpoch=identity.restoreEpoch,
        supportedPermissionModes=[],
        capabilities=BusinessCapabilities(
            projects=CapabilityStatus(
                supported=True, available=False, reasonCode="executionUnavailable"
            ),
            tasks=CapabilityStatus(
                supported=True, available=False, reasonCode="executionUnavailable"
            ),
            commandReceipts=CapabilityStatus(supported=True, available=True),
            runs=CapabilityStatus(
                supported=True, available=False, reasonCode="executionUnavailable"
            ),
            cancellation=unavailable,
            snapshots=unavailable,
            events=unavailable,
            files=unavailable,
            approvals=unavailable,
            questions=unavailable,
            artifacts=unavailable,
            terminal=unavailable,
            git=unavailable,
            plans=unavailable,
            schedules=unavailable,
            preview=unavailable,
            deviceBinding=unavailable,
            recovery=unavailable,
        ),
        limits=ServiceLimits(maxCommandBytes=MAX_COMMAND_BYTES),
    )
