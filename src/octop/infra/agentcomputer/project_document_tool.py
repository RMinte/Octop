"""One journal-only, read-only tool for managed project documents.

The model-visible LangChain tool is deliberately inert. EngineEntryMiddleware
must first journal a validated call, then invoke ``execute_document_read``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.models import EngineInvocation, ToolEffectAction, ToolEffectOutput

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

    from octop.infra.agents.manager import AgentManager

TOOL_NAME = "read_project_document"
CAPABILITY_VERSION = "read-project-document-v1"
POLICY_VERSION = "project-documents-read-only-v1"
MAX_DOCUMENT_BYTES = 16_384
MAX_FILENAME_LENGTH = 128


@tool
def read_project_document(path: str) -> str:
    """Read one UTF-8 text file from this project's documents directory.

    Args:
        path: Filename under documents/, such as brief.md or notes.txt. Do not
            pass a directory, absolute path, URL, or a documents/ prefix.
    """
    # The bound middleware performs the read only after its durable effect CAS.
    raise CommandAdmissionError("runtimeUnavailable", reason="toolExecutionUnavailable")


def _filename(arguments: object) -> str:
    if not isinstance(arguments, dict) or set(arguments) != {"path"}:
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed")
    return validate_document_filename(arguments["path"])


def validate_document_filename(path: object) -> str:
    """Validate the single filename shared by import and model-read paths."""
    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= MAX_FILENAME_LENGTH
        or path != path.strip()
        or path.startswith(".")
        or not path.endswith((".md", ".txt"))
        or "/" in path
        or "\\" in path
        or ":" in path
        or not all(character.isalnum() or character in "._-" for character in path)
    ):
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed")
    return path


def _workspace(registry: AgentManager, invocation: EngineInvocation) -> BackendWorkspace:
    intent = invocation.intent
    if intent.projectID is None:
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed")
    row = registry.get_row(intent.snapshot.agentID)
    if row is None or row.user_id != intent.ownerID:
        raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
    return registry.workspace_for_project_preparation(
        intent.snapshot.agentID, str(intent.projectID)
    )


def document_workspace_path(workspace: BackendWorkspace, filename: str) -> Path:
    """Check the exact local document; workspace resolution rejects escapes."""
    filename = validate_document_filename(filename)
    root = Path(workspace.workspace_dir).resolve()
    raw_dir = Path(workspace.workspace_dir) / "documents"
    raw_file = raw_dir / filename
    if raw_dir.is_symlink() or raw_file.is_symlink():
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed")
    try:
        directory = Path(workspace.resolve_path("documents"))
        candidate = Path(workspace.resolve_path(f"documents/{filename}"))
    except (OSError, PermissionError, ValueError) as exc:
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed") from exc
    if directory.parent != root or candidate.parent != directory:
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed")
    return candidate


def document_workspace_key(workspace: BackendWorkspace, filename: str) -> str:
    """Address only the managed workspace file, not virtual-root fallbacks."""
    candidate = document_workspace_path(workspace, filename)
    try:
        root = Path(workspace.resolve_path("/"))
        key = "/" + candidate.relative_to(root).as_posix()
        if Path(workspace.resolve_path(key)) != candidate:
            raise ValueError("document backend key mismatch")
    except (OSError, PermissionError, ValueError) as exc:
        raise CommandAdmissionError("accessDenied", reason="documentPathNotAllowed") from exc
    return key


def _workspace_digest(workspace: BackendWorkspace, invocation: EngineInvocation) -> str:
    intent = invocation.intent
    return content_digest(
        {
            "schemaVersion": 1,
            "workspaceID": str(intent.snapshot.workspaceID),
            "projectID": str(intent.projectID),
            "agentID": intent.snapshot.agentID,
            "workspaceDir": str(Path(workspace.workspace_dir).resolve()),
            "scope": "documents",
        }
    )


def document_read_action(
    registry: AgentManager,
    invocation: EngineInvocation,
    *,
    tool_call_id: str,
    arguments: object,
) -> ToolEffectAction:
    """Validate a model call and bind it to the registered project workspace."""
    if not isinstance(tool_call_id, str) or not 1 <= len(tool_call_id) <= 256:
        raise CommandAdmissionError("accessDenied", reason="invalidToolCall")
    filename = _filename(arguments)
    workspace = _workspace(registry, invocation)
    document_workspace_path(workspace, filename)
    payload = {"path": filename}
    return ToolEffectAction(
        schemaVersion=1,
        executionAttempt=1,
        toolCallID=tool_call_id,
        toolName=TOOL_NAME,
        argumentsJSON=canonical_json(payload),
        argumentsDigest=content_digest(payload),
        capabilityVersion=CAPABILITY_VERSION,
        policyVersion=POLICY_VERSION,
        workspaceDigest=_workspace_digest(workspace, invocation),
    )


def read_project_document_content(workspace: BackendWorkspace, filename: str) -> dict[str, Any]:
    """Read one validated document with a bounded, transport-neutral status."""
    path = document_workspace_path(workspace, filename)
    key = document_workspace_key(workspace, filename)
    result: dict[str, Any] = {
        "path": f"documents/{filename}",
        "status": "notFound",
        "content": None,
    }
    try:
        if not path.is_file():
            return result
        file_size = path.stat().st_size
    except OSError:
        result["status"] = "unreadable"
        return result
    if file_size > MAX_DOCUMENT_BYTES:
        result["status"] = "tooLarge"
        return result
    content = workspace.read_text(key, limit=MAX_DOCUMENT_BYTES + 1)
    if content is None:
        result["status"] = "unreadable"
    elif len(content.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        result["status"] = "tooLarge"
    else:
        result["status"] = "ok"
        result["content"] = content
    return result


async def execute_document_read(
    registry: AgentManager,
    invocation: EngineInvocation,
    action: ToolEffectAction,
) -> ToolEffectOutput:
    """Run the bounded read inside an already-dispatched effect handler."""
    try:
        arguments = json.loads(action.argumentsJSON)
    except (TypeError, ValueError) as exc:
        raise CommandAdmissionError("accessDenied", reason="invalidToolCall") from exc
    filename = _filename(arguments)
    workspace = await asyncio.to_thread(_workspace, registry, invocation)
    if (
        action.toolName != TOOL_NAME
        or action.executionAttempt != 1
        or action.capabilityVersion != CAPABILITY_VERSION
        or action.policyVersion != POLICY_VERSION
        or action.argumentsJSON != canonical_json({"path": filename})
        or action.argumentsDigest != content_digest({"path": filename})
        or action.workspaceDigest != _workspace_digest(workspace, invocation)
    ):
        raise CommandAdmissionError("accessDenied", reason="invalidToolCall")
    result = await asyncio.to_thread(read_project_document_content, workspace, filename)
    return ToolEffectOutput(
        schemaVersion=1,
        outcome="succeeded",
        resultJSON=canonical_json(result),
        resultDigest=content_digest(result),
        exitCode=None,
        fileChangeIDs=(),
        artifactIDs=(),
        effects="reported",
    )
