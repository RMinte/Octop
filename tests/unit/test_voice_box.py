"""Box voice protocol, mixed provider routing, and endpoint validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.db.repos.voice_providers import VoiceProviderRepo, VoiceProviderRow
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.box_models import validate_box_base_url
from octop.infra.utils.ssrf_guard import UnsafeOutboundUrl
from octop.infra.voice import adapters
from octop.infra.voice.manager import VoiceManager, validate_voice_configuration
from octop.infra.voice.presets import DEFAULT_BOX_VOICE_INSTRUCTIONS, load_voice_presets


def _row(
    capability: str,
    *,
    base_url: str | None = None,
    extra: dict[str, Any] | None = None,
) -> VoiceProviderRow:
    return VoiceProviderRow(
        id=1,
        name=f"box-{capability}",
        kind="box",
        capability=capability,
        base_url=base_url,
        api_key=None,
        extra_json=json.dumps(extra) if extra else None,
        note=None,
        enabled=1,
        created_at=0,
        updated_at=0,
    )


@pytest.fixture
def box_http(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    original = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def factory(**kwargs: Any) -> httpx.AsyncClient:
            return original(**kwargs, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(adapters.httpx, "AsyncClient", factory)

    return install


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:58100/v1/",
        "http://192.168.1.2:58100/v1/",
        "http://171.88.165.251:58100/v1/",
        "http://[::1]:58100/v1/",
        "https://box.example/v1/",
    ],
)
def test_box_urls_allow_local_lan_and_development_hosts(base_url: str) -> None:
    assert validate_box_base_url(base_url) == base_url.rstrip("/")


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/v1",
        "ftp://box.example/v1",
        "//box.example/v1",
        "http:///v1",
        "http://user:password@box.example/v1",
        "http://box.example:99999/v1",
        "http://box.example:/v1",
        "http://box.example/v1?token=secret",
        "http://box.example/v1#fragment",
        "http://box.example/v1?",
        "http://box.example/\nv1",
        "http://box.example\\other/v1",
    ],
)
def test_box_urls_reject_ambiguous_or_non_http_addresses(base_url: str) -> None:
    with pytest.raises(ValueError):
        validate_box_base_url(base_url)
    with pytest.raises(OctopError) as exc:
        validate_voice_configuration(kind="box", capability="stt", base_url=base_url)
    assert exc.value.code == ErrorCode.SLASH_BAD_ARGS
    assert exc.value.details == {"field": "base_url"}


def test_box_presets_have_independent_local_endpoints_and_no_key() -> None:
    presets = {preset["id"]: preset for preset in load_voice_presets()}
    assert presets["box-stt"]["base_url"] == "http://127.0.0.1:58100/v1"
    assert presets["box-stt"]["extra"]["model"] == "qwen3-asr"
    assert presets["box-tts"]["base_url"] == "http://127.0.0.1:58101/v1"
    assert presets["box-tts"]["extra"]["model"] == "qwen3-tts"
    assert presets["box-stt"]["requires_key"] is False
    assert presets["box-tts"]["requires_key"] is False
    with pytest.raises(OctopError) as exc:
        validate_voice_configuration(kind="box", capability="both", base_url=None)
    assert exc.value.code == ErrorCode.VOICE_CAPABILITY_MISMATCH


async def test_box_asr_uses_multipart_without_authentication(box_http: Callable[..., None]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:58100/v1/audio/transcriptions"
        assert "authorization" not in request.headers
        assert "multipart/form-data" in request.headers["content-type"]
        assert b"qwen3-asr" in request.content
        assert b"audio.webm" in request.content
        assert b"audio/webm" in request.content
        assert b"recorded-audio" in request.content
        return httpx.Response(200, json={"text": "  盒子识别结果  "})

    box_http(handler)
    result = await adapters.transcribe_box(
        _row("stt"), b"recorded-audio", mime="audio/webm", language="zh-CN"
    )
    assert result.text == "盒子识别结果"


async def test_box_tts_uses_voice_design_and_configured_endpoint(
    box_http: Callable[..., None],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://box.local:58101/v1/audio/speech"
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {
            "model": "qwen3-tts",
            "input": "你好",
            "task_type": "VoiceDesign",
            "instructions": "自然清晰的女声",
            "response_format": "mp3",
        }
        return httpx.Response(200, content=b"mp3-audio", headers={"content-type": "audio/mpeg"})

    box_http(handler)
    output = b"".join(
        [
            chunk
            async for chunk in adapters.synthesize_box(
                _row(
                    "tts",
                    base_url="http://box.local:58101/v1/",
                    extra={"instructions": "自然清晰的女声"},
                ),
                "你好",
                voice_id="do-not-send",
                speed=2.0,
            )
        ]
    )
    assert output == b"mp3-audio"


async def test_box_tts_empty_instructions_use_default_and_do_not_follow_redirects(
    box_http: Callable[..., None],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert json.loads(request.content)["instructions"] == DEFAULT_BOX_VOICE_INSTRUCTIONS
        return httpx.Response(307, headers={"location": "http://other.local/audio/speech"})

    box_http(handler)
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in adapters.synthesize_box(
            _row("tts", extra={"instructions": " "}), "hello", voice_id=None, speed=1.0
        ):
            pass
    assert len(requests) == 1


async def test_box_probes_execute_without_key_and_remote_openai_still_requires_key(
    box_http: Callable[..., None],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        if request.url.path.endswith("transcriptions"):
            return httpx.Response(200, json={"text": ""})
        return httpx.Response(200, content=b"audio")

    box_http(handler)
    assert await adapters.test_stt(_row("stt"), "box") == {"ok": True, "mode": "box"}
    assert await adapters.test_tts(_row("tts"), "box") == {"ok": True, "bytes": 5}
    assert await adapters.test_stt(_row("stt"), "openai") == {
        "ok": False,
        "error": "API credentials missing",
    }
    assert await adapters.test_tts(_row("tts"), "openai") == {
        "ok": False,
        "error": "API credentials missing",
    }
    assert await adapters.test_stt(_row("tts"), "box") == {
        "ok": False,
        "error": "Voice capability does not match.",
    }
    assert await adapters.test_tts(_row("stt"), "box", locale="zh") == {
        "ok": False,
        "error": OctopError.localized(ErrorCode.VOICE_CAPABILITY_MISMATCH, "zh").message,
    }


async def test_openai_voice_retains_public_https_restriction() -> None:
    with pytest.raises(UnsafeOutboundUrl):
        await adapters.transcribe_openai(
            replace(
                _row("stt"), kind="openai", api_key="remote-key", base_url="http://127.0.0.1/v1"
            ),
            b"audio",
            mime="audio/wav",
            language="zh-CN",
        )


async def test_voice_manager_mixes_box_asr_and_remote_tts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    repo = VoiceProviderRepo(db)
    repo.create(name="box-stt", kind="box", capability="stt")
    repo.create(name="remote-tts", kind="openai", capability="tts", api_key="remote-key")
    manager = VoiceManager(settings_repo=SettingsRepo(db), voice_provider_repo=repo)
    manager.set_active(stt="box-stt", tts="remote-tts")

    async def recognize(row: VoiceProviderRow, audio: bytes, **kwargs: Any) -> adapters.STTResult:
        assert row.kind == "box" and row.api_key is None
        return adapters.STTResult(text="spoken text")

    async def synthesize(row: VoiceProviderRow, text: str, **kwargs: Any):
        assert row.name == "remote-tts" and row.api_key == "remote-key"
        yield b"remote-audio"

    monkeypatch.setattr(adapters, "transcribe_box", recognize)
    monkeypatch.setattr(adapters, "synthesize_openai", synthesize)
    assert (await manager.transcribe(b"audio", mime="audio/wav")).text == "spoken text"
    assert b"".join([chunk async for chunk in manager.synthesize("hello")]) == b"remote-audio"
    assert manager.media_type(None) == "audio/mpeg"
    with pytest.raises(OctopError) as exc:
        async for _ in manager.synthesize("hello", provider_name="box-stt"):
            pass
    assert exc.value.code == ErrorCode.VOICE_CAPABILITY_MISMATCH
    db.close()
