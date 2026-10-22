"""HTTP glue for the live STT probe: provider errors surface as ok:false."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import octop.infra.voice.adapters as voice_adapters

TENCENT_EXTRA = {"secret_id": "sid", "secret_key": "sk", "region": "ap-guangzhou"}


def _raising(exc: Exception) -> Any:
    async def fake(row: Any, audio: bytes, *, mime: str, language: str) -> Any:
        raise exc

    return fake


async def test_probe_stt_reports_provider_error_over_http(
    env: tuple[httpx.AsyncClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _srv, auth = env
    monkeypatch.setattr(
        voice_adapters,
        "transcribe_tencent",
        _raising(RuntimeError("AuthFailure.SecretIdNotFound: The SecretId is not found.")),
    )

    r = await client.post(
        "/api/admin/voice/providers/test-configuration",
        headers={**auth, "Accept-Language": "zh"},
        json={
            "name": "tencent-asr",
            "kind": "tencent",
            "capability": "both",
            "api_key": "sid:sk",
            "extra_json": json.dumps(TENCENT_EXTRA),
            "mode": "stt",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "SecretId 不存在，请检查密钥是否填写正确。"


async def test_probe_stt_success_over_http(
    env: tuple[httpx.AsyncClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.infra.voice.adapters import STTResult

    async def fake(row: Any, audio: bytes, *, mime: str, language: str) -> STTResult:
        assert mime == "audio/wav"
        return STTResult(text="")

    client, _srv, auth = env
    monkeypatch.setattr(voice_adapters, "transcribe_tencent", fake)

    r = await client.post(
        "/api/admin/voice/providers/test-configuration",
        headers=auth,
        json={
            "name": "tencent-asr",
            "kind": "tencent",
            "capability": "both",
            "api_key": "sid:sk",
            "extra_json": json.dumps(TENCENT_EXTRA),
            "mode": "stt",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "mode": "tencent"}


async def test_box_voice_providers_work_without_keys_over_http(
    env: tuple[httpx.AsyncClient, Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.infra.voice.adapters import STTResult

    client, _srv, auth = env

    async def recognize(row: Any, audio: bytes, *, mime: str, language: str) -> STTResult:
        assert row.kind == "box" and row.api_key is None
        assert row.capability == "stt"
        assert audio == b"recording"
        return STTResult(text="盒子语音")

    async def speak(row: Any, text: str, *, voice_id: str | None, speed: float):
        assert row.kind == "box" and row.api_key is None
        assert row.capability == "tts"
        assert text == "盒子回复"
        yield b"box-mp3"

    monkeypatch.setattr(voice_adapters, "transcribe_box", recognize)
    monkeypatch.setattr(voice_adapters, "synthesize_box", speak)
    for capability in ("stt", "tts"):
        response = await client.post(
            "/api/admin/voice/providers",
            headers=auth,
            json={"name": f"box-{capability}", "kind": "box", "capability": capability},
        )
        assert response.status_code == 201
        assert response.json()["api_key"] is None
    response = await client.put(
        "/api/voice/active", headers=auth, json={"stt": "box-stt", "tts": "box-tts"}
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/voice/stt",
        headers=auth,
        files={"audio": ("recording.wav", b"recording", "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "盒子语音"
    response = await client.post("/api/voice/tts", headers=auth, json={"text": "盒子回复"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"box-mp3"


async def test_box_voice_configuration_validation_over_http(
    env: tuple[httpx.AsyncClient, Any, dict[str, str]],
) -> None:
    client, _srv, auth = env
    for fields in (
        {"capability": "both"},
        {"capability": "stt", "base_url": "file:///tmp/audio"},
    ):
        response = await client.post(
            "/api/admin/voice/providers",
            headers=auth,
            json={"name": "invalid-box", "kind": "box", **fields},
        )
        assert response.status_code == 400
    response = await client.post(
        "/api/admin/voice/providers",
        headers=auth,
        json={"name": "box-stt", "kind": "box", "capability": "stt"},
    )
    provider_id = response.json()["id"]
    response = await client.patch(
        f"/api/admin/voice/providers/{provider_id}",
        headers=auth,
        json={"base_url": "http://user:password@box.local/v1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"field": "base_url"}
