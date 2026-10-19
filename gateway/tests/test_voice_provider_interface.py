# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Provider-independent device lifecycle; no model or hardware connections."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy


@pytest.mark.asyncio
async def test_login_voice_uses_common_wake_and_end_callbacks_without_gemini_key(
    monkeypatch,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    gate = SimpleNamespace(
        available=False,
        state=SimpleNamespace(value="DORMANT"),
        is_listening=False,
        is_tts_active=None,
    )
    fake = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        wait_connected=AsyncMock(return_value=True),
        running=True,
        _connected_event=asyncio.Event(),
    )
    fake._connected_event.set()
    observed = {}

    def factory(**kwargs):
        observed.update(kwargs)
        return fake

    proxy = GbotVoiceProxy(
        esp32_ref=lambda: object(),
        bridge_factory=factory,
        codec_factory=lambda: object(),
        edge_tts_provider_factory=lambda **kwargs: object(),
        wake_gate_factory=lambda: gate,
    )
    try:
        assert await proxy.start(SimpleNamespace(device_id="fixture"), {}, AsyncMock())
        assert proxy._wake_gate is gate
        assert callable(observed.get("on_end_conversation"))
        assert observed.get("api_key") is None
        assert proxy.server_hello["provider"] == "gbot_voice"
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_grok_shared_speak_waits_for_first_delayed_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_SPEAK_TIMEOUT_SECONDS", ".3")
    proxy = GbotVoiceProxy()
    ready = asyncio.Event()
    ready.set()

    async def inject(text):
        async def later():
            await asyncio.sleep(0.01)
            proxy._speak_text_frames = 1

        asyncio.create_task(later())

    proxy._bridge = SimpleNamespace(
        running=True, _connected_event=ready, inject_user_text=inject
    )
    proxy._codec = SimpleNamespace(_encode_buffer=b"")
    proxy._send_to_device = AsyncMock()
    proxy._ready_event.set()
    result = await proxy.speak_text(
        "fixture", session_id="fixture", prompt_audio_frames=[]
    )
    assert result["ok"] and result["frames_sent"] == 1
