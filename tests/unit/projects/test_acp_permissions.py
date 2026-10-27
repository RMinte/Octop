"""Native permission details remain useful when the vendor supplies no raw arguments."""
# ruff: noqa: F811

from __future__ import annotations

import pytest
from tests.unit.projects.test_execution_engines import ready
from tests.unit.projects.test_expert_execution import services  # noqa: F401

from octop.infra.projects import interactions, tasks
from octop.infra.projects.engines.base import EngineRequest
from octop.infra.projects.engines.permissions import EnginePermissions


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        (
            {
                "title": "Read seed file",
                "kind": "read",
                "rawInput": {},
                "locations": [{"path": "seed.txt"}],
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": "Read the selected local file"},
                    }
                ],
            },
            {
                "title": "Read seed file",
                "kind": "read",
                "locations": [{"path": "seed.txt"}],
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": "Read the selected local file"},
                    }
                ],
            },
        ),
        ({"rawInput": {}}, {}),
        ({"rawInput": {"filePath": "seed.txt"}, "title": "Read"}, {"filePath": "seed.txt"}),
    ],
)
def test_permission_preserves_actual_native_description_without_fabricating_parameters(
    services, monkeypatch, tool, expected
):
    selection, _ = ready(services, monkeypatch)
    task = tasks.send(
        services,
        1,
        tasks.TaskCreate(
            request_id="native-permission-details", prompt="Read local seed", engine=selection
        ),
    )
    turn = services.project_task_turn_repo.claim("permission-worker", tasks.now_ms())
    permission = EnginePermissions(EngineRequest(services, 1, turn, []), "en")
    identifier = permission._begin({"toolCallId": "native-read", **tool}, True)
    stored = services.project_task_turn_repo.get(turn.turn_id)
    proposal = next(item for item in interactions.load(stored) if item.interaction_id == identifier)
    assert proposal.payload.arguments == expected
    assert stored.status == "running" and stored.wait_kind == "engine_approval"
    assert proposal.task_id == task.task_id and proposal.tool_call_id == "native-read"
