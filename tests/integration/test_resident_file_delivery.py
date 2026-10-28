"""Delegated file results return to private history with owned download bytes."""

from __future__ import annotations

import hashlib

from octop.infra.projects.artifact_models import CreateArtifactArgs
from octop.infra.projects.artifacts import TaskArtifactPublisher
from tests.integration.test_resident_closed_loop import (
    _delegate_body,
    _delegation,
    _json,
    _send_resident,
    _task,
)
from tests.integration.test_resident_closed_loop import closed_loop as closed_loop
from tests.support.auth import create_user
from tests.support.harness import patch_task_stream


async def test_delegated_file_returns_to_private_history_and_downloads_only_for_owner(
    closed_loop, monkeypatch, tmp_path
):
    client, server, auth, _binding, base, source, requests, _calls = closed_loop
    published = []
    content = "Requested checklist\n1. Review the draft\n2. Send the result\n"

    async def work(services, owner, turn, history):
        publisher = TaskArtifactPublisher(services, owner, turn, tmp_path, "en")
        artifact = await publisher.create(
            "attach-result", CreateArtifactArgs(name="checklist.txt", content=content)
        )
        published.append(artifact)
        yield {"type": "token", "content": "The checklist is attached."}

    patch_task_stream(monkeypatch, work)
    body = _delegate_body(source, prompt="Create and return the requested checklist file.")
    delegated = await _json(
        await client.post(base + "/conversation/delegations", headers=auth, json=body), 202
    )
    await _task(client, auth, delegated["task_id"])
    delivered = await _delegation(client, auth, base, delegated["delegation_id"])
    report = delivered["reports"][0]
    assert len(published) == 1
    artifact = published[0]
    assert report["artifacts"] == [
        {
            "artifact_id": artifact.artifact_id,
            "name": "checklist.txt",
            "media_type": "text/plain",
            "byte_count": len(content.encode()),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "business_category": "document",
        }
    ]
    history = await _json(await client.get(base + "/conversation/history", headers=auth))
    item = next(item for item in history["items"] if item["kind"] == "task_report")
    assert item["report"]["artifacts"] == report["artifacts"]
    located = await _json(
        await client.get(base + "/conversation/history/items/" + item["item_id"], headers=auth)
    )
    assert any(entry["report"] == item["report"] for entry in located["items"])
    downloaded = await client.get(artifact.download_url, headers=auth)
    assert downloaded.status_code == 200 and downloaded.content == content.encode()
    assert "checklist.txt" in downloaded.headers["content-disposition"]
    stranger = await create_user(client, auth, username="file-result-stranger")
    assert (await client.get(artifact.download_url, headers=stranger)).status_code == 404
    assert (await client.get(artifact.download_url)).status_code in {401, 403}
    replay = await _json(
        await client.post(base + "/conversation/delegations", headers=auth, json=body), 202
    )
    assert replay["delegation_id"] == delegated["delegation_id"] and len(published) == 1
    await _send_resident(client, auth, base, "Which file did you return?")
    assert any(
        message.id == report["message_id"] and "checklist.txt" in str(message.content)
        for message in requests[-1]["previous"]
    )
