"""File receipts compare observed workspace contents, never proposed edits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.messages import ToolMessage, message_to_dict, messages_from_dict
from langgraph.prebuilt.tool_node import ToolCallRequest
from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.projects import execution, file_changes
from octop.infra.projects.content import TaskContent
from octop.infra.projects.execution import TaskExecution
from octop.infra.projects.file_changes import (
    TaskFileChange,
    change_path,
    changes,
    recorded_file_changes,
    snapshot,
)


@pytest.fixture
def workspace(tmp_path):
    return BackendWorkspace(
        FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True),
        tmp_path,
        system_files_path=".octop",
    )


@pytest.fixture
def middleware(monkeypatch, workspace):
    events = []
    monkeypatch.setattr(execution, "get_config", lambda: {"configurable": {"thread_id": "turn"}})
    monkeypatch.setattr(execution, "get_stream_writer", lambda: events.append)
    planning = SimpleNamespace(phase="unplanned", binding=lambda step_id=None: (None, None))
    executor = TaskExecution(planning, "en", "turn")
    executor.workspace = workspace
    return executor, events


def request(name, call_id="call", **arguments):
    return ToolCallRequest(
        tool_call={"name": name, "args": arguments, "id": call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


async def test_new_edit_replace_all_and_delete_use_actual_text(workspace, middleware):
    executor, events = middleware

    async def write(actual):
        await workspace.awrite_text("notes.txt", "old\nkeep\nold\n")
        return ToolMessage(content="Updated file", tool_call_id=actual.tool_call["id"])

    await executor.run(
        request("write_file", file_path="/notes.txt", content="old\nkeep\nold\n"), write
    )
    assert events[-1]["file_changes"][0]["additions"] == 3
    assert events[-1]["file_changes"][0]["deletions"] == 0

    async def edit(actual):
        text = await workspace.aread_text("notes.txt")
        await workspace.awrite_text("notes.txt", text.replace("old", "new\nextra"))
        return ToolMessage(content="Updated file", tool_call_id=actual.tool_call["id"])

    result = await executor.run(
        request(
            "edit_file",
            "edit",
            file_path="/notes.txt",
            old_string="old",
            new_string="new\nextra",
            replace_all=True,
        ),
        edit,
    )
    receipt = recorded_file_changes(result)[0]
    assert (receipt.path, receipt.additions, receipt.deletions) == ("notes.txt", 4, 2)
    assert "+extra\n" in receipt.diff and " keep\n" in receipt.diff
    assert "octop_task_file_changes" not in result.additional_kwargs

    async def delete(actual):
        await workspace.adelete("notes.txt")
        return ToolMessage(content="Deleted", tool_call_id=actual.tool_call["id"])

    await executor.run(request("delete", "delete", file_path="/notes.txt"), delete)
    assert events[-1]["file_changes"][0]["deletions"] == 5
    assert events[-1]["file_changes"][0]["additions"] == 0


@pytest.mark.parametrize("initial,final", [(None, ""), ("", None)])
async def test_empty_file_existence_changes_are_not_lost(workspace, initial, final):
    if initial is not None:
        await workspace.awrite_text("empty.txt", initial)
    before = await snapshot(workspace, "empty.txt")
    if final is None:
        await workspace.adelete("empty.txt")
    else:
        await workspace.awrite_text("empty.txt", final)
    assert await changes(workspace, before) == [
        TaskFileChange(path="empty.txt", additions=0, deletions=0, diff="")
    ]


async def test_failed_and_unchanged_tools_have_no_file_receipt(workspace, middleware):
    executor, events = middleware
    await workspace.awrite_text("notes.txt", "unchanged\n")
    failed = AsyncMock(
        return_value=ToolMessage(content="No match", status="error", tool_call_id="bad")
    )
    await executor.run(request("edit_file", "bad", file_path="/notes.txt"), failed)
    assert events[-1]["failed"] is True and events[-1]["file_changes"] == []
    unchanged = AsyncMock(return_value=ToolMessage(content="Unchanged", tool_call_id="same"))
    await executor.run(request("edit_file", "same", file_path="/notes.txt"), unchanged)
    assert events[-1]["file_changes"] == []


async def test_unreadable_binary_large_and_system_paths_keep_unknown_counts(workspace, monkeypatch):
    for path, text in [
        ("binary", "a\x00b"),
        ("large", "x" * (file_changes.MAX_SNAPSHOT_CHARACTERS + 1)),
    ]:
        await workspace.awrite_text(path, text)
        before = await snapshot(workspace, path)
        assert before.text is None
        assert await changes(workspace, before) == [TaskFileChange(path=path)]
    monkeypatch.setattr(workspace, "aexists", AsyncMock(side_effect=PermissionError))
    assert (await snapshot(workspace, "unreadable")).text is None
    assert (await snapshot(workspace, "skills/foo/SKILL.md")).text is None
    assert (await snapshot(workspace, "notes.txt ")).text is None
    assert workspace.aexists.await_count == 1


@pytest.mark.parametrize(
    "path", ["/.octop/task/token", "inbound/file", "../outside", "/", "~/secret", "..\\outside"]
)
def test_internal_and_escaping_paths_are_not_observed(path):
    assert change_path("write_file", {"file_path": path}) is None


async def test_same_file_parallel_writes_have_separate_observed_diffs(workspace, middleware):
    executor, events = middleware
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(actual):
        if actual.tool_call["id"] == "first":
            entered.set()
            await release.wait()
        await workspace.awrite_text("notes.txt", actual.tool_call["args"]["content"])
        return ToolMessage(content="Updated", tool_call_id=actual.tool_call["id"])

    first = asyncio.create_task(
        executor.run(
            request("write_file", "first", file_path="/notes.txt", content="first\n"), write
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(
        executor.run(
            request("write_file", "second", file_path="notes.txt", content="second\n"), write
        )
    )
    release.set()
    await asyncio.gather(first, second)
    receipts = [event["file_changes"][0] for event in events if event["type"] == "task_tool_end"]
    assert [(item["additions"], item["deletions"]) for item in receipts] == [(1, 0), (1, 1)]


async def test_receipt_artifact_and_content_restore_preserve_diff_before_graph_returns(
    workspace, middleware
):
    executor, events = middleware
    saved = []

    class Receipts:
        async def run(self, actual, handler):
            result = await handler(actual)
            saved.append(message_to_dict(result))
            return result

    async def write(actual):
        await workspace.awrite_text("saved.txt", "written\n")
        return ToolMessage(content="Updated", tool_call_id="call")

    await executor.run(
        request("write_file", file_path="/saved.txt", content="written\n"),
        write,
        receipts=Receipts(),
    )
    restored_message = messages_from_dict(saved)[0]
    assert recorded_file_changes(restored_message)[0].additions == 1
    content = TaskContent("turn")
    content.start_tool("call", "write_file")
    content.end_tool("call", file_changes=events[-1]["file_changes"])
    restored = TaskContent.restore("turn", content.dump(), content.revision)
    assert restored.blocks[0].file_changes == recorded_file_changes(restored_message)
    assert (
        restored.blocks[0].file_changes[0].diff
        == "--- a/saved.txt\n+++ b/saved.txt\n@@ -0,0 +1 @@\n+written\n"
    )
