"""Patch octop-harness so tests run without a real LLM."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from tests.support.fakes import FakeHarnessAgent


def _sync_fake_skill_dirs(agent: FakeHarnessAgent, config: Any) -> None:
    """Mirror harness ``skills_dir`` onto the fake so package skills list correctly."""
    skills_dir = getattr(config, "skills_dir", None)
    agent.config.skills_dir = skills_dir
    roots: list[dict[str, str]] = []
    for raw in skills_dir or ():
        path = Path(str(raw))
        package_id = ""
        parts = path.parts
        if "skill-packages" in parts:
            idx = parts.index("skill-packages")
            if idx + 1 < len(parts):
                package_id = parts[idx + 1]
        if package_id:
            roots.append({"id": package_id, "path": str(path)})
    agent.config.skill_package_roots = roots or None


def _wire_add_provider(mock_manager: MagicMock) -> None:
    def _add_provider(provider: Any) -> None:
        mock_manager._providers.append(provider)
        if mock_manager._shared_factory is None:
            factory = MagicMock()
            factory._providers = {provider.id: provider}
            mock_manager._shared_factory = factory
            mock_manager.shared_factory = factory
        else:
            mock_manager._shared_factory._providers[provider.id] = provider

    mock_manager.add_provider = MagicMock(side_effect=_add_provider)
    mock_manager.remove_provider = MagicMock()


def patch_resident_stream(monkeypatch: Any, stream: Any) -> None:
    """Replace only the model stream, retaining native Task claims and guards."""
    from langchain_core.messages import AIMessage, convert_to_messages

    from octop.infra.agents.resident_identity import ResidentTextOnlyMiddleware

    async def model_stream(self: Any, request: dict[str, Any]) -> Any:
        thread_id = next(
            middleware.thread_id
            for middleware in self.config.middleware
            if isinstance(middleware, ResidentTextOnlyMiddleware)
        )
        messages = convert_to_messages(request["messages"])
        incoming = messages[-1]
        message = SimpleNamespace(
            text=incoming.content,
            agent_id=request["agent_id"],
            user_id=int(request["user"]),
            metadata={
                "thread_id": thread_id,
                "_resident_request_id": incoming.additional_kwargs["resident_request_id"],
            },
        )
        reply = ""
        async for event in stream(message):
            if event.get("type") == "token":
                reply += str(event.get("content") or "")
            yield event
        self.seed_thread_messages(request["thread_id"], [*messages, AIMessage(content=reply)])

    monkeypatch.setattr(FakeHarnessAgent, "stream", model_stream)


def patch_task_stream(monkeypatch: Any, stream: Any) -> None:
    """Program ordinary Tasks without intercepting the private resident runtime."""
    import json

    from octop.infra.projects import resident_tasks
    from octop.infra.projects import runtime as task_runtime

    async def run(services: Any, owner: int, turn: Any, history: list[Any]) -> Any:
        target = (
            resident_tasks.run
            if json.loads(turn.configuration_json).get("execution_kind") == "resident"
            else stream
        )
        async for event in target(services, owner, turn, history):
            yield event

    monkeypatch.setattr(task_runtime, "run_turn", run)


def build_harness_manager_mock(
    *,
    fake_agent: Any | None = None,
    providers: list[Any] | None = None,
) -> MagicMock:
    """Return a MagicMock that quacks like HarnessAgentManager."""
    from octop_harness.manager import AgentEntry  # type: ignore[import]

    agent = fake_agent if fake_agent is not None else FakeHarnessAgent()
    entries: dict[str, AgentEntry] = {}

    def _create(config: Any, agent_id: str | None = None, **_kw: Any) -> AgentEntry:
        aid = agent_id or "fake"
        if isinstance(agent, FakeHarnessAgent):
            entry_agent: Any = FakeHarnessAgent(
                chunks=list(agent.chunks),
                raise_on_stream=agent.raise_on_stream,
            )
        else:
            entry_agent = agent
        ws_dir = getattr(config, "workspace_dir", None)
        if ws_dir is not None and isinstance(entry_agent, FakeHarnessAgent):
            backend_cfg = getattr(config, "backend", None)
            virtual_mode = True
            if isinstance(backend_cfg, dict):
                virtual_mode = bool(backend_cfg.get("virtual_mode", True))
                root_dir = str(backend_cfg.get("root_dir") or "").strip()
                if virtual_mode and root_dir not in {"", "/", "\\"}:
                    ws_dir = Path(root_dir) / str(ws_dir).lstrip("/\\")
            entry_agent.use_workspace_dir(Path(str(ws_dir)), virtual_mode=virtual_mode)
        if isinstance(entry_agent, FakeHarnessAgent):
            _sync_fake_skill_dirs(entry_agent, config)
        entry = AgentEntry(
            agent_id=aid,
            agent=entry_agent,
            config=config,
            metadata={},
            tags=[],
            created_at=datetime.now(tz=UTC),
        )
        entries[aid] = entry
        return entry

    def _get_agent(agent_id: str) -> AgentEntry:
        return entries[agent_id]

    def _remove(agent_id: str) -> None:
        entries.pop(agent_id, None)

    async def _acreate(config: Any, agent_id: str | None = None, **kw: Any) -> AgentEntry:
        entry = _create(config, agent_id=agent_id, **kw)
        if kw.get("init_workspace", True) and isinstance(entry.agent, FakeHarnessAgent):
            await entry.agent.seed_default_subagent()
        return entry

    async def _arebuild(agent_id: str, config: Any, **kw: Any) -> AgentEntry:
        old_entry = entries.get(agent_id)
        old_agent = old_entry.agent if old_entry else None
        # Reuse the existing FakeHarnessAgent on reload so in-memory state
        # (workspace files, ``skills_disabled``, …) survives the rebuild — a
        # real running harness agent is the same object across reloads.
        if isinstance(old_agent, FakeHarnessAgent):
            ws_dir = old_agent._workspace_dir
            backend_cfg = getattr(config, "backend", None)
            virtual_mode = True
            if isinstance(backend_cfg, dict):
                virtual_mode = bool(backend_cfg.get("virtual_mode", True))
            old_agent.use_workspace_dir(ws_dir, virtual_mode=virtual_mode)
            _sync_fake_skill_dirs(old_agent, config)
            entry = AgentEntry(
                agent_id=agent_id,
                agent=old_agent,
                config=config,
                metadata={},
                tags=[],
                created_at=datetime.now(tz=UTC),
            )
            entries[agent_id] = entry
            return entry
        ws_dir = old_agent._workspace_dir if isinstance(old_agent, FakeHarnessAgent) else None
        _remove(agent_id)
        entry = await _acreate(config, agent_id=agent_id, init_workspace=False, **kw)
        if ws_dir is not None and isinstance(entry.agent, FakeHarnessAgent):
            backend_cfg = getattr(config, "backend", None)
            virtual_mode = True
            if isinstance(backend_cfg, dict):
                virtual_mode = bool(backend_cfg.get("virtual_mode", True))
            entry.agent.use_workspace_dir(ws_dir, virtual_mode=virtual_mode)
            _sync_fake_skill_dirs(entry.agent, config)
        return entry

    async def _aremove(agent_id: str) -> None:
        _remove(agent_id)

    mock_manager = MagicMock()
    mock_manager.aclose = AsyncMock()
    mock_manager.create_agent.side_effect = _create
    mock_manager.acreate_agent = AsyncMock(side_effect=_acreate)
    mock_manager.arebuild_agent = AsyncMock(side_effect=_arebuild)
    mock_manager.aremove_agent = AsyncMock(side_effect=_aremove)
    mock_manager.get_agent.side_effect = _get_agent
    mock_manager.remove_agent.side_effect = _remove

    def _stream(agent_id: str, request: Any) -> Any:
        return _get_agent(agent_id).agent.stream(request)

    def _call(agent_id: str, request: Any) -> Any:
        agent_obj = _get_agent(agent_id).agent
        if hasattr(agent_obj, "call"):
            return agent_obj.call(request)
        raise NotImplementedError("fake agent has no call()")

    def _resume_hitl(agent_id: str, thread_id: str, decisions: Any) -> Any:
        agent_obj = _get_agent(agent_id).agent
        if hasattr(agent_obj, "resume_hitl"):
            return agent_obj.resume_hitl(thread_id, decisions)
        raise NotImplementedError("fake agent has no resume_hitl()")

    mock_manager.stream = MagicMock(side_effect=_stream)
    mock_manager.call = MagicMock(side_effect=_call)
    mock_manager.resume_hitl = MagicMock(side_effect=_resume_hitl)
    mock_manager._shared_factory = None
    mock_manager.shared_factory = None
    mock_manager._providers = []
    mock_manager.team = MagicMock()
    _wire_add_provider(mock_manager)
    for provider in providers or []:
        mock_manager.add_provider(provider)
    return mock_manager


@contextmanager
def patch_harness(fake_agent: Any | None = None):
    """Patch the AgentManager harness layer so tests run without a real LLM."""
    agent = fake_agent if fake_agent is not None else FakeHarnessAgent()

    def _constructor(**kw: Any) -> MagicMock:
        return build_harness_manager_mock(
            fake_agent=agent,
            providers=kw.get("providers"),
        )

    class TaskResidentHarness(FakeHarnessAgent):
        """Expose the short-lived Task harness surface to existing test streams."""

        def __init__(self, config: Any, *, agent_id: str) -> None:
            super().__init__(
                chunks=list(agent.chunks),
                raise_on_stream=agent.raise_on_stream,
                workspace_dir=Path(config.workspace_dir),
                system_files_path=config.system_files_path or "",
            )
            self.config = config
            self.agent_id = agent_id
            self.checkpointer = config.checkpointer
            self._base_graph = self.graph
            self.protocol = SimpleNamespace(_graph=self.graph)
            self.memory = None

        async def stream(self, request: Any, *, protocol: Any = None) -> Any:
            from langchain_core.messages import AIMessage, convert_to_messages

            request = request if isinstance(request, dict) else vars(request)
            reply = ""
            async for chunk in FakeHarnessAgent.stream(self, request):
                if chunk.get("type") == "token":
                    reply += str(chunk.get("content") or "")
                yield chunk
            if not await self.aget_history(request["thread_id"], limit=100_000):
                messages = convert_to_messages(request["messages"])
                if reply:
                    messages.append(AIMessage(content=reply))
                self.seed_thread_messages(request["thread_id"], messages)

        async def aclose(self) -> None:
            self.close()

    with (
        patch(
            "octop.infra.agents.manager.HarnessAgentManager",
            side_effect=_constructor,
        ),
        patch("octop.infra.projects.resident_tasks.HarnessAgent", TaskResidentHarness),
        patch("octop.infra.projects.title.ChatModelFactory") as title_factory,
        patch("octop.infra.projects.narration.ChatModelFactory") as narration_factory,
    ):
        from langchain_core.messages import AIMessage

        title_model = title_factory.return_value.get_chat_model.return_value
        title_model._harness_max_output_tokens = 8_192
        title_model.ainvoke = AsyncMock(return_value=AIMessage(content=""))
        title_factory.return_value.aclose = AsyncMock()
        narration_model = narration_factory.return_value.get_chat_model.return_value
        narration_model._harness_max_output_tokens = 512
        narration_model.model_copy.return_value = narration_model
        narration_model.ainvoke = AsyncMock(return_value=AIMessage(content=""))
        narration_factory.return_value.aclose = AsyncMock()
        yield agent
