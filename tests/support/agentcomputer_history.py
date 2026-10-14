"""Isolated versioned history for AgentComputer engine boundary tests."""

from __future__ import annotations

from octop.infra.db.services import SharedServices
from octop.infra.history.service import HistoryArchive
from octop.infra.history.store import HistoryStore


def open_history_archive(services: SharedServices, *, enabled: bool = True) -> HistoryArchive:
    return HistoryArchive(
        HistoryStore(
            services.paths.root / "history_v2.sqlite",
            identity=str(services.paths.db.resolve()),
        ),
        services.thread_message_repo,
        services.trajectory_event_repo,
        enabled=enabled,
    )
