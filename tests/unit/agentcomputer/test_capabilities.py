"""Discovery must not imply that Octop features implement the new protocol."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from octop import __version__
from octop.config import OctopConfig
from octop.infra.agentcomputer.capabilities import CapabilityStatus, ServiceLimits, get_capabilities
from octop.infra.db.pool import DatabasePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


def test_capabilities_are_truthful_authenticated_discovery_data(tmp_path: Path) -> None:
    pool = Mock(spec=DatabasePool)
    services = build_shared_services(
        db=cast(DatabasePool, pool),
        paths=PathLayout(tmp_path),
        config=OctopConfig(default_timezone="Europe/London", max_upload_mb=100),
    )
    before = datetime.now(UTC)
    response = get_capabilities(services)
    after = datetime.now(UTC)
    data = response.model_dump(mode="json")

    assert response.deviceID is None
    assert response.authenticationMode == "octopUserSession"
    assert response.protocolVersion == response.minimumClientProtocolVersion == 1
    assert response.capabilityRevision == 8
    assert response.restoreEpoch == 1
    assert response.serverBuild.octopVersion == __version__
    assert response.serverBuild.adapterVersion == "0.8.0"
    assert response.serverBuild.imageDigest is None
    assert before <= response.serverTime <= after
    assert response.serverTime.tzinfo == UTC
    assert response.timezone == "Europe/London"
    assert response.executionPlatform.system == platform.system()
    assert response.executionPlatform.architecture == platform.machine()
    assert response.supportedPermissionModes == []
    assert data["deviceID"] is None
    assert data["serverBuild"]["imageDigest"] is None
    assert set(data["capabilities"]) == {
        "projects",
        "tasks",
        "commandReceipts",
        "runs",
        "cancellation",
        "snapshots",
        "events",
        "files",
        "approvals",
        "questions",
        "artifacts",
        "terminal",
        "git",
        "plans",
        "schedules",
        "preview",
        "deviceBinding",
        "recovery",
    }
    assert data["capabilities"]["commandReceipts"] == {
        "supported": True,
        "available": True,
        "reasonCode": None,
    }
    assert data["capabilities"]["projects"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }
    assert data["capabilities"]["tasks"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }
    assert data["capabilities"]["runs"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }
    assert all(
        value == {"supported": False, "available": False, "reasonCode": "notImplemented"}
        for name, value in data["capabilities"].items()
        if name not in {"commandReceipts", "projects", "tasks", "runs"}
    )
    assert data["limits"]["maxCommandBytes"] == 65536
    assert all(value is None for name, value in data["limits"].items() if name != "maxCommandBytes")
    assert pool.mock_calls == []


def test_new_services_keep_box_identity_and_use_current_timezone(tmp_path: Path) -> None:
    paths = PathLayout(tmp_path)
    first_pool = Mock(spec=DatabasePool)
    first = build_shared_services(
        db=cast(DatabasePool, first_pool), paths=paths, config=OctopConfig()
    )
    original = get_capabilities(first)
    identity_bytes = paths.agentcomputer_identity.read_bytes()
    second_pool = Mock(spec=DatabasePool)
    replacement = build_shared_services(
        db=cast(DatabasePool, second_pool),
        paths=paths,
        config=OctopConfig(default_timezone="UTC"),
    )

    current = get_capabilities(replacement)

    assert current.boxID == original.boxID
    assert current.restoreEpoch == original.restoreEpoch
    assert current.timezone == "UTC"
    assert paths.agentcomputer_identity.read_bytes() == identity_bytes
    assert first_pool.mock_calls == second_pool.mock_calls == []


@pytest.mark.parametrize(
    "data",
    [
        {"supported": False, "available": True},
        {"supported": False, "available": False},
        {"supported": True, "available": False, "reasonCode": ""},
        {"supported": True, "available": False, "reasonCode": " "},
        {"supported": True, "available": True, "reasonCode": "notImplemented"},
    ],
)
def test_capability_status_rejects_invalid_combinations(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CapabilityStatus.model_validate(data)


def test_supported_but_temporarily_unavailable_requires_reason() -> None:
    assert CapabilityStatus(supported=True, available=False, reasonCode="notReady").supported
    assert CapabilityStatus(supported=True, available=True).available


def test_unknown_limit_is_nullable_and_invalid_limit_is_rejected() -> None:
    assert ServiceLimits().maxCommandBytes is None
    with pytest.raises(ValidationError):
        ServiceLimits(maxCommandBytes=0)


def test_unknown_capability_fields_do_not_hide_schema_mismatch() -> None:
    with pytest.raises(ValidationError):
        CapabilityStatus.model_validate(
            {"supported": False, "available": False, "reasonCode": "notImplemented", "extra": True}
        )
