"""Chat attachments cross the bridge as a tunneled multipart upload."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, File, UploadFile

from octop.infra.bridge import transport
from octop.infra.bridge.http_tunnel import execute_local_http
from octop.infra.bridge.ids import rewrite_tunneled_json
from octop.infra.bridge.transport import (
    BODY_CHUNK_RAW_BYTES,
    FEATURE_BODY_CHUNKS,
    BridgeSession,
)


def _body_chunk(
    request_id: str = "request",
    sequence: object = 0,
    *,
    data: bytes = b"part",
    more: bool = True,
) -> str:
    return json.dumps(
        {
            "type": "tunnel.request",
            "id": request_id,
            "body_seq": sequence,
            "body_more": more,
            "body_b64": base64.b64encode(data).decode("ascii"),
        }
    )


def _receiving_session() -> BridgeSession:
    return BridgeSession(
        connection_id="cid",
        send_text=AsyncMock(),
        on_tunnel_request=AsyncMock(),
        peer_features={FEATURE_BODY_CHUNKS},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sequence", [-1, 1, transport.MAX_BODY_CHUNKS, "0", True])
async def test_rejects_invalid_or_missing_initial_chunk_sequence(sequence: object) -> None:
    session = _receiving_session()
    await session.handle_message(_body_chunk(sequence=sequence))
    assert session.closed
    assert not session._body_parts
    assert session._body_bytes == 0


@pytest.mark.asyncio
async def test_rejects_duplicate_chunks_and_clears_buffer() -> None:
    session = _receiving_session()
    await session.handle_message(_body_chunk())
    await session.handle_message(_body_chunk())
    assert session.closed
    assert session._body_bytes == 0


@pytest.mark.asyncio
async def test_reassembly_budget_is_shared_by_all_pending_bodies(monkeypatch) -> None:
    monkeypatch.setattr(transport, "BRIDGE_TUNNEL_CHUNKED_MAX_BODY_BYTES", 6)
    session = _receiving_session()
    await session.handle_message(_body_chunk("first"))
    await session.handle_message(_body_chunk("second"))
    assert session.closed
    assert not session._body_parts
    assert session._body_bytes == 0


@pytest.mark.asyncio
async def test_limits_empty_partial_body_entries(monkeypatch) -> None:
    monkeypatch.setattr(transport, "MAX_PENDING_BODY_ASSEMBLIES", 2)
    session = _receiving_session()
    for request_id in ("first", "second", "third"):
        await session.handle_message(_body_chunk(request_id, data=b""))
    assert session.closed
    assert not session._body_parts


@pytest.mark.asyncio
async def test_rejects_oversized_chunk(monkeypatch) -> None:
    monkeypatch.setattr(transport, "BODY_CHUNK_RAW_BYTES", 3)
    session = _receiving_session()
    await session.handle_message(_body_chunk(data=b"1234"))
    assert session.closed
    assert not session._body_parts


@pytest.mark.asyncio
async def test_rejects_invalid_base64_chunk() -> None:
    session = _receiving_session()
    payload = json.loads(_body_chunk())
    payload["body_b64"] = "not base64!"
    await session.handle_message(json.dumps(payload))
    assert session.closed
    assert not session._body_parts


@pytest.mark.asyncio
async def test_final_chunk_requires_sequence_when_body_is_partial() -> None:
    session = _receiving_session()
    await session.handle_message(_body_chunk())
    await session.handle_message(json.dumps({"type": "tunnel.request", "id": "request"}))
    assert session.closed
    assert not session._body_parts


@pytest.mark.asyncio
async def test_tunnel_timeout_releases_partial_response() -> None:
    session: BridgeSession

    async def partial_response(text: str) -> None:
        payload = json.loads(text)
        if payload.get("type") == "tunnel.request":
            response = json.loads(_body_chunk(payload["id"]))
            response["type"] = "tunnel.response"
            await session.handle_message(json.dumps(response))

    session = BridgeSession(connection_id="cid", send_text=partial_response)
    with pytest.raises(TimeoutError):
        await session.tunnel_request(method="GET", path="/", timeout=0.001)
    assert not session._pending
    assert not session._body_parts
    assert session._body_bytes == 0
    await session.close()


def _multipart(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----octop-bridge"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.mark.asyncio
async def test_execute_local_http_accepts_chat_upload() -> None:
    app = FastAPI()

    @app.post("/api/agents/{agent_id}/upload")
    async def upload(agent_id: str, file: UploadFile = File(...)) -> dict[str, object]:  # noqa: B008
        data = await file.read()
        return {
            "path": f"inbound/{file.filename}",
            "workspace_path": f"inbound/{file.filename}",
            "filename": file.filename,
            "media_type": file.content_type,
            "url": f"/api/agents/{agent_id}/media/preview?source=inbound/{file.filename}",
            "access_url": f"/api/agents/{agent_id}/media/preview?source=inbound/{file.filename}",
            "size": len(data),
        }

    raw, content_type = _multipart("note.txt", b"hello-bridge")
    result = await execute_local_http(
        app=app,
        method="POST",
        path="/api/agents/doctor/upload",
        headers={"content-type": content_type},
        body=raw,
        access_token="token",
    )
    assert result["status"] == 200
    payload = json.loads(base64.b64decode(str(result["body_b64"])))
    rewritten = rewrite_tunneled_json(
        payload,
        remote_agent_id="doctor",
        bridge_agent_id="bridge:cid1:doctor",
    )
    assert rewritten["filename"] == "note.txt"
    assert rewritten["path"] == "inbound/note.txt"
    assert rewritten["url"].startswith("/api/agents/bridge:cid1:doctor/media/preview")


@pytest.mark.asyncio
async def test_chunked_upload_body_is_reassembled() -> None:
    captured: dict[str, object] = {}

    async def on_request(payload: dict[str, object]) -> dict[str, object]:
        from octop.infra.bridge.http_tunnel import decode_body_b64

        captured["body"] = decode_body_b64(payload)
        captured["path"] = payload.get("path")
        captured["content_type"] = (payload.get("headers") or {}).get("content-type")  # type: ignore[union-attr]
        return {"status": 201, "headers": {"content-type": "application/json"}, "done": True}

    peer: BridgeSession
    initiator: BridgeSession

    async def peer_send(text: str) -> None:
        await initiator.handle_message(text)

    async def initiator_send(text: str) -> None:
        await peer.handle_message(text)

    peer = BridgeSession(
        connection_id="cid",
        send_text=peer_send,
        on_tunnel_request=on_request,
        peer_features={FEATURE_BODY_CHUNKS},
    )
    initiator = BridgeSession(
        connection_id="cid",
        send_text=initiator_send,
        peer_features={FEATURE_BODY_CHUNKS},
    )
    raw, content_type = _multipart("blob.bin", b"a" * (BODY_CHUNK_RAW_BYTES + 32))
    result = await initiator.tunnel_request(
        method="POST",
        path="/api/agents/doctor/upload",
        headers={"content-type": content_type},
        body=raw,
    )
    assert result["status"] == 201
    assert captured["body"] == raw
    assert captured["path"] == "/api/agents/doctor/upload"
    assert captured["content_type"] == content_type
    await asyncio.gather(peer.close(), initiator.close())
