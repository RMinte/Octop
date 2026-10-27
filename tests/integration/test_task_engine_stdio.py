"""Exercise the real ACP SDK and subprocess pipes without a vendor or model call."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from octop.infra.projects.engines.acp import ACPTaskSession

AGENT = """
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
def update(session, content):
    send({"jsonrpc":"2.0","method":"session/update","params":{
        "sessionId":session,"update":{"sessionUpdate":"agent_message_chunk",
        "content":{"type":"text","text":content}}}})
for line in sys.stdin:
    request=json.loads(line)
    method=request.get("method")
    params=request.get("params",{})
    result={}
    if method=="initialize":
        result={"protocolVersion":params["protocolVersion"],
                "agentCapabilities":{"loadSession":True},"authMethods":[]}
    elif method=="session/new":
        result={"sessionId":"native-stdio"}
    elif method=="session/load":
        update(params["sessionId"],"old-history-must-not-repeat")
    elif method=="session/prompt":
        for index in range(25):
            update(params["sessionId"],f"chunk-{index}")
        update(params["sessionId"],"real-stdio-response")
        send({"jsonrpc":"2.0","method":"session/update","params":{
            "sessionId":params["sessionId"],"update":{"sessionUpdate":"tool_call",
            "toolCallId":"tail-tool","title":"Read","kind":"read","status":"in_progress"}}})
        send({"jsonrpc":"2.0","method":"session/update","params":{
            "sessionId":params["sessionId"],"update":{"sessionUpdate":"tool_call_update",
            "toolCallId":"tail-tool","status":"completed","rawOutput":"tail result"}}})
        result={"stopReason":"end_turn"}
    if "id" in request:
        send({"jsonrpc":"2.0","id":request["id"],"result":result})
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, "native-stdio"])
async def test_real_stdio_new_load_and_stream_cleanup(tmp_path, monkeypatch, saved):
    script = tmp_path / "agent.py"
    script.write_text(AGENT, encoding="utf-8")
    session = ACPTaskSession([sys.executable, "-u", str(script)], dict(os.environ), str(tmp_path))
    replay_entered, replay_release = asyncio.Event(), asyncio.Event()
    tail_entered, tail_release = asyncio.Event(), asyncio.Event()
    original_update = session._update

    async def update(session_id, value):
        text = (value.get("content") or {}).get("text")
        if text == "old-history-must-not-repeat":
            replay_entered.set()
            await replay_release.wait()
        if text == "real-stdio-response":
            tail_entered.set()
            await tail_release.wait()
        await original_update(session_id, value)

    monkeypatch.setattr(session, "_update", update)
    try:
        opening = asyncio.create_task(session.open(saved))
        if saved:
            await asyncio.wait_for(replay_entered.wait(), 3)
            assert not opening.done()
            replay_release.set()
        info = await asyncio.wait_for(opening, 3)
        process = session._process
        assert info.session_id == "native-stdio" and info.resumed == bool(saved)

        async def collect():
            return [event async for event in session.stream([{"type": "text", "text": "hello"}])]

        streaming = asyncio.create_task(collect())
        await asyncio.wait_for(tail_entered.wait(), 3)
        assert not streaming.done()
        tail_release.set()
        events = await asyncio.wait_for(streaming, 3)
        assert [event["content"] for event in events if event["type"] == "token"] == [
            *(f"chunk-{index}" for index in range(25)),
            "real-stdio-response",
        ]
        assert [event["type"] for event in events][-3:] == ["tool_start", "tool_end", "message_end"]
        assert events[-2]["output"] == "tail result"
        assert events[-1] == {"type": "message_end"}
    finally:
        replay_release.set()
        tail_release.set()
        await session.close()
    assert process.returncode is not None
