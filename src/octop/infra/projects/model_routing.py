"""Persist task routing policy and make stable, network-free model decisions."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import ProjectModel

SETTINGS_KEY = "task_model_routing"
POLICY_VERSION: Literal["difficulty-v1"] = "difficulty-v1"
RoutingMode = Literal["box", "hybrid"]
RoutingLevel = Literal[0, 20, 40, 60, 80, 100]
ROUTING_MODES: tuple[RoutingMode, ...] = ("box", "hybrid")
ROUTING_LEVELS: tuple[RoutingLevel, ...] = (0, 20, 40, 60, 80, 100)
REMOTE_THRESHOLDS: dict[RoutingLevel, int] = {20: 80, 40: 60, 60: 40, 80: 20, 100: 10}
ModelReference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
Difficulty = Annotated[StrictInt, Field(ge=0, le=100)]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


def _strict_level(value: object) -> object:
    if type(value) is not int or value not in ROUTING_LEVELS:
        raise ValueError("invalid routing level")
    return value


class RoutingRequest(ProjectModel):
    mode: RoutingMode
    level: RoutingLevel | None = Field(
        default=None, description="混合模式难度档位；省略使用服务端默认档位。盒子模式仅允许 0。"
    )

    @field_validator("level", mode="before")
    @classmethod
    def strict_level(cls, value: object) -> object:
        return None if value is None else _strict_level(value)

    @model_validator(mode="after")
    def box_level(self) -> Self:
        if self.mode == "box" and self.level not in {None, 0}:
            raise ValueError("box mode requires level 0")
        return self


class ModelRoutingSettings(ProjectModel):
    box_model_ref: ModelReference | None = None
    remote_model_ref: ModelReference | None = None
    default_level: RoutingLevel = 0

    @field_validator("default_level", mode="before")
    @classmethod
    def strict_level(cls, value: object) -> object:
        return _strict_level(value)

    @field_validator("box_model_ref", "remote_model_ref")
    @classmethod
    def complete_reference(cls, value: str | None) -> str | None:
        if value is not None:
            provider, separator, model = value.partition("/")
            if not separator or not provider.strip() or not model.strip():
                raise ValueError("a complete provider/model reference is required")
        return value

    @model_validator(mode="after")
    def distinct_references(self) -> Self:
        if self.box_model_ref is not None and self.box_model_ref == self.remote_model_ref:
            raise ValueError("box and remote references must be distinct")
        return self


class ModelRoutingDecision(ProjectModel):
    mode: RoutingMode
    level: RoutingLevel
    target: Literal["box", "remote", "explicit"]
    model_ref: ModelReference
    difficulty: Difficulty | None = None
    reason_code: ReasonCode
    policy_version: Literal["difficulty-v1"] = POLICY_VERSION
    input_digest: str = ""
    classifier_input_tokens: Annotated[StrictInt, Field(ge=0)] = 0
    classifier_output_tokens: Annotated[StrictInt, Field(ge=0)] = 0
    classifier_duration_ms: Annotated[StrictInt, Field(ge=0)] | None = None
    classifier_input_truncated: StrictBool = False


class RoutingCapabilities(ProjectModel):
    modes: list[RoutingMode] = Field(default_factory=lambda: list(ROUTING_MODES))
    levels: list[RoutingLevel] = Field(default_factory=lambda: list(ROUTING_LEVELS))
    default_level: RoutingLevel
    box_model_ref: str | None
    remote_model_ref: str | None
    box_available: bool
    remote_available: bool


def _settings_invalid() -> OctopError:
    return OctopError.localized(
        ErrorCode.TASK_SETTINGS_INVALID, setting=SETTINGS_KEY, details={"setting": SETTINGS_KEY}
    )


def load_settings(services: SharedServices) -> ModelRoutingSettings:
    raw = services.settings_repo.get(SETTINGS_KEY)
    if raw is None:
        return ModelRoutingSettings()
    try:
        return ModelRoutingSettings.model_validate_json(raw)
    except ValidationError as exc:
        raise _settings_invalid() from exc


def _source_available(providers: ProviderStore, ref: str | None, *, box: bool) -> bool:
    if not ref or not providers.is_model_ref_usable(ref):
        return False
    provider_name = ref.partition("/")[0]
    return any(
        row.name == provider_name and (row.kind == "box") == box
        for row in providers.iter_usable_rows()
    )


def save_settings(services: SharedServices, settings: ModelRoutingSettings) -> ModelRoutingSettings:
    providers = ProviderStore(services.provider_repo)
    if not _source_available(providers, settings.box_model_ref, box=True):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
    if settings.default_level > 0 and not _source_available(
        providers, settings.remote_model_ref, box=False
    ):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
    services.settings_repo.set(SETTINGS_KEY, settings.model_dump_json())
    return settings


def routing_capabilities(services: SharedServices) -> RoutingCapabilities:
    settings = load_settings(services)
    providers = ProviderStore(services.provider_repo)
    return RoutingCapabilities(
        **settings.model_dump(),
        box_available=_source_available(providers, settings.box_model_ref, box=True),
        remote_available=_source_available(providers, settings.remote_model_ref, box=False),
    )


def decide_model(
    providers: ProviderStore,
    settings: ModelRoutingSettings,
    *,
    mode: RoutingMode = "hybrid",
    level: RoutingLevel | None = None,
    explicit_model_ref: str | None = None,
    difficulty: int | None = None,
    reason_code: str | None = None,
    requires_images: bool = False,
) -> ModelRoutingDecision:
    """Apply explicit overrides, capability constraints, then a difficulty threshold."""
    try:
        request = RoutingRequest(mode=mode, level=level)
    except ValidationError as exc:
        raise _settings_invalid() from exc
    selected_level: RoutingLevel = (
        0
        if request.mode == "box"
        else request.level
        if request.level is not None
        else settings.default_level
    )
    if explicit_model_ref:
        if not providers.is_model_ref_usable(explicit_model_ref):
            raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
        if requires_images and not providers.is_model_ref_multimodal(explicit_model_ref):
            raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
        return ModelRoutingDecision(
            mode=mode,
            level=selected_level,
            target="explicit",
            model_ref=explicit_model_ref,
            reason_code="explicit_model",
        )

    if not _source_available(providers, settings.box_model_ref, box=True):
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
    assert settings.box_model_ref is not None
    target: Literal["box", "remote"] = "box"
    selected_ref = settings.box_model_ref
    selected_reason = "box_mode" if mode == "box" else "level_zero"
    selected_difficulty = None
    if selected_level > 0:
        if difficulty is not None and (type(difficulty) is not int or not 0 <= difficulty <= 100):
            raise _settings_invalid()
        selected_difficulty = difficulty
        selected_reason = reason_code or (
            "classifier_unavailable" if difficulty is None else "difficulty_below_threshold"
        )
        if requires_images and not providers.is_model_ref_multimodal(selected_ref):
            if _source_available(providers, settings.remote_model_ref, box=False) and (
                providers.is_model_ref_multimodal(settings.remote_model_ref or "")
            ):
                target = "remote"
                selected_ref = settings.remote_model_ref or ""
                selected_reason = "capability_image"
            else:
                raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
        elif (
            requires_images
            and _source_available(providers, settings.remote_model_ref, box=False)
            and not providers.is_model_ref_multimodal(settings.remote_model_ref or "")
        ):
            selected_reason = "capability_image_box"
        elif difficulty is not None and difficulty >= REMOTE_THRESHOLDS[selected_level]:
            if not _source_available(providers, settings.remote_model_ref, box=False):
                raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE)
            target = "remote"
            selected_ref = settings.remote_model_ref or ""
            selected_reason = reason_code or "difficulty_at_or_above_threshold"
    if requires_images and not providers.is_model_ref_multimodal(selected_ref):
        raise OctopError.localized(ErrorCode.TASK_IMAGE_MODEL_REQUIRED)
    return ModelRoutingDecision(
        mode=mode,
        level=selected_level,
        target=target,
        model_ref=selected_ref,
        difficulty=selected_difficulty,
        reason_code=selected_reason,
    )
