import json

import pytest

from stackchan_mcp.edge_tts_provider import EdgeTTSProvider


class FakeCodec:
    def __init__(self) -> None:
        self.pcm: list[bytes] = []
        self.flushed = False
        self.reset_called = False

    def encode_pcm_24k(self, pcm: bytes) -> list[bytes]:
        self.pcm.append(pcm)
        return [b"OPUS"]

    def flush(self) -> list[bytes]:
        self.flushed = True
        return [b"TAIL"]

    def reset_encode_buffer(self) -> None:
        self.reset_called = True


class FakeEdgeProvider(EdgeTTSProvider):
    async def _iter_pcm_24k(self, text: str):
        assert text == "你好呀"
        yield b"\x01\x02" * 100


@pytest.mark.asyncio
async def test_edge_tts_provider_sends_tts_envelope_and_opus_frames():
    sent: list[str | bytes] = []
    states: list[str] = []

    async def send(msg):
        sent.append(msg)

    provider = FakeEdgeProvider(
        codec=FakeCodec(),
        send_to_device=send,
        tts_start_delay_s=0.0,
        on_device_state=states.append,
    )

    result = await provider.speak_text("你好呀")

    assert result["ok"] is True
    json_msgs = [json.loads(m) for m in sent if isinstance(m, str)]
    assert json_msgs[0] == {"type": "tts", "state": "start"}
    assert json_msgs[1]["state"] == "sentence_start"
    assert json_msgs[-1] == {"type": "tts", "state": "stop"}
    assert [m for m in sent if isinstance(m, bytes)] == [b"OPUS", b"TAIL"]
    assert states == ["speaking", "idle"]


@pytest.mark.asyncio
async def test_edge_tts_provider_empty_text_is_rejected_without_device_send():
    sent: list[str | bytes] = []

    async def send(msg):
        sent.append(msg)

    provider = FakeEdgeProvider(
        codec=FakeCodec(),
        send_to_device=send,
        tts_start_delay_s=0.0,
    )

    result = await provider.speak_text("  ")

    assert result["ok"] is False
    assert sent == []
