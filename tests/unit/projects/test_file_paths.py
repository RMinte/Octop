"""Host workspace hints must not be repeated beneath the virtual task root."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from octop_harness.backends import resolve_backend

from octop.infra.projects.context import task_backend
from octop.infra.projects.file_paths import TaskFilePathMiddleware


def request(name, **arguments):
    return ToolCallRequest(
        tool_call={"name": name, "args": arguments, "id": "call", "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("read_file", "file_path"),
        ("write_file", "file_path"),
        ("edit_file", "file_path"),
        ("delete", "file_path"),
        ("ls", "path"),
        ("glob", "path"),
        ("grep", "path"),
    ],
)
async def test_only_workspace_prefix_is_rewritten(tmp_path, name, field):
    middleware = TaskFilePathMiddleware(tmp_path, "en")
    original = request(
        name, **{field: str(tmp_path / "nested" / "proof.txt"), "pattern": "../text"}
    )
    handler = AsyncMock(return_value=ToolMessage(content="ok", tool_call_id="call"))
    await middleware.awrap_tool_call(original, handler)
    actual = handler.call_args.args[0].tool_call["args"]
    assert actual[field] == "/nested/proof.txt"
    assert actual["pattern"] == "../text"
    assert original.tool_call["args"][field] == str(tmp_path / "nested" / "proof.txt")


@pytest.mark.parametrize(
    "path", ["/outside/file.txt", "/workspace-other/file.txt", "proof.txt", "/", "."]
)
async def test_other_virtual_and_relative_paths_keep_their_meaning(path):
    middleware = TaskFilePathMiddleware("/workspace")
    handler = AsyncMock()
    await middleware.awrap_tool_call(request("read_file", file_path=path), handler)
    assert handler.call_args.args[0].tool_call["args"]["file_path"] == path


@pytest.mark.parametrize("path", ["/workspace/../other", "/outside/../file", "../file", r"..\file"])
async def test_parent_traversal_is_denied_without_dispatch(path):
    handler = AsyncMock()
    result = await TaskFilePathMiddleware("/workspace").awrap_tool_call(
        request("write_file", file_path=path, content="data"), handler
    )
    assert result.status == "error"
    assert result.tool_call_id == "call"
    handler.assert_not_awaited()


@pytest.mark.parametrize("name", ["execute", "client__write_file", "mcp_write", "plugin_write"])
async def test_other_tools_are_not_reinterpreted(tmp_path, name):
    original = request(name, file_path=str(tmp_path / "file"), command="../command")
    handler = AsyncMock()
    await TaskFilePathMiddleware(tmp_path).awrap_tool_call(original, handler)
    assert handler.call_args.args[0] is original


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [
        ("/workspace", "/workspace", "/"),
        ("C:/Project", r"c:\project\nested\proof.txt", "/nested/proof.txt"),
        ("C:/Project", "/outside/proof.txt", "/outside/proof.txt"),
        ("C:/Project", "C:/Project-other/file.txt", "C:/Project-other/file.txt"),
    ],
)
async def test_native_root_boundaries_and_windows_paths(root, path, expected):
    handler = AsyncMock()
    await TaskFilePathMiddleware(root).awrap_tool_call(request("ls", path=path), handler)
    assert handler.call_args.args[0].tool_call["args"]["path"] == expected


async def test_actual_task_backend_writes_to_project_once(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    backend = resolve_backend(task_backend(project, tmp_path / "runtime"), workspace_dir=project)

    async def write(actual):
        result = await backend.awrite(**actual.tool_call["args"])
        assert result.error is None
        return ToolMessage(content="written", tool_call_id="call")

    middleware = TaskFilePathMiddleware(project)
    await middleware.awrap_tool_call(
        request("write_file", file_path=str(project / "model-proof.txt"), content="proof"), write
    )
    await middleware.awrap_tool_call(
        request("write_file", file_path="/outside/virtual.txt", content="virtual"), write
    )
    assert (project / "model-proof.txt").read_text() == "proof"
    assert (project / "outside" / "virtual.txt").read_text() == "virtual"
    assert sorted(
        path.relative_to(project).as_posix() for path in project.rglob("*") if path.is_file()
    ) == [
        "model-proof.txt",
        "outside/virtual.txt",
    ]
