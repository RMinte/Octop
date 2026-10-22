"""Hot-sync DB provider configs into a shared ``HarnessAgentManager`` factory."""

from __future__ import annotations

import logging
from typing import Any

from octop_harness import HarnessAgentManager
from octop_harness.config import ProviderConfig

from octop.infra.agents.providers.box import OctopChatModelFactory

logger = logging.getLogger(__name__)


class OctopHarnessAgentManager(HarnessAgentManager):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self._shared_factory is not None:
            self._shared_factory = OctopChatModelFactory(self._shared_factory.provider_configs())

    def add_provider(self, provider: ProviderConfig) -> None:
        if self._shared_factory is None:
            self._shared_factory = OctopChatModelFactory([provider])
        else:
            super().add_provider(provider)


def sync_providers_to_harness(
    harness_manager: HarnessAgentManager,
    providers: list[ProviderConfig],
    *,
    shared_factory: Any | None,
) -> None:
    """Apply *providers* to the harness manager's shared model factory."""
    if shared_factory is None:
        if not providers:
            return
        for provider in providers:
            try:
                harness_manager.add_provider(provider)
            except Exception:
                logger.exception("add_provider %s failed", provider.id)
        return

    new_ids = {provider.id for provider in providers}
    existing_ids = set(getattr(shared_factory, "_providers", {}).keys())
    for provider_id in existing_ids - new_ids:
        try:
            harness_manager.remove_provider(provider_id)
        except Exception:
            logger.exception("remove_provider %s failed", provider_id)
    for provider in providers:
        try:
            harness_manager.add_provider(provider)
        except Exception:
            logger.exception("add_provider %s failed", provider.id)


__all__ = ["OctopHarnessAgentManager", "sync_providers_to_harness"]
