"""Read independent logical model invocation receipts without changing legacy totals."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from octop.infra.db.repos.model_calls import ModelCallSource, ModelCallStatus

if TYPE_CHECKING:
    from octop.infra.db.services import SharedServices


class ModelCallFilters(BaseModel):
    agent_id: str | None = Field(default=None, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    task_id: str | None = Field(default=None, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    submission_id: str | None = Field(default=None, max_length=256)
    provider: str | None = Field(default=None, max_length=256)
    model_ref: str | None = Field(default=None, max_length=512)
    source: ModelCallSource | None = Field(
        default=None,
        description="Product source: box for Box providers, third_party for all others.",
    )
    purpose: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: ModelCallStatus | None = None
    before_id: int | None = Field(
        default=None, gt=0, description="Exclusive cursor from next_before_id."
    )
    limit: int = Field(default=100, ge=1, le=100)


class ModelCallTokenPresence(BaseModel):
    input_tokens: bool = False
    output_tokens: bool = False
    total_tokens: bool = False
    uncached_input_tokens: bool = False
    cache_read_tokens: bool = False
    cache_write_tokens: bool = False
    reasoning_tokens: bool = False


class ModelCallTokens(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    presence: ModelCallTokenPresence = Field(
        default_factory=ModelCallTokenPresence,
        description="True only for fields explicitly reported by the adapter; unknown is null, not zero.",
    )


class ModelCallMetadata(BaseModel):
    version: int | None = None
    trace_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    request_id: str | None = None
    provider_request_id: str | None = None
    usage_source: (
        Literal["usage_metadata", "response_metadata", "llm_output", "mixed", "missing", "derived"]
        | None
    ) = None


class ModelCallReceipt(BaseModel):
    call_id: str
    parent_call_id: str
    submission_id: str
    user_id: int | None
    agent_id: str
    thread_id: str
    project_id: str
    task_id: str
    turn_id: str
    provider: str
    model_ref: str = Field(
        description="Actual factory model, including auxiliary and summary models."
    )
    source: ModelCallSource = Field(
        description="Product source: box for Box providers, third_party for all others; not deployment location."
    )
    purpose: str
    started_at: int = Field(description="Unix milliseconds.")
    finished_at: int | None = Field(description="Unix milliseconds; null while unfinished.")
    status: ModelCallStatus
    usage: ModelCallTokens
    metadata: ModelCallMetadata


class ModelCallPage(BaseModel):
    measurement_unit: Literal["logical_model_invocation"] = "logical_model_invocation"
    recording_scope: Literal["new_calls_only"] = "new_calls_only"
    physical_attempts_available: Literal[False] = False
    included_in_legacy_summary: Literal[False] = False
    timezone: str
    generated_at: int = Field(description="Unix milliseconds.")
    next_before_id: int | None = Field(description="Use as before_id; null means no older page.")
    items: list[ModelCallReceipt]


async def list_model_calls(
    services: SharedServices,
    *,
    user_id: int | None,
    filters: ModelCallFilters,
    timezone: str,
) -> ModelCallPage:
    params = filters.model_dump(exclude={"limit"})
    rows = await asyncio.to_thread(
        services.model_call_repo.list_page,
        user_id=user_id,
        limit=filters.limit + 1,
        **params,
    )
    has_more = len(rows) > filters.limit
    rows = rows[: filters.limit]
    return ModelCallPage(
        timezone=timezone,
        generated_at=time.time_ns() // 1_000_000,
        next_before_id=rows[-1].pk if has_more else None,
        items=[
            ModelCallReceipt(
                **{
                    key: getattr(row, key)
                    for key in ModelCallReceipt.model_fields
                    if key not in {"usage", "metadata"}
                },
                usage=ModelCallTokens.model_validate(json.loads(row.usage_json)),
                metadata=ModelCallMetadata.model_validate(json.loads(row.metadata_json)),
            )
            for row in rows
        ],
    )
