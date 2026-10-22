"""Directory login prepares each user's private steward before returning."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.config import OctopConfig
from octop.infra.auth.ldap.client import LdapIdentity
from octop.infra.auth.ldap.service import LdapAuthService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import User
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = PathLayout(tmp_path / ".octop")
    monkeypatch.setenv("OCTOP_HOME", str(paths.root))
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    service = LdapAuthService(services, UserManager(services))
    service.put_config(
        {
            "enabled": True,
            "server_url": "ldaps://directory.example.org",
            "user_base_dn": "dc=example,dc=org",
            "auto_provision": True,
        },
        actor="admin",
    )
    yield service
    db.close()


def _identity() -> LdapIdentity:
    return LdapIdentity(
        dn="uid=alice,dc=example,dc=org",
        username="alice",
        email="alice@example.org",
        display_name="Alice",
        groups=(),
        subject="uuid-alice",
    )


async def test_ldap_provision_prepares_stable_resident(service: LdapAuthService):
    user = await service.resolve_user(_identity())
    services = service._services
    binding = services.resident_conversation_repo.get(user.id)
    assert binding is not None and binding.status == "ready"
    assert services.agent_repo.get(binding.agent_id).user_id == user.id
    assert services.thread_repo.get(binding.thread_id).user_id == user.id
    permissions = services.user_repo.get(user.id).permissions

    again = await service.resolve_user(_identity())
    assert again.id == user.id
    assert services.resident_conversation_repo.get(user.id) == binding
    assert len(services.agent_repo.list_by_user(user.id)) == 1
    assert services.user_repo.get(user.id).permissions == permissions


@pytest.mark.parametrize("cached", [False, True])
async def test_ldap_login_prepares_existing_user(service: LdapAuthService, cached: bool):
    services = service._services
    provider = services.sso_repo.get_by_kind("ldap")
    assert provider is not None
    user_id = services.user_repo.create(
        username="alice",
        password_hash=None,
        role="user",
        permissions=["channels"],
        sso_provider_id=provider.id,
        sso_subject="uuid-alice",
    )
    if cached:
        service._user_manager.register_cached_user(
            User(
                id=user_id,
                username="alice",
                role="user",
                display_name=None,
                permissions=["channels"],
            )
        )
    assert services.resident_conversation_repo.get(user_id) is None

    user = await service.resolve_user(_identity())
    assert user.id == user_id
    binding = services.resident_conversation_repo.get(user_id)
    assert binding is not None and binding.status == "ready"
    assert services.user_repo.get(user_id).permissions == ["channels"]
    assert user.permissions == ["channels"]


async def test_ldap_disabled_user_does_not_receive_resident(service: LdapAuthService):
    services = service._services
    provider = services.sso_repo.get_by_kind("ldap")
    assert provider is not None
    user_id = services.user_repo.create(
        username="alice",
        password_hash=None,
        role="user",
        sso_provider_id=provider.id,
        sso_subject="uuid-alice",
    )
    services.user_repo.set_disabled(user_id, True)

    with pytest.raises(OctopError) as error:
        await service.resolve_user(_identity())
    assert error.value.code == ErrorCode.USER_DISABLED
    assert services.resident_conversation_repo.get(user_id) is None
