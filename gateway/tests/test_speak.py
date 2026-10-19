"""Tests for StackChan speak/TTS streaming."""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolRequest, ListToolsRequest

from stackchan_mcp import audio_stream
from stackchan_mcp.audio_stream import SynthesizedSpeech
from stackchan_mcp.stdio_server import create_server


@pytest.mark.asyncio
async def test_speak_text_streams_tts_protocol(monkeypatch):
    async def fake_synthesize(text: str, *, emotion: str | None = None):
        assert text == "验证通过"
        assert emotion == "happy"
        return SynthesizedSpeech(opus_frames=[b"opus-1", b"opus-2"], provider="fake")

    sent: list[str | bytes] = []

    async def send_to_device(message: str | bytes) -> None:
        sent.append(message)

    monkeypatch.setattr(audio_stream, "synthesize_speech", fake_synthesize)

    result = await audio_stream.speak_text(send_to_device, "验证通过", emotion="happy")

    assert result.ok is True
    assert result.frames_sent == 2
    assert result.provider == "fake"
    assert json.loads(sent[0]) == {"type": "llm", "emotion": "happy"}
    assert json.loads(sent[1]) == {"type": "tts", "state": "start"}
    assert json.loads(sent[2]) == {"type": "tts", "state": "sentence_start", "text": "验证通过"}
    assert sent[3:5] == [b"opus-1", b"opus-2"]
    assert json.loads(sent[5]) == {"type": "tts", "state": "stop"}


@pytest.mark.asyncio
async def test_speak_text_defaults_to_local_tts_even_when_cloud_is_connected(monkeypatch):
    async def fake_synthesize(text: str, *, emotion: str | None = None):
        assert text == "你好，我是机器人"
        return SynthesizedSpeech(opus_frames=[b"local-opus"], provider="local_fake")

    class FakeCloudProxy:
        connected = True

        async def speak_text(self, *args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("cloud speak should not be used by default")

    sent: list[str | bytes] = []

    async def send_to_device(message: str | bytes) -> None:
        sent.append(message)

    monkeypatch.setattr(audio_stream, "synthesize_speech", fake_synthesize)

    result = await audio_stream.speak_text(
        send_to_device,
        "你好，我是机器人",
        emotion="happy",
        cloud_proxy=FakeCloudProxy(),
        session_id="local-session",
    )

    assert result.ok is True
    assert result.provider == "local_fake"
    assert b"local-opus" in sent


@pytest.mark.asyncio
async def test_speak_text_uses_xiaozhi_cloud_when_required_and_connected(monkeypatch):
    async def fake_prompt(text: str):
        assert text == "你好，我是机器人"
        return SynthesizedSpeech(opus_frames=[b"prompt-opus"], provider="macos_say")

    class FakeCloudProxy:
        connected = True

        def __init__(self):
            self.calls = []

        async def speak_text(self, text, *, session_id, prompt_audio_frames, emotion=None):
            self.calls.append((text, session_id, prompt_audio_frames, emotion))
            return {
                "ok": True,
                "text": text,
                "emotion": emotion,
                "frames_sent": 3,
                "provider": "xiaozhi_cloud",
                "error": None,
            }

    sent: list[str | bytes] = []

    async def send_to_device(message: str | bytes) -> None:
        sent.append(message)

    cloud = FakeCloudProxy()
    monkeypatch.setattr(audio_stream, "synthesize_cloud_prompt", fake_prompt)

    result = await audio_stream.speak_text(
        send_to_device,
        "你好，我是机器人",
        emotion="happy",
        cloud_proxy=cloud,
        session_id="local-session",
        require_cloud=True,
    )

    assert result.ok is True
    assert result.provider == "xiaozhi_cloud"
    assert result.frames_sent == 3
    assert cloud.calls == [("你好，我是机器人", "local-session", [b"prompt-opus"], "happy")]
    assert sent == []


@pytest.mark.asyncio
async def test_speak_text_rejects_empty_text():
    sent: list[str | bytes] = []

    async def send_to_device(message: str | bytes) -> None:
        sent.append(message)

    result = await audio_stream.speak_text(send_to_device, "   ")

    assert result.ok is False
    assert "empty" in (result.error or "")
    assert sent == []


def test_opus_packets_are_extracted_from_ogg_pages():
    ogg = _ogg_page(b"OpusHead") + _ogg_page(b"OpusTags") + _ogg_page(b"audio-frame-1") + _ogg_page(b"audio-frame-2")

    assert audio_stream._opus_packets_from_ogg(ogg) == [b"audio-frame-1", b"audio-frame-2"]


@pytest.mark.asyncio
async def test_list_tools_includes_speak():
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))

    tool = next((t for t in result.root.tools if t.name == "speak"), None)
    assert tool is not None
    assert set(tool.inputSchema["required"]) == {"text"}
    assert "emotion" in tool.inputSchema["properties"]


@pytest.mark.asyncio
async def test_stdio_speak_relays_to_gateway(monkeypatch):
    calls = []

    class FakeESP32:
        device_connected = True

        async def speak(self, text, emotion=None):
            calls.append((text, emotion))
            return {"ok": True, "text": text, "emotion": emotion, "frames_sent": 1, "provider": "fake"}, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "speak", "arguments": {"text": "你好", "emotion": "happy"}},
        )
    )

    assert calls == [("你好", "happy")]
    assert json.loads(result.root.content[0].text)["ok"] is True


@pytest.mark.asyncio
async def test_stdio_speak_returns_engine_error(monkeypatch):
    class FakeESP32:
        device_connected = True

        async def speak(self, text, emotion=None):
            return {"ok": False, "text": text, "provider": "none"}, {"message": "No speech engine"}

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params={"name": "speak", "arguments": {"text": "你好"}})
    )

    parsed = json.loads(result.root.content[0].text)
    assert parsed["error"] == "No speech engine"
    assert parsed["result"]["ok"] is False


def _ogg_page(packet: bytes) -> bytes:
    if len(packet) >= 255:
        raise ValueError("test packet too large")
    header = bytearray()
    header.extend(b"OggS")
    header.extend(b"\x00")  # version
    header.extend(b"\x00")  # header type
    header.extend(b"\x00" * 8)  # granule position
    header.extend(b"\x01\x00\x00\x00")  # serial
    header.extend(b"\x00\x00\x00\x00")  # sequence
    header.extend(b"\x00\x00\x00\x00")  # checksum ignored by parser
    header.extend(b"\x01")
    header.extend(bytes([len(packet)]))
    return bytes(header) + packet


@pytest.mark.asyncio
async def test_cloud_prompt_puts_chinese_text_into_the_english_template(monkeypatch):
    prompts: list[str] = []

    async def fake_say(text: str, *, sample_rate: int = 24000) -> SynthesizedSpeech:
        prompts.append(text)
        assert sample_rate == 16000
        return SynthesizedSpeech(opus_frames=[b"opus"], provider="fake")

    monkeypatch.delenv("XIAOZHI_CLOUD_TTS_PROMPT_TEMPLATE", raising=False)
    monkeypatch.setattr(audio_stream, "_synthesize_macos_say", fake_say)

    await audio_stream.synthesize_cloud_prompt("今天天气很好")

    assert prompts == ["Repeat after me: 今天天气很好. Say only: 今天天气很好."]


@pytest.mark.asyncio
async def test_cloud_prompt_template_can_be_overridden(monkeypatch):
    prompts: list[str] = []

    async def fake_say(text: str, *, sample_rate: int = 24000) -> SynthesizedSpeech:
        prompts.append(text)
        return SynthesizedSpeech(opus_frames=[b"opus"], provider="fake")

    monkeypatch.setenv("XIAOZHI_CLOUD_TTS_PROMPT_TEMPLATE", "跟我说：{text}。只说这句：{text}")
    monkeypatch.setattr(audio_stream, "_synthesize_macos_say", fake_say)

    await audio_stream.synthesize_cloud_prompt("你好")

    assert prompts == ["跟我说：你好。只说这句：你好"]
