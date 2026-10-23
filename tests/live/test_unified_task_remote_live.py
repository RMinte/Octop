"""Opt-in two-instance Bridge acceptance with one real remote-model turn."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from uuid import uuid4

import pytest
import uvicorn
from tests.live.unified_task_support import live_options, live_task_environment
from tests.support.auth import TEST_PASSWORD
from tests.support.http import ws_connect

pytestmark = pytest.mark.live


async def test_live_remote_task_uses_peer_model_workspace_and_saved_state(live_task_env, tmp_path):
    local = live_task_env
    async with live_task_environment(tmp_path / "remote-octop") as peer:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(128)
        http_server = uvicorn.Server(
            uvicorn.Config(
                peer.client._octop_app,
                host="127.0.0.1",
                port=port,
                lifespan="off",
                log_level="error",
                access_log=False,
                ws_max_size=32 * 1024 * 1024,
            )
        )
        serving = asyncio.create_task(http_server.serve(sockets=[sock]))
        connection_id = None
        trace = []
        for label, environment in (("local", local), ("peer", peer)):
            relay = environment.server.app_runtime.bridge_manager.task_streams
            original_handle = relay.handle

            async def tracked(cid, frame, original=original_handle, side=label):
                trace.append(
                    {
                        "side": side,
                        "type": frame.get("type"),
                        "inner_type": (frame.get("frame") or {}).get("type"),
                    }
                )
                await original(cid, frame)

            relay.handle = tracked
        try:
            async with asyncio.timeout(15):
                while not http_server.started:
                    await asyncio.sleep(0.05)
            response = await local.client.post(
                "/api/bridge/connections",
                headers=local.auth,
                json={
                    "peer_base_url": f"http://127.0.0.1:{port}",
                    "peer_username": "admin",
                    "password": TEST_PASSWORD,
                    "display_name": "Acceptance remote",
                    "connect": True,
                },
            )
            assert response.status_code == 201, response.status_code
            connection_id = response.json()["connection_id"]
            prefix = f"/api/bridge/connections/{connection_id}"
            async with asyncio.timeout(30):
                while True:
                    current = await local.client.get(prefix, headers=local.auth)
                    if current.json()["status"] == "connected":
                        break
                    await asyncio.sleep(0.1)
            cap = await local.client.get(prefix + "/task-capabilities", headers=local.auth)
            assert cap.status_code == 200 and cap.json()["capabilities"]["tasks"]
            directory = peer.home / "remote-project"
            directory.mkdir()
            nonce = "remote-proof-" + uuid4().hex
            (directory / "seed.txt").write_text(nonce)
            expert_id = "remote-acceptance-expert"
            peer.server.services.agent_repo.create(
                agent_id=expert_id,
                user_id=peer.owner_id,
                name="Remote acceptance expert",
                config_json=json.dumps(
                    {
                        "workspace_dir": str(peer.home / "expert-source"),
                        "memory": {"memory_enabled": False},
                    }
                ),
                system_prompt="按用户指示调用read_file和write_file，在当前项目工作目录读写文件，不运行命令或浏览网络，简短回复。",
                default_model=peer.model_ref,
            )
            project = await local.client.post(
                prefix + "/task-api/projects",
                headers={**local.auth, "Idempotency-Key": uuid4().hex},
                json={
                    "name": "Remote acceptance",
                    "directory": str(directory),
                    "memory_enabled": False,
                },
            )
            assert project.status_code == 201, project.status_code
            attachment = await local.client.post(
                prefix + "/task-api/project-tasks/attachments",
                headers=local.auth,
                files={"file": ("remote-input.txt", b"remote input content", "text/plain")},
            )
            assert attachment.status_code == 201, attachment.status_code
            preview_url = attachment.json()["preview_url"]
            assert preview_url.startswith(prefix + "/task-api/")
            preview = await local.client.get(preview_url, headers=local.auth)
            assert preview.content == b"remote input content"
            payload = {
                "request_id": "remote-live-" + uuid4().hex,
                "project_id": project.json()["project_id"],
                "selected_expert_id": expert_id,
                "web_search_enabled": False,
                "prompt": "实际调用read_file读取/seed.txt，再调用write_file将读到的字符串原样写到/proof.txt，然后简短报告。不要猜测字符串。",
                "options": live_options(peer),
            }
            frames = []
            async with ws_connect(
                local.client._octop_app, prefix + "/project-tasks/ws", headers=local.auth
            ) as ws:
                ready = await ws.receive_json()
                assert ready == {"type": "ready", "protocol_version": 3}
                await ws.send_json({"type": "task.create", "id": "remote-create", "data": payload})
                async with asyncio.timeout(60):
                    while True:
                        frame = await ws.receive_json(timeout=60)
                        frames.append(frame)
                        assert frame.get("type") != "session"
                        assert frame.get("type") != "error", (frame.get("error") or {}).get("code")
                        if frame.get("id") == "remote-create":
                            assert frame["type"] == "result", frame.get("type")
                            task_id = frame["data"]["task_id"]
                            break
                await ws.send_json(
                    {"type": "task.subscribe", "id": "remote-watch", "task_id": task_id}
                )
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    frame = await ws.receive_json(timeout=60)
                    frames.append(frame)
                    assert frame.get("type") != "session"
                    if (
                        frame.get("type") == "turn"
                        and frame["data"]["turn"]["status"] == "completed"
                    ):
                        break
                else:
                    raise TimeoutError("Remote turn did not complete")
            saved = await local.client.get(
                prefix + "/task-api/project-tasks/" + task_id, headers=local.auth
            )
            assert saved.status_code == 200 and saved.json()["turns"][-1]["status"] == "completed"
            assert (directory / "proof.txt").read_text().strip() == nonce
            assert not (peer.home / "expert-source" / "proof.txt").exists()
            assert local.server.services.project_task_repo.get(task_id, local.owner_id) is None
            with local.server.services.db.connect() as conn:
                assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
            with peer.server.services.db.connect() as conn:
                model_calls = conn.execute("SELECT count(*) FROM model_calls").fetchone()[0]
            assert model_calls > 0
            refs = await local.client.get(prefix + "/task-references", headers=local.auth)
            assert any(item["task_id"] == task_id for item in refs.json()["items"])
            exported = await local.client.get(
                prefix + "/task-api/project-tasks/" + task_id + "/export", headers=local.auth
            )
            assert exported.status_code == 200 and exported.json()["turns"][-1]["reply"]
            await local.client.post(prefix + "/disconnect", headers=local.auth)
            offline = await local.client.get(prefix + "/task-references", headers=local.auth)
            assert offline.status_code == 200 and offline.json()["items"]
            (local.home / "remote-live-evidence.json").write_text(
                json.dumps(
                    {
                        "real_bridge_socket": True,
                        "remote_task_ws": True,
                        "peer_model_calls": model_calls,
                        "local_model_calls": 0,
                        "peer_file_matches_nonce": True,
                        "source_workspace_unchanged": True,
                        "remote_attachment": True,
                        "offline_cached_reference": True,
                        "export": True,
                        "peer_session_token_frames": sum(
                            frame.get("type") == "session" for frame in frames
                        ),
                        "source_config": "read-only",
                        "task_id": task_id,
                    },
                    indent=2,
                )
            )
        finally:
            (local.home / "remote-live-transport-trace.json").write_text(
                json.dumps(trace, indent=2)
            )
            if connection_id:
                await local.server.app_runtime.bridge_manager.disconnect(connection_id)
            http_server.should_exit = True
            await asyncio.wait_for(serving, 15)
            sock.close()
