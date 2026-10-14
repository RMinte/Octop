"""A model-requested document read can finish only with one bound journal receipt."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from tests.unit.agentcomputer.test_engine_entry import _open_fixture_gate
from tests.unit.agentcomputer.test_engine_entry import entry as entry
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer import dispatch, effects
from octop.infra.agentcomputer.commands import CommandAdmissionError
from octop.infra.agentcomputer.completions import ToolCompletionEvidence
from octop.infra.agentcomputer.project_document_tool import read_project_document
from octop.infra.agentcomputer.results import get_run_result
from octop.infra.agentcomputer.runs import get_run
from octop.infra.agents.manager import AgentManager


async def test_one_journaled_project_read_produces_verified_final_answer(
    entry, tmp_path, monkeypatch
):
    monkeypatch.setattr(effects, "get_capabilities", _open_fixture_gate)
    intent = entry.claim.intent
    entry.registry = AgentManager(repos=entry.services.repos, paths=entry.services.paths)
    workspace = entry.registry.workspace_for_project_preparation(
        intent.snapshot.agentID, str(intent.projectID)
    )
    workspace.write_text("documents/brief.md", "项目代号：海星。", force=True)
    entry.tools = [read_project_document]
    entry.model.responses = [
        AIMessage(
            content="",
            id=str(uuid4()),
            tool_calls=[
                {"name": "read_project_document", "args": {"path": "brief.md"}, "id": "read-1"}
            ],
        ),
        AIMessage(content="项目代号是海星。", id=str(intent.assistantMessageID)),
    ]
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "tool-checkpoints.sqlite")
    ) as saver:
        entry.checkpointer = saver
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
        assert entry.model.calls == 2
        observed_tool = [
            message for message in entry.model.seen[1] if isinstance(message, ToolMessage)
        ]
        assert len(observed_tool) == 1
        assert json.loads(observed_tool[0].content)["content"] == "项目代号：海星。"
        view = get_run(entry.services, owner_id=1, run_id=intent.runID)
        assert view is not None and view.state == "succeeded"
        result = get_run_result(entry.services, owner_id=1, run_id=intent.runID)
        assert result is not None and result.text == "项目代号是海星。"
        with entry.services.db.connect() as conn:
            rows = conn.execute(
                "SELECT phase,terminal_json FROM ac_tool_effects WHERE run_id=?",
                (str(intent.runID),),
            ).fetchall()
            terminal = conn.execute(
                "SELECT receipt_json FROM ac_run_terminals WHERE run_id=?",
                (str(intent.runID),),
            ).fetchone()
        assert len(rows) == 1 and rows[0]["phase"] == "terminal"
        assert terminal is not None
        from octop.infra.agentcomputer.completions import RunCompletionReceipt

        receipt = RunCompletionReceipt.model_validate_json(terminal["receipt_json"])
        assert isinstance(receipt.evidence, ToolCompletionEvidence)
        assert receipt.evidence.toolCallID == "read-1"


async def test_other_registered_tool_still_cannot_dispatch(entry, tmp_path):
    called: list[str] = []

    @tool
    def fixture_effect(value: str) -> str:
        """This fixture must never run through the managed tool boundary."""
        called.append(value)
        return value

    entry.registry = AgentManager(repos=entry.services.repos, paths=entry.services.paths)
    entry.tools = [read_project_document, fixture_effect]
    entry.model.responses = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "fixture_effect", "args": {"value": "write"}, "id": "forbidden-call"}
            ],
        )
    ]
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "forbidden.sqlite")) as saver:
        entry.checkpointer = saver
        with pytest.raises(CommandAdmissionError, match="runtimeUnavailable"):
            await dispatch.dispatch_run(
                entry.services,
                claim=entry.claim,
                invoke=entry.invoke,
                active_runs=entry.active_runs,
            )
    assert called == []
    with entry.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0


async def test_read_without_final_answer_keeps_run_fenced(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(effects, "get_capabilities", _open_fixture_gate)
    intent = entry.claim.intent
    entry.registry = AgentManager(repos=entry.services.repos, paths=entry.services.paths)
    entry.tools = [read_project_document]
    entry.model.responses = [
        AIMessage(
            content="",
            id=str(uuid4()),
            tool_calls=[
                {"name": "read_project_document", "args": {"path": "missing.md"}, "id": "read-1"}
            ],
        ),
        AIMessage(content="", id=str(intent.assistantMessageID)),
    ]
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "missing-final.sqlite")) as saver:
        entry.checkpointer = saver
        with pytest.raises(CommandAdmissionError) as error:
            await dispatch.dispatch_run(
                entry.services,
                claim=entry.claim,
                invoke=entry.invoke,
                active_runs=entry.active_runs,
            )
    assert error.value.reason == "completionEvidenceUnavailable"
    view = get_run(entry.services, owner_id=1, run_id=intent.runID)
    assert view is not None and view.state == "interrupted" and view.reconciliationRequired
    with entry.services.db.connect() as conn:
        effect = conn.execute(
            "SELECT phase FROM ac_tool_effects WHERE run_id=?", (str(intent.runID),)
        ).fetchone()
        assert effect is not None and effect["phase"] == "terminal"
        assert conn.execute("SELECT COUNT(*) FROM ac_run_terminals").fetchone()[0] == 0
        project = conn.execute(
            "SELECT active_run_id FROM ac_projects WHERE project_id=?", (str(intent.projectID),)
        ).fetchone()
        assert project is not None and project["active_run_id"] == str(intent.runID)
