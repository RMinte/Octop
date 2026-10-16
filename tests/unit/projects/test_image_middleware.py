"""Model requests receive image bytes while durable task messages retain references."""

import json
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)
from PIL import Image

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import input_attachments
from octop.infra.projects.image_middleware import TaskImageMaterializeMiddleware
from octop.infra.projects.image_tools import READ_TASK_IMAGE, TASK_IMAGE_KEY, read_image_tool
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def stored_image(tmp_path):
    db = SqlitePool(tmp_path / "images.db")
    run_migrations(db)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    owner_id = services.user_repo.create(username="image-user", role="user")
    encoded = BytesIO()
    Image.new("RGB", (4, 4), "red").save(encoded, "PNG")
    data = encoded.getvalue()
    image = input_attachments.save(services, owner_id, "screenshot.png", data)
    task = ProjectTaskRow("image-task", owner_id, None, "Image task", "request", None, 1, 1)
    with db.transaction() as conn:
        assert services.project_task_repo.lock_owner(conn, owner_id)
        services.project_task_repo.insert(conn, task)
        input_attachments.bind(services, conn, owner_id, task.task_id, [image.attachment_id])
    yield services, owner_id, task.task_id, image, data
    db.close()


@pytest.mark.parametrize("asynchronous", [True, False])
async def test_model_expansion_keeps_original_message_unchanged(stored_image, asynchronous):
    services, owner_id, task_id, image, data = stored_image
    human = HumanMessage(content=input_attachments.content("Check this", [image.attachment_id]))
    original = deepcopy(message_to_dict(human))
    captured = {}

    def handler(request):
        captured["messages"] = request.messages
        return MagicMock()

    async def async_handler(request):
        return handler(request)

    request = MagicMock()
    request.messages = [human]
    request.override = lambda **kwargs: SimpleNamespace(messages=kwargs["messages"])
    middleware = TaskImageMaterializeMiddleware(services, owner_id, task_id)
    if asynchronous:
        await middleware.awrap_model_call(request, async_handler)
    else:
        middleware.wrap_model_call(request, handler)

    import base64

    blocks = captured["messages"][0].content
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == data
    assert message_to_dict(human) == original
    assert blocks[0] == {"type": "text", "text": "Check this"}


async def test_model_cannot_resolve_a_different_tasks_image(stored_image):
    services, owner_id, _, image, _ = stored_image
    human = HumanMessage(content=input_attachments.content("", [image.attachment_id]))
    request, handler = MagicMock(), MagicMock()
    request.messages = [human]
    middleware = TaskImageMaterializeMiddleware(services, owner_id, "another-task")
    with pytest.raises(OctopError) as caught:
        middleware.wrap_model_call(request, handler)
    assert caught.value.code == ErrorCode.TASK_IMAGE_UNAVAILABLE
    handler.assert_not_called()


async def test_image_directory_lists_metadata_without_loading_bytes(stored_image, monkeypatch):
    from octop.infra.projects import image_tools

    services, owner_id, task_id, image, _ = stored_image
    download = MagicMock(side_effect=AssertionError("directory must not read image bytes"))
    monkeypatch.setattr(image_tools, "download", download)
    tool = read_image_tool(services, owner_id, task_id, "zh")
    result = await tool.ainvoke(
        {"type": "tool_call", "name": READ_TASK_IMAGE, "id": "directory", "args": {}}
    )
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "directory" and result.status == "success"
    assert json.loads(result.content) == [image.model_dump(mode="json")]
    assert TASK_IMAGE_KEY not in result.additional_kwargs
    assert "data:image/" not in result.content
    download.assert_not_called()


@pytest.mark.parametrize("asynchronous", [True, False])
async def test_image_read_after_compaction_follows_complete_parallel_tool_batch(
    stored_image, asynchronous
):
    services, owner_id, task_id, image, data = stored_image
    tool = read_image_tool(services, owner_id, task_id, "zh")
    result = await tool.ainvoke(
        {
            "type": "tool_call",
            "name": READ_TASK_IMAGE,
            "id": "read-image",
            "args": {"attachment_id": image.attachment_id},
        }
    )
    assert isinstance(result, ToolMessage)
    assert result.additional_kwargs[TASK_IMAGE_KEY] == image.attachment_id
    assert "data:image/" not in result.content
    messages = [
        SystemMessage(content="Earlier image input was compacted into this summary."),
        HumanMessage(content="Read the previous screenshot again."),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "read-image",
                    "name": READ_TASK_IMAGE,
                    "args": {"attachment_id": image.attachment_id},
                },
                {"id": "ordinary", "name": "ordinary_tool", "args": {}},
            ],
        ),
        result,
        ToolMessage(content="Ordinary result", tool_call_id="ordinary"),
        AIMessage(content="The screenshot was inspected."),
    ]
    original = deepcopy([message_to_dict(message) for message in messages])
    captured = {}

    def handler(request):
        captured["messages"] = request.messages
        return MagicMock()

    async def async_handler(request):
        return handler(request)

    request = MagicMock()
    request.messages = messages
    request.override = lambda **kwargs: SimpleNamespace(messages=kwargs["messages"])
    middleware = TaskImageMaterializeMiddleware(services, owner_id, task_id)
    if asynchronous:
        await middleware.awrap_model_call(request, async_handler)
    else:
        middleware.wrap_model_call(request, handler)

    import base64

    expanded = captured["messages"]
    assert expanded[:5] == messages[:5]
    assert isinstance(expanded[5], HumanMessage)
    assert expanded[6] == messages[5]
    blocks = expanded[5].content
    assert blocks[0] == {"type": "text", "text": "task-image://" + image.attachment_id}
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == data
    assert [message_to_dict(message) for message in messages] == original


@pytest.mark.parametrize("attachment_id", [None, "image"])
async def test_image_tool_ownership_errors_are_recoverable(stored_image, attachment_id):
    services, owner_id, _, image, _ = stored_image
    tool = read_image_tool(services, owner_id, "another-task", "en")
    result = await tool.ainvoke(
        {
            "type": "tool_call",
            "name": READ_TASK_IMAGE,
            "id": "failed-read",
            "args": {"attachment_id": image.attachment_id} if attachment_id else {},
        }
    )
    assert isinstance(result, ToolMessage)
    assert result.status == "error" and result.tool_call_id == "failed-read"
    assert json.loads(result.content)["error_code"] == "TASK_IMAGE_UNAVAILABLE"
    assert TASK_IMAGE_KEY not in result.additional_kwargs
