"""Recovery classification keeps transport outages distinct from permanent errors."""

from __future__ import annotations

from urllib.error import HTTPError, URLError

import httpx
import pytest

from octop.infra.projects.recovery import TaskInterruption, diagnostic, transient
from octop.infra.projects.runtime import McpToolsUnavailable, SkillPreparationError
from octop.infra.skills.skillhub_market import SkillHubMarketTimeout, SkillHubPackageError


def httpx_error(status):
    request = httpx.Request("GET", "https://resource.example/private?token=credential")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("private response text", request=request, response=response)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private network details"),
        ConnectionError("private connection details"),
        httpx.ConnectTimeout("private connect timeout"),
        httpx.ReadError("private response details"),
        URLError("private DNS details"),
        SkillHubMarketTimeout("private SkillHub timeout"),
    ],
)
def test_transport_unavailability_is_recoverable(error):
    assert transient(error) is True


@pytest.mark.parametrize(
    ("status", "recoverable"),
    [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_httpx_status_preserves_permanent_client_errors_and_temporary_server_failures(
    status, recoverable
):
    error = httpx_error(status)
    assert transient(error) is recoverable
    result = diagnostic("skill_download", error, can_resume=recoverable)
    assert result.status_code == status
    assert result.cause_type == "HTTPStatusError"
    assert "credential" not in result.model_dump_json()
    assert "private response text" not in result.model_dump_json()


@pytest.mark.parametrize(("status", "recoverable"), [(404, False), (429, True), (503, True)])
def test_urllib_http_error_is_classified_by_status_before_generic_url_error(status, recoverable):
    error = HTTPError(
        "https://resource.example/private", status, "private HTTP message", None, None
    )
    assert transient(error) is recoverable
    result = diagnostic("skill_download", error, can_resume=recoverable)
    assert result.status_code == status
    assert result.cause_type == "HTTPError"
    assert "private" not in result.model_dump_json()


def test_skill_wrapper_keeps_exact_resource_and_only_the_deepest_sanitized_cause():
    underlying = httpx.ConnectTimeout("private bearer credential and connection URL")
    market = SkillHubMarketTimeout("private marketplace URL and response")
    market.__cause__ = underlying
    error = SkillPreparationError("recovery-notes")
    error.__cause__ = market
    assert transient(error) is True
    result = diagnostic(
        "skill_download",
        error,
        can_resume=True,
        reason_code="TASK_RESOURCE_UNAVAILABLE",
        resource_type="skill",
        resource_id=error.resource_id,
    )
    assert result.cause_type == "ConnectTimeout"
    assert result.resource_type == "skill" and result.resource_id == "recovery-notes"
    assert result.phase == "skill_download" and result.can_resume is True
    assert result.status_code is None
    assert result.diagnostic_id.startswith("diag_")
    encoded = TaskInterruption(result, "prepare").encode()
    assert "recovery-notes" in encoded
    assert all(text not in encoded for text in ("private", "bearer", "credential", "marketplace"))


def test_wrapped_http_failure_preserves_deepest_status_code_without_response_payload():
    error = SkillPreparationError("missing-notes")
    error.__cause__ = httpx_error(404)
    result = diagnostic(
        "skill_download", error, can_resume=transient(error), resource_id=error.resource_id
    )
    assert result.can_resume is False
    assert result.cause_type == "HTTPStatusError" and result.status_code == 404
    assert "credential" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("errors", "recoverable"),
    [
        ([TimeoutError(), ConnectionError()], True),
        ([TimeoutError(), httpx_error(503)], True),
        ([TimeoutError(), httpx_error(404)], False),
        ([ConnectionError(), SkillHubPackageError("invalid manifest")], False),
        ([ExceptionGroup("temporary", [TimeoutError(), ConnectionError()])], True),
        ([ExceptionGroup("mixed", [TimeoutError(), ValueError("invalid settings")])], False),
    ],
)
def test_exception_group_requires_every_failure_to_be_temporary(errors, recoverable):
    assert transient(ExceptionGroup("resource preparation failed", errors)) is recoverable


def test_invalid_skill_package_stays_permanent_even_with_a_nested_transport_error():
    error = SkillHubPackageError("manifest content failed validation")
    error.__cause__ = TimeoutError("an earlier download timeout")
    assert transient(error) is False


def test_mcp_discovery_failure_names_resource_without_inventing_an_underlying_exception():
    error = McpToolsUnavailable("selected-mcp")
    assert error.__cause__ is None and transient(error) is True
    result = diagnostic(
        "mcp_discovery", error, can_resume=True, resource_type="mcp", resource_id=error.resource_id
    )
    assert result.cause_type == "McpToolsUnavailable"
    assert result.resource_id == "selected-mcp" and result.status_code is None
