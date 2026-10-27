"""Unified Task forwarding; remote execution stays authoritative on the peer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from octop.infra.bridge.http_tunnel import MAX_TASK_TUNNEL_BODY_BYTES
from octop.infra.bridge.tunnel_policy import is_task_tunnel_path_allowed
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError


class RemoteTasks:
    def __init__(self, services: SharedServices, manager: Any) -> None:
        self.services, self.manager = services, manager

    @staticmethod
    def connection_fingerprint(row: Any) -> str:
        return hashlib.sha256(
            json.dumps([row.peer_base_url, row.peer_username]).encode()
        ).hexdigest()

    async def capabilities(self, owner_id: int, connection_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self.manager.get_owned, connection_id, owner_id)
        response = await self.manager.tunnel_http(
            connection_id=connection_id,
            owner_user_id=owner_id,
            method="GET",
            path="/api/project-tasks/capabilities",
        )
        if response.status_code == 401:
            raise OctopError.localized(ErrorCode.BRIDGE_AUTH_FAILED, status=403)
        if response.status_code != 200:
            raise OctopError.localized(ErrorCode.BRIDGE_REMOTE_UNSUPPORTED)
        try:
            data = response.json()
        except ValueError:
            raise OctopError.localized(ErrorCode.BRIDGE_REMOTE_UNSUPPORTED) from None
        if not isinstance(data, dict) or (
            data.get("contract") != "unified-task-conversations"
            or data.get("version") != 1
            or data.get("protocol_version") != 3
            or not data.get("instance_id")
            or not data.get("account_id")
            or not isinstance(data.get("capabilities"), dict)
            or data["capabilities"].get("tasks") is not True
        ):
            raise OctopError.localized(ErrorCode.BRIDGE_REMOTE_UNSUPPORTED)
        binding = hashlib.sha256(
            json.dumps(
                [row.peer_base_url, row.peer_username, data["instance_id"], data["account_id"]]
            ).encode()
        ).hexdigest()
        previous, fingerprint = await asyncio.to_thread(
            self.services.federated_task_repo.bind_host,
            owner_id,
            connection_id,
            binding,
            self.connection_fingerprint(row),
        )
        if previous != binding or fingerprint != self.connection_fingerprint(row):
            raise OctopError.localized(ErrorCode.BRIDGE_AUTH_FAILED, status=403)
        return {
            "host_id": connection_id,
            "peer_identity": binding,
            "available": True,
            "protocol_version": 3,
            "capabilities": data["capabilities"],
            "max_request_bytes": MAX_TASK_TUNNEL_BODY_BYTES,
        }

    def _media(self, connection_id: str, value: Any) -> Any:
        if isinstance(value, list):
            return [self._media(connection_id, item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if (
                key in {"download_url", "preview_url", "url"}
                and isinstance(item, str)
                and item.startswith("/api/")
            ):
                parsed = urlsplit(item)
                if is_task_tunnel_path_allowed("GET", parsed.path):
                    query = urlencode(
                        [
                            (k, v)
                            for k, v in parse_qsl(parsed.query)
                            if k not in {"token", "access_token"}
                        ]
                    )
                    item = (
                        f"/api/bridge/connections/{connection_id}/task-api/{parsed.path.removeprefix('/api/')}"
                        + ("?" + query if query else "")
                    )
                else:
                    # Unrecognized peer links must not become authenticated
                    # requests against a coincidentally matching local route.
                    item = ""
            result[key] = self._media(connection_id, item)
        return result

    async def proxy(
        self,
        owner_id: int,
        connection_id: str,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        body: bytes,
    ) -> httpx.Response:
        if not is_task_tunnel_path_allowed(method, path):
            raise OctopError.localized(ErrorCode.BRIDGE_REMOTE_UNSUPPORTED)
        capability = await self.capabilities(owner_id, connection_id)
        if not capability["capabilities"].get("execution_engines"):
            requires_engines = path in {
                "/api/project-tasks/engines",
                "/api/project-tasks/engines/inspect",
            }
            if body and body.lstrip().startswith(b"{"):
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and "engine" in payload:
                    selection = payload["engine"]
                    native_default = selection is None or (
                        isinstance(selection, dict)
                        and not selection.keys() - {"engine_id", "connection_id", "model_id"}
                        and selection.get("engine_id", "octop") == "octop"
                        and selection.get("connection_id") is None
                        and selection.get("model_id") is None
                    )
                    if not native_default:
                        requires_engines = True
                    else:
                        payload.pop("engine")
                        if method.upper() == "PATCH" and not payload.keys() - {
                            "expected_revision",
                            "expected_configuration_revision",
                        }:
                            requires_engines = True
                        body = json.dumps(payload, ensure_ascii=False).encode()
            if requires_engines:
                raise OctopError.localized(ErrorCode.BRIDGE_REMOTE_UNSUPPORTED)
        query = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(query, keep_blank_values=True)
                if k not in {"token", "access_token"}
            ]
        )
        response = await self.manager.tunnel_http(
            connection_id=connection_id,
            owner_user_id=owner_id,
            method=method,
            path=path,
            query=query,
            headers={
                key: value
                for key, value in headers.items()
                if key.lower()
                in {
                    "accept",
                    "accept-language",
                    "content-type",
                    "range",
                    "if-range",
                    "if-none-match",
                    "idempotency-key",
                }
            },
            body=body,
        )
        if response.status_code == 401:
            # Peer authentication loss must not log the browser out locally.
            raise OctopError.localized(ErrorCode.BRIDGE_AUTH_FAILED, status=403)
        # HTTP renewal tokens and cookies belong to the peer account, never to
        # the user's local session. Only product response metadata is forwarded.
        public_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower()
            in {
                "content-type",
                "content-disposition",
                "content-range",
                "accept-ranges",
                "etag",
                "cache-control",
                "last-modified",
                "content-security-policy",
                "x-content-type-options",
            }
        }
        content = response.content
        if (
            response.is_success
            and "application/json" in response.headers.get("content-type", "")
            and "content-disposition" not in response.headers
        ):
            data = response.json()
            candidates = (
                data.get("items", [])
                if isinstance(data, dict)
                else data
                if isinstance(data, list)
                else []
            )
            if isinstance(data, dict) and data.get("task_id") and "title" in data:
                candidates = [data]
            from octop.infra.projects.tasks import TaskSummary

            for task in candidates:
                if isinstance(task, dict) and task.get("task_id") and "title" in task:
                    summary = {
                        key: value for key, value in task.items() if key in TaskSummary.model_fields
                    }
                    await asyncio.to_thread(
                        self.services.federated_task_repo.remember,
                        owner_id,
                        connection_id,
                        capability["peer_identity"],
                        summary,
                        time.time_ns() // 1_000_000,
                    )
            content = json.dumps(self._media(connection_id, data), ensure_ascii=False).encode()
        if method == "DELETE" and response.is_success:
            parts = path.split("/")
            if len(parts) == 4 and parts[2] == "project-tasks":
                await asyncio.to_thread(
                    self.services.federated_task_repo.forget, owner_id, connection_id, parts[3]
                )
        return httpx.Response(response.status_code, headers=public_headers, content=content)
