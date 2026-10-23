"""Task routing levels select by difficulty and persist only credential-free policy."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.providers import ProviderRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.model_routing import (
    REMOTE_THRESHOLDS,
    SETTINGS_KEY,
    ModelRoutingDecision,
    ModelRoutingSettings,
    RoutingRequest,
    decide_model,
    load_settings,
    routing_capabilities,
    save_settings,
)


@pytest.fixture
def services(tmp_path: Path):
    db = SqlitePool(tmp_path / "routing.sqlite")
    run_migrations(db)
    providers = ProviderRepo(db)
    for name, kind, key, model, inputs in (
        ("box", "box", "", "local", ["text"]),
        ("cloud", "openai", "fixture-secret", "strong", ["text", "image"]),
        ("another", "openai", "fixture-secret-two", "chat", ["text"]),
    ):
        providers.create(
            name=name,
            kind=kind,
            api_key=key,
            base_url=f"http://{name}.invalid/v1",
            models_json=json.dumps([{"id": model, "enabled": True, "input": inputs}]),
        )
    yield SimpleNamespace(settings_repo=SettingsRepo(db), provider_repo=providers)
    db.close()


def configured(**updates) -> ModelRoutingSettings:
    return ModelRoutingSettings(
        **{"box_model_ref": "box/local", "remote_model_ref": "cloud/strong", **updates}
    )


@pytest.mark.parametrize("value", [True, False, 20.0, 40.5, "20", -20, 1, 99, 120])
def test_routing_levels_are_strict_and_limited_to_six_choices(value) -> None:
    with pytest.raises(ValidationError):
        RoutingRequest(mode="hybrid", level=value)
    with pytest.raises(ValidationError):
        ModelRoutingSettings(default_level=value)


def test_box_request_only_accepts_zero_or_omitted_level() -> None:
    assert RoutingRequest(mode="box").level is None
    assert RoutingRequest(mode="box", level=0).level == 0
    with pytest.raises(ValidationError):
        RoutingRequest(mode="box", level=20)
    with pytest.raises(ValidationError):
        RoutingRequest(mode="remote")
    with pytest.raises(ValidationError):
        configured(remote_model_ref="box/local")


@pytest.mark.parametrize("level,threshold", list(REMOTE_THRESHOLDS.items()))
def test_each_level_routes_at_its_difficulty_boundary(services, level, threshold) -> None:
    store = ProviderStore(services.provider_repo)
    below = decide_model(store, configured(), level=level, difficulty=threshold - 1)
    reached = decide_model(store, configured(), level=level, difficulty=threshold)
    assert (below.target, below.model_ref) == ("box", "box/local")
    assert (reached.target, reached.model_ref) == ("remote", "cloud/strong")
    assert below.difficulty == threshold - 1 and reached.difficulty == threshold
    assert reached.policy_version == "difficulty-v1"


def test_higher_levels_never_move_the_same_task_back_from_remote_to_box(services) -> None:
    store = ProviderStore(services.provider_repo)
    for difficulty in (0, 9, 10, 19, 20, 39, 40, 59, 60, 79, 80, 100):
        targets = [
            decide_model(store, configured(), level=level, difficulty=difficulty).target
            for level in (0, 20, 40, 60, 80, 100)
        ]
        assert targets == sorted(targets)


@pytest.mark.parametrize("mode", ["box", "hybrid"])
def test_zero_uses_only_box_and_does_not_need_a_remote_provider(services, mode) -> None:
    services.provider_repo.update(services.provider_repo.get_by_name("cloud").id, enabled=False)
    decision = decide_model(
        ProviderStore(services.provider_repo),
        configured(remote_model_ref=None, default_level=80),
        mode=mode,
        level=0,
        difficulty=100,
    )
    assert decision.model_ref == "box/local" and decision.target == "box"
    assert decision.level == 0 and decision.difficulty is None


def test_box_mode_ignores_nonzero_server_default(services) -> None:
    decision = decide_model(
        ProviderStore(services.provider_repo), configured(default_level=80), mode="box"
    )
    assert decision.level == 0 and decision.reason_code == "box_mode"


def test_hybrid_uses_server_default_and_never_guesses_another_remote(services) -> None:
    store = ProviderStore(services.provider_repo)
    decision = decide_model(store, configured(default_level=60), difficulty=50)
    assert decision.level == 60 and decision.model_ref == "cloud/strong"
    with pytest.raises(OctopError) as exc:
        decide_model(store, configured(remote_model_ref=None), level=100, difficulty=90)
    assert exc.value.code == ErrorCode.TASK_MODEL_UNAVAILABLE


def test_explicit_model_wins_without_requiring_configured_routing_sources(services) -> None:
    store = ProviderStore(services.provider_repo)
    decision = decide_model(
        store, ModelRoutingSettings(), mode="box", explicit_model_ref="another/chat"
    )
    assert decision.target == "explicit" and decision.model_ref == "another/chat"
    assert decision.reason_code == "explicit_model" and decision.difficulty is None
    with pytest.raises(OctopError) as exc:
        decide_model(store, configured(), explicit_model_ref="absent/model")
    assert exc.value.code == ErrorCode.TASK_MODEL_UNAVAILABLE


def test_unavailable_classifier_stays_on_box_and_preserves_failure_code(services) -> None:
    decision = decide_model(
        ProviderStore(services.provider_repo),
        configured(),
        level=100,
        difficulty=None,
        reason_code="classifier_timeout",
    )
    assert decision.target == "box" and decision.reason_code == "classifier_timeout"
    assert decision.difficulty is None


@pytest.mark.parametrize("difficulty", [True, False, -1, 101, 10.0, "20"])
def test_invalid_difficulty_does_not_route(services, difficulty) -> None:
    with pytest.raises(OctopError) as exc:
        decide_model(
            ProviderStore(services.provider_repo), configured(), level=20, difficulty=difficulty
        )
    assert exc.value.code == ErrorCode.TASK_SETTINGS_INVALID


def test_images_can_select_explicit_remote_in_hybrid_but_not_in_box_or_zero(services) -> None:
    store = ProviderStore(services.provider_repo)
    decision = decide_model(store, configured(), level=20, requires_images=True)
    assert decision.target == "remote" and decision.reason_code == "capability_image"
    for mode, level in (("box", 0), ("hybrid", 0)):
        with pytest.raises(OctopError) as exc:
            decide_model(store, configured(), mode=mode, level=level, requires_images=True)
        assert exc.value.code == ErrorCode.TASK_IMAGE_MODEL_REQUIRED
    with pytest.raises(OctopError) as exc:
        decide_model(
            store, configured(), level=100, requires_images=True, explicit_model_ref="box/local"
        )
    assert exc.value.code == ErrorCode.TASK_IMAGE_MODEL_REQUIRED


def test_wrong_source_configuration_is_rejected_without_cross_source_guessing(services) -> None:
    store = ProviderStore(services.provider_repo)
    with pytest.raises(OctopError) as exc:
        decide_model(store, configured(box_model_ref="another/chat"), mode="box")
    assert exc.value.code == ErrorCode.TASK_MODEL_UNAVAILABLE
    with pytest.raises(OctopError) as exc:
        decide_model(
            store, configured(remote_model_ref="box/local-other"), level=100, difficulty=90
        )
    assert exc.value.code == ErrorCode.TASK_MODEL_UNAVAILABLE


def test_image_capable_box_remains_selected_when_remote_supports_only_text(services) -> None:
    local = services.provider_repo.get_by_name("box")
    services.provider_repo.update(
        local.id,
        models_json=json.dumps([{"id": "local", "enabled": True, "input": ["text", "image"]}]),
    )
    store = ProviderStore(services.provider_repo)
    settings = configured(remote_model_ref="another/chat")
    for difficulty in (None, 0, 100):
        decision = decide_model(
            store, settings, level=100, difficulty=difficulty, requires_images=True
        )
        assert decision.model_ref == "box/local" and decision.target == "box"
        assert decision.reason_code == "capability_image_box"
    with pytest.raises(OctopError) as exc:
        decide_model(
            store,
            settings,
            level=100,
            difficulty=100,
            requires_images=True,
            explicit_model_ref="another/chat",
        )
    assert exc.value.code == ErrorCode.TASK_IMAGE_MODEL_REQUIRED


def test_settings_round_trip_does_not_store_or_expose_provider_credentials(services) -> None:
    assert load_settings(services) == ModelRoutingSettings()
    saved = save_settings(services, configured(default_level=40))
    assert load_settings(services) == saved
    raw = services.settings_repo.get(SETTINGS_KEY)
    resources = routing_capabilities(services).model_dump()
    assert resources == {
        "modes": ["box", "hybrid"],
        "levels": [0, 20, 40, 60, 80, 100],
        "default_level": 40,
        "box_model_ref": "box/local",
        "remote_model_ref": "cloud/strong",
        "box_available": True,
        "remote_available": True,
    }
    assert "fixture-secret" not in raw + json.dumps(resources)
    assert "base_url" not in raw + json.dumps(resources)


def test_zero_settings_allow_an_unavailable_remote_but_hybrid_default_requires_it(services) -> None:
    save_settings(services, configured(remote_model_ref=None, default_level=0))
    assert routing_capabilities(services).remote_available is False
    services.provider_repo.update(services.provider_repo.get_by_name("cloud").id, enabled=False)
    save_settings(services, configured(default_level=0))
    assert routing_capabilities(services).remote_available is False
    with pytest.raises(OctopError) as exc:
        save_settings(services, configured(default_level=20))
    assert exc.value.code == ErrorCode.TASK_MODEL_UNAVAILABLE


@pytest.mark.parametrize("raw", ["not-json", '{"default_level":true}', '{"default_level":30}'])
def test_corrupt_policy_is_a_localized_settings_error(services, raw) -> None:
    services.settings_repo.set(SETTINGS_KEY, raw)
    with pytest.raises(OctopError) as exc:
        load_settings(services)
    assert exc.value.code == ErrorCode.TASK_SETTINGS_INVALID
    assert exc.value.details == {"setting": SETTINGS_KEY}


def test_decision_audit_fields_reject_negative_usage() -> None:
    values = {
        "mode": "box",
        "level": 0,
        "target": "box",
        "model_ref": "box/local",
        "reason_code": "box_mode",
    }
    decision = ModelRoutingDecision(**values)
    assert decision.classifier_input_tokens == decision.classifier_output_tokens == 0
    assert decision.input_digest == "" and decision.classifier_duration_ms is None
    assert decision.classifier_input_truncated is False
    with pytest.raises(ValidationError):
        ModelRoutingDecision(**values, classifier_input_tokens=-1)
