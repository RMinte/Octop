"""The only model-visible managed-project tool stays inside documents/."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from deepagents.backends.filesystem import FilesystemBackend
from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.agentcomputer.commands import CommandAdmissionError, content_digest
from octop.infra.agentcomputer.project_document_tool import (
    MAX_DOCUMENT_BYTES,
    document_read_action,
    execute_document_read,
    read_project_document,
    read_project_document_content,
)


@pytest.fixture
def bound(tmp_path: Path):
    root = tmp_path / "project-root"
    workspace_dir = root / ".octop" / "workspaces" / "managed-agent"
    workspace_dir.mkdir(parents=True)
    workspace = BackendWorkspace(
        FilesystemBackend(root_dir=root, virtual_mode=True),
        workspace_dir,
        system_files_path=".octop",
    )
    invocation = SimpleNamespace(
        intent=SimpleNamespace(
            ownerID=7,
            projectID=uuid4(),
            snapshot=SimpleNamespace(agentID="managed-agent", workspaceID=uuid4()),
        )
    )
    registry = SimpleNamespace(
        get_row=lambda agent_id: SimpleNamespace(user_id=7),
        workspace_for_project_preparation=lambda agent_id, project_id: workspace,
    )
    return registry, invocation, workspace


async def test_document_read_binds_workspace_and_reports_exact_text(bound):
    registry, invocation, workspace = bound
    workspace.write_text("documents/brief.md", "项目的第一条资料。", force=True)
    action = document_read_action(
        registry, invocation, tool_call_id="call-1", arguments={"path": "brief.md"}
    )
    result = await execute_document_read(registry, invocation, action)
    assert result.outcome == "succeeded"
    assert result.effects == "reported"
    assert result.fileChangeIDs == result.artifactIDs == ()
    assert result.resultDigest == content_digest(json.loads(result.resultJSON))
    assert json.loads(result.resultJSON) == {
        "path": "documents/brief.md",
        "status": "ok",
        "content": "项目的第一条资料。",
    }


async def test_document_read_ignores_virtual_root_shadow(bound):
    registry, invocation, workspace = bound
    workspace.write_text("documents/brief.md", "受管项目资料", force=True)
    root_shadow = Path(workspace.resolve_path("/")) / "documents" / "brief.md"
    root_shadow.parent.mkdir(parents=True)
    root_shadow.write_text("盒子根目录的同名文件", encoding="utf-8")
    action = document_read_action(
        registry, invocation, tool_call_id="call-1", arguments={"path": "brief.md"}
    )
    result = await execute_document_read(registry, invocation, action)
    assert json.loads(result.resultJSON)["content"] == "受管项目资料"


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "../secret.md"},
        {"path": "/tmp/secret.md"},
        {"path": "documents/brief.md"},
        {"path": "..\\secret.md"},
        {"path": ".octop.txt"},
        {"path": "brief.pdf"},
        {"path": "brief.md", "other": "x"},
        {"path": 123},
    ],
)
def test_document_read_rejects_unscoped_arguments(bound, arguments):
    registry, invocation, _ = bound
    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        document_read_action(registry, invocation, tool_call_id="call-1", arguments=arguments)


def test_transport_neutral_reader_revalidates_filename(bound):
    _, _, workspace = bound
    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        read_project_document_content(workspace, "../secret.md")


async def test_document_read_reports_missing_and_oversize_without_returning_content(bound):
    registry, invocation, workspace = bound
    missing = document_read_action(
        registry, invocation, tool_call_id="call-1", arguments={"path": "missing.txt"}
    )
    missing_result = await execute_document_read(registry, invocation, missing)
    assert json.loads(missing_result.resultJSON)["status"] == "notFound"
    assert json.loads(missing_result.resultJSON)["content"] is None

    workspace.write_text("documents/long.txt", "x" * (MAX_DOCUMENT_BYTES + 1), force=True)
    long = document_read_action(
        registry, invocation, tool_call_id="call-2", arguments={"path": "long.txt"}
    )
    long_result = await execute_document_read(registry, invocation, long)
    assert json.loads(long_result.resultJSON)["status"] == "tooLarge"
    assert json.loads(long_result.resultJSON)["content"] is None


@pytest.mark.skipif(__import__("os").name != "posix", reason="symlink semantics are POSIX-only")
def test_document_read_rejects_symlink_to_outside_workspace(bound, tmp_path: Path):
    registry, invocation, workspace = bound
    documents = Path(workspace.workspace_dir) / "documents"
    documents.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("private", encoding="utf-8")
    (documents / "link.txt").symlink_to(outside)
    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        document_read_action(
            registry, invocation, tool_call_id="call-1", arguments={"path": "link.txt"}
        )


def test_unjournaled_model_visible_tool_is_inert():
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable"):
        read_project_document.func("brief.md")
