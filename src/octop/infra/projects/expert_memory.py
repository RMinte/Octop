"""Task access to explicitly scoped expert memory, separate from checkpoints.

Legacy memory is opt-in and read-only. New evidence is captured only into the
current project (or standalone task) namespace, then queued for durable extraction.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from octop_memory import MemoryService
from octop_memory.adapters.bridge.handlers import Bridge
from octop_memory.core import Memory
from pydantic import BaseModel, Field

from octop.i18n import tr
from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.workspace.dir import workspace_dir_from_config
from octop.infra.db.services import SharedServices
from octop.infra.projects.checkpoint_saver import checkpoint_fence
from octop.infra.projects.expert_profiles import expert_configuration

logger = logging.getLogger(__name__)
EXPERT_MEMORY_TOOLS = frozenset({"expert_memory_search", "expert_memory_get"})


def memory_namespace(owner_id: int, expert_id: str, kind: str, scope_id: str = "") -> str:
    identity = json.dumps([owner_id, expert_id, kind, scope_id], separators=(",", ":"))
    return "octop_expert_" + hashlib.sha256(identity.encode()).hexdigest()


@contextlib.contextmanager
def _scope_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=4096)
    source: Literal["general", "scope", "legacy"] = "scope"
    limit: int = Field(default=5, ge=1, le=20)


class GetArgs(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    source: Literal["general", "scope", "legacy"] = "scope"


class ExpertMemory:
    def __init__(
        self,
        services: SharedServices,
        owner_id: int,
        task_id: str,
        turn_id: str,
        project_id: str | None,
        profile: dict[str, Any],
        *,
        enabled: bool | None,
        worker_id: str | None = None,
    ) -> None:
        self.services, self.owner_id = services, owner_id
        self.task_id, self.turn_id = task_id, turn_id
        self.profile = profile
        self.worker_id = worker_id
        policy = profile.get("memory") or {}
        self.enabled = policy.get("enabled", True) is not False and enabled is not False
        self.capture_enabled = self.enabled and policy.get("capture_enabled", True) is not False
        expert_id = str(profile["expert_id"])
        self.project_id = project_id
        self.namespaces = {
            "general": memory_namespace(owner_id, expert_id, "general"),
            "scope": memory_namespace(
                owner_id, expert_id, "project" if project_id else "task", project_id or task_id
            ),
        }
        if (
            policy.get("legacy_recall_enabled") is True
            and profile.get("owner_id", owner_id) == owner_id
        ):
            self.namespaces["legacy"] = "agent_" + expert_id

    def journal_path(self, name: str) -> Path:
        identity = hashlib.sha256(str(self.profile["expert_id"]).encode()).hexdigest()
        return (
            self.services.paths.root
            / "project-tasks"
            / self.task_id
            / self.turn_id
            / "expert-memory"
            / identity
            / f"{name}.json"
        )

    def _configuration(self) -> tuple[Any, dict[str, Any]]:
        from octop.infra.projects.source_access import external_unverified

        turn = self.services.project_task_turn_repo.get(self.turn_id)
        if turn is not None and external_unverified(json.loads(turn.configuration_json)):
            raise ValueError("unverified external sources cannot access expert memory")
        user = self.services.user_repo.get(self.owner_id)
        if user is None or user.disabled:
            raise ValueError("expert memory user is disabled")
        return expert_configuration(self.services, self.owner_id, str(self.profile["expert_id"]))

    def _call(self, source: str, method: str, **kwargs: Any) -> Any:
        if not self.enabled or source not in self.namespaces:
            raise ValueError("expert memory source is not enabled")
        # Resolve credentials and current ownership here; secrets never enter a
        # configuration snapshot. Each operation owns its backend connection.
        row, config = self._configuration()
        policy = config.get("memory") or {}
        if isinstance(policy, dict) and policy.get("memory_enabled") is False:
            raise ValueError("expert memory is disabled")
        if source == "legacy" and (
            row.user_id != self.owner_id
            or not isinstance(policy, dict)
            or policy.get("legacy_recall_enabled") is not True
        ):
            raise ValueError("legacy expert memory is not enabled")
        if (
            method == "capture"
            and isinstance(policy, dict)
            and policy.get("capture_enabled") is False
        ):
            return None
        backend_config: dict[str, Any] | None
        if row.user_id != self.owner_id:
            # A public identity never grants access to its publisher's storage,
            # credentials, or legacy library. The viewer owns this independent DB.
            root = self.services.paths.root / "expert-memory-users" / str(self.owner_id)
            root.mkdir(parents=True, exist_ok=True)
            backend, backend_config = "sqlite", {"db_path": str(root / "memory.sqlite")}
        else:
            workspace = workspace_dir_from_config(
                config, paths=self.services.paths, agent_id=row.agent_id, ensure=False
            )
            _, backend, backend_config = open_memory_kwargs(
                agent_id=row.agent_id,
                cfg=config,
                octop_config=self.services.config,
                workspace_dir=workspace,
            )
        namespace = self.namespaces[source]
        lock = self.services.paths.root / "expert-memory-locks" / f"{namespace}.lock"
        with _scope_lock(lock):
            # Lock contention must not let an admitted operation outlive a
            # revoked identity or an expert storage/policy change.
            _, current_config = self._configuration()
            if current_config != config:
                raise ValueError("expert memory configuration changed")
            memory = Memory(namespace=namespace, backend=backend, backend_config=backend_config)
            try:
                service = MemoryService(memory, host="octop-project-task")
                if method == "management_operation":
                    operation: Callable[[Memory], Any] = kwargs["operation"]
                    return operation(memory)
                if method == "capture":
                    if self.worker_id is None:
                        return self._capture(memory, expected_configuration=config, **kwargs)
                    turn = self.services.project_task_turn_repo.get(self.turn_id)
                    if turn is None:
                        return None
                    with checkpoint_fence(self.services, turn):
                        current = self.services.project_task_turn_repo.get(self.turn_id)
                        if (
                            current is None
                            or current.status != "running"
                            or current.worker_id != self.worker_id
                            or current.cancel_requested
                        ):
                            return None
                        return self._capture(memory, expected_configuration=config, **kwargs)
                return getattr(service, method)(**kwargs)
            finally:
                memory.backend.close()

    def management_operation(self, source: str, operation: Callable[[Memory], Any]) -> Any:
        """Run a server-owned maintenance operation under the same scope lock."""
        return self._call(source, "management_operation", operation=operation)

    @contextlib.contextmanager
    def _capture_guard(self, expected_configuration: dict[str, Any]) -> Iterator[None]:
        # The job is enqueued before this boundary. No model I/O runs while
        # holding these locks; a successful revoke/cancel fences later captures.
        with self.services.db.transaction() as conn:
            if not self.services.project_task_repo.lock_owner(conn, self.owner_id):
                raise ValueError("expert memory user is disabled")
            if self.services.db.dialect == "postgresql":
                conn.execute(
                    "SELECT agent_id FROM agents WHERE agent_id=? FOR UPDATE",
                    (str(self.profile["expert_id"]),),
                ).fetchone()
                conn.execute(
                    "SELECT turn_id FROM project_task_turns WHERE turn_id=? FOR UPDATE",
                    (self.turn_id,),
                ).fetchone()
            _, current = self._configuration()
            policy = current.get("memory") or {}
            if current != expected_configuration or (
                isinstance(policy, dict)
                and (
                    policy.get("memory_enabled") is False or policy.get("capture_enabled") is False
                )
            ):
                raise ValueError("expert memory capture policy changed")
            if self.worker_id is not None:
                turn = self.services.project_task_turn_repo.get(self.turn_id)
                if (
                    turn is None
                    or turn.status != "running"
                    or turn.worker_id != self.worker_id
                    or turn.cancel_requested
                ):
                    raise ValueError("expert memory turn is no longer active")
            yield

    def _prompt_snapshot(self, query: str) -> str:
        # A resumed graph sees the same recall text as its original execution.
        # Authorization is checked again before serving even a local snapshot.
        row, config = self._configuration()
        policy = config.get("memory") or {}
        if isinstance(policy, dict) and policy.get("memory_enabled") is False:
            raise ValueError("expert memory is disabled")
        if "legacy" in self.namespaces and (
            row.user_id != self.owner_id
            or not isinstance(policy, dict)
            or policy.get("legacy_recall_enabled") is not True
        ):
            raise ValueError("legacy expert memory is not enabled")
        path = self.journal_path("recall")
        identity = {
            "owner_id": self.owner_id,
            "expert_id": self.profile["expert_id"],
            "profile_digest": self.profile["profile_digest"],
            "query_digest": hashlib.sha256(query.encode()).hexdigest(),
            "namespaces": self.namespaces,
        }
        with _scope_lock(path.with_suffix(".lock")):
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("identity") != identity:
                    raise ValueError("expert memory recall snapshot identity changed")
                return str(cached["content"])
            parts = []
            for source in self.namespaces:
                result = self._call(source, "recall", query=query)
                if result.rendered:
                    parts.append(f"[{source}]\n{result.rendered}")
            content = "\n\n".join(parts)
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps({"identity": identity, "content": content}))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            return content

    async def prompt(self, query: str) -> str:
        if not self.enabled:
            return ""
        return await asyncio.to_thread(self._prompt_snapshot, query)

    def tools(self, locale: str) -> list[StructuredTool]:
        if not self.enabled:
            return []

        async def search(query: str, source: str = "scope", limit: int = 5) -> Any:
            return await asyncio.to_thread(
                self._call, source, "search", query=query, max_results=limit
            )

        async def get(path: str, source: str = "scope") -> Any:
            return await asyncio.to_thread(self._call, source, "get", path=path)

        return [
            StructuredTool.from_function(
                name="expert_memory_search",
                description=tr("projects.expert_memory_search_description", locale),
                args_schema=SearchArgs,
                coroutine=search,
            ),
            StructuredTool.from_function(
                name="expert_memory_get",
                description=tr("projects.expert_memory_get_description", locale),
                args_schema=GetArgs,
                coroutine=get,
            ),
        ]

    def _capture(
        self,
        memory: Memory,
        *,
        prompt: str,
        reply: str,
        expected_configuration: dict[str, Any],
        source_kind: Literal["user", "delegation"] = "user",
    ) -> None:
        path = self.journal_path("capture")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._enqueue_extraction(path, source_kind=source_kind)
        if path.exists():
            previous = json.loads(path.read_text())
            # Unknown cross-store outcomes require reconciliation. Never replay
            # a capture merely because the process died before acknowledging it.
            if previous["status"] != "pending":
                return
        record = {
            "status": "writing",
            "owner_id": self.owner_id,
            "expert_id": self.profile["expert_id"],
            "namespace": self.namespaces["scope"],
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "source_kind": source_kind,
            "source_digest": hashlib.sha256(
                json.dumps([prompt, reply], ensure_ascii=False).encode()
            ).hexdigest(),
        }

        def save() -> None:
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(record))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)

        events = [
            {
                "id": hashlib.sha256(f"{self.turn_id}:{role}".encode()).hexdigest(),
                "event_type": role,
                "content": content,
                "payload": {
                    "source_task_id": self.task_id,
                    "source_turn_id": self.turn_id,
                    "source_kind": source_kind,
                },
            }
            for role, content in (("user_message", prompt), ("assistant_message", reply))
        ]
        with self._capture_guard(expected_configuration):
            save()
            try:
                response = Bridge(memory).handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "capture",
                        "params": {
                            "host": "octop-project-task",
                            "user": str(self.owner_id),
                            "session_id": self.turn_id,
                            "thread_id": self.task_id,
                            "events": events,
                        },
                    }
                )
                if "error" in response:
                    raise RuntimeError("expert memory capture failed")
                record["status"] = "completed"
                record["result"] = response.get("result")
                save()
            except Exception:
                record["status"] = "uncertain"
                save()
                raise

    def _enqueue_extraction(
        self, capture_path: Path, *, source_kind: Literal["user", "delegation"]
    ) -> None:
        turn = self.services.project_task_turn_repo.get(self.turn_id)
        if turn is None:
            return
        _, config = expert_configuration(
            self.services, self.owner_id, str(self.profile["expert_id"])
        )
        policy = config.get("memory") or {}
        if isinstance(policy, dict) and (
            policy.get("auto_extract_enabled") is False
            or policy.get("extract_on_session_end") is False
        ):
            return
        if (self.profile.get("memory") or {}).get("auto_extract_enabled") is False:
            return
        namespace = self.namespaces["scope"]
        job_id = hashlib.sha256(f"{namespace}:{self.turn_id}".encode()).hexdigest()
        self.services.expert_memory_job_repo.enqueue(
            job_id=job_id,
            owner_id=self.owner_id,
            expert_id=str(self.profile["expert_id"]),
            task_id=self.task_id,
            turn_id=self.turn_id,
            namespace=namespace,
            payload_json=json.dumps(
                {
                    "project_id": self.project_id,
                    "profile": {
                        "expert_id": self.profile["expert_id"],
                        "memory": {
                            **self.profile.get("memory", {}),
                            **(
                                {"general_promotion_enabled": False}
                                if source_kind == "delegation"
                                else {}
                            ),
                        },
                    },
                    "source_kind": source_kind,
                    "model_ref": turn.model_ref,
                    "capture_path": str(capture_path.relative_to(self.services.paths.root)),
                    "raw_event_ids": [
                        hashlib.sha256(f"{self.turn_id}:{role}".encode()).hexdigest()
                        for role in ("user_message", "assistant_message")
                    ],
                }
            ),
            now=int(time.time() * 1000),
        )

    async def capture(
        self, prompt: str, reply: str, *, source_kind: Literal["user", "delegation"] = "user"
    ) -> None:
        if not self.capture_enabled:
            return
        try:
            await asyncio.to_thread(
                self._call, "scope", "capture", prompt=prompt, reply=reply, source_kind=source_kind
            )
        except Exception as exc:
            logger.warning(
                "Expert memory capture requires reconciliation: turn=%s cause=%s",
                self.turn_id,
                type(exc).__name__,
            )
