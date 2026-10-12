"""Legacy ingress must not mutate or execute AgentComputer-owned experts."""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError


def is_managed_agent(row: Any) -> bool:
    return getattr(row, "managed_by", None) == "agentcomputer"


def assert_legacy_agent_access(row: Any) -> None:
    if is_managed_agent(row):
        raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "managedAgent"})
