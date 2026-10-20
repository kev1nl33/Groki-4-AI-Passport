"""Tests for Stage H misuse guards and the notify device messages."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from stackchan_mcp.esp32_client import ESP32Connection, ESP32Manager
from stackchan_mcp.tts import EngineRegistry, TTSEngine, synthesize_and_send


class _StaticEngine(TTSEngine):
    def __init__(self, name: str = "voicevox") -> None:
        self.name = name

    async def synthesize(self, text: str, **opts) -> bytes:
        return b"\x00\x01" * 960


class _RecordingSend:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, payload: str) -> None:
        self.sent.append(payload)


def _pcm_gateway() -> SimpleNamespace:
    proxy = SimpleNamespace(device_pcm_downlink=True)
    connection = SimpleNamespace(
        cloud_proxy=proxy,
        protocol_version=1,
        connected=True,
        initialized=False,
    )
    manager = SimpleNamespace(
        device_connected=True,
        connection=connection,
        tts_lock=asyncio.Lock(),
    )
    return SimpleNamespace(esp32=manager)


def _opus_gateway() -> SimpleNamespace:
    proxy = SimpleNamespace(device_pcm_downlink=False)
    connection = SimpleNamespace(
        cloud_proxy=proxy,
        protocol_version=1,
        connected=True,
        initialized=False,
    )
    manager = SimpleNamespace(
        device_connected=True,
        connection=connection,
        tts_lock=asyncio.Lock(),
    )
    return SimpleNamespace(esp32=manager)


@pytest.mark.asyncio
async def test_say_rejected_on_pcm_downlink_connection():
    registry = EngineRegistry()
    registry.register(_StaticEngine())
    with pytest.raises(RuntimeError, match="PCM"):
        await synthesize_and_send(
            {"text": "hello"}, gateway=_pcm_gateway(), registry=registry
        )


@pytest.mark.asyncio
async def test_say_still_runs_for_opus_connections(monkeypatch):
    """The guard must not regress the original Opus say() pipeline."""
    from stackchan_mcp.tts import orchestrator

    monkeypatch.setattr(
        orchestrator, "encode_opus_frames", lambda frame: [b"OPUS" + frame[:3]]
    )
    sent_frames: list[bytes] = []
    tts_states: list[str] = []
    gateway = _opus_gateway()

    async def send_audio_frame(frame: bytes) -> None:
        sent_frames.append(frame)

    async def send_tts_state(state: str) -> None:
        tts_states.append(state)

    gateway.esp32.send_audio_frame = send_audio_frame
    gateway.esp32.send_tts_state = send_tts_state

    registry = EngineRegistry()
    registry.register(_StaticEngine())
    result = await synthesize_and_send(
        {"text": "hello"}, gateway=gateway, registry=registry
    )
    assert result["frame_count"] > 0
    assert tts_states[0] == "start"
    assert tts_states[-1] == "stop"


@pytest.mark.asyncio
async def test_manager_speak_rejected_on_pcm_downlink_connection():
    manager = ESP32Manager.__new__(ESP32Manager)
    manager._connection = _pcm_gateway().esp32.connection
    result, error = await manager.speak("hello")
    assert result is None
    assert "PCM" in error["message"] or "notify" in error["message"]


@pytest.mark.asyncio
async def test_connection_send_notify_state_payload_and_disconnect_guard():
    connection = ESP32Connection.__new__(ESP32Connection)
    connection._connected = True
    connection.session_id = "sess-1"
    recorder = _RecordingSend()
    connection._ws_send = recorder

    await connection.send_notify_state("start", flash=True)
    await connection.send_notify_state("stop")
    assert json.loads(recorder.sent[0]) == {
        "type": "notify",
        "state": "start",
        "flash": True,
    }
    assert json.loads(recorder.sent[1]) == {
        "type": "notify",
        "state": "stop",
        "flash": False,
    }

    connection._connected = False
    with pytest.raises(ConnectionError):
        await connection.send_notify_state("start")
