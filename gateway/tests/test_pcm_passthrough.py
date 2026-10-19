# SPDX-License-Identifier: MIT
"""Stage C: PCM negotiation and direct passthrough on the gateway."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from stackchan_mcp.cloud_proxy import CloudProxy
from stackchan_mcp.esp32_client import ESP32Manager, _safe_diagnostic
from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy
from stackchan_mcp.protocol import (
    PCM_DEVICE_FRAME_BYTES,
    parse_device_audio_params,
)


# --- negotiation -------------------------------------------------------------


PCM_HELLO = {
    "type": "hello",
    "version": 1,
    "transport": "websocket",
    "features": {"mcp": False},
    "audio_params": {
        "format": "pcm",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration": 20,
    },
}

OPUS_HELLO = {
    "type": "hello",
    "version": 1,
    "transport": "websocket",
    "features": {"mcp": False},
    "audio_params": {
        "format": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration": 60,
    },
}


def test_pcm_tuple_enables_direct_passthrough():
    audio = parse_device_audio_params(PCM_HELLO)
    assert audio.format == "pcm"
    assert audio.direct_pcm is True


@pytest.mark.parametrize(
    "params",
    [
        {"format": "pcm", "sample_rate": 8000, "channels": 1, "frame_duration": 20},
        {"format": "pcm", "sample_rate": 16000, "channels": 2, "frame_duration": 20},
        {"format": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        {"format": "pcm"},
    ],
)
def test_pcm_with_other_parameters_stays_on_the_opus_path(params):
    audio = parse_device_audio_params({"audio_params": params})
    assert audio.direct_pcm is False


def test_opus_and_missing_params_keep_the_legacy_path():
    assert parse_device_audio_params(OPUS_HELLO).direct_pcm is False
    assert parse_device_audio_params({"type": "hello"}).direct_pcm is False
    assert parse_device_audio_params({"audio_params": "bogus"}).direct_pcm is False


def test_unknown_format_falls_back_to_opus():
    audio = parse_device_audio_params(
        {"audio_params": {"format": "aac", "sample_rate": 16000}}
    )
    assert audio.format == "opus"
    assert audio.direct_pcm is False


# --- websocket handshake and binary routing ---------------------------------


class RecordingProxy:
    def __init__(self) -> None:
        self.frames: list[tuple[int, bool]] = []
        self.json_from_device: list[dict] = []
        self.start_calls = 0
        self.pcm_flags: list[bool] = []
        self.connected = True

    def set_device_pcm(self, enabled: bool) -> None:
        self.pcm_flags.append(bool(enabled))

    async def start(self, info, device_hello, send_to_device):
        self.start_calls += 1
        return True

    async def stop(self):
        pass

    async def try_handle_device_mcp(self, message):
        return False

    async def send_device_json(self, message):
        self.json_from_device.append(message)
        return True

    async def send_device_binary(self, data: bytes, *, pcm: bool = False):
        self.frames.append((len(data), pcm))
        return True


async def _connect_and_hello(manager, hello: dict) -> dict:
    port = manager._test_port
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await ws.send(json.dumps(hello))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
    return ws, resp


@pytest.mark.asyncio
async def test_pcm_hello_gets_pcm_server_params_and_binary_passthrough():
    proxy = RecordingProxy()
    manager = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await manager.start("127.0.0.1", 0)
    port = manager._server.sockets[0].getsockname()[1]
    manager._test_port = port
    try:
        ws, resp = await _connect_and_hello(manager, PCM_HELLO)
        assert resp["type"] == "hello"
        assert resp["audio_params"] == {
            "format": "pcm",
            "sample_rate": 24000,
            "channels": 1,
            "frame_duration": 20,
        }
        assert manager.connection is not None
        assert manager.connection.pcm_direct is True

        await ws.send(b"\x00\x01" * (PCM_DEVICE_FRAME_BYTES // 2))
        await asyncio.sleep(0.1)
        assert proxy.frames == [(PCM_DEVICE_FRAME_BYTES, True)]
        await ws.close()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_opus_hello_keeps_original_server_params_and_opus_routing():
    proxy = RecordingProxy()
    manager = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await manager.start("127.0.0.1", 0)
    manager._test_port = manager._server.sockets[0].getsockname()[1]
    try:
        ws, resp = await _connect_and_hello(manager, OPUS_HELLO)
        assert resp["audio_params"] == {
            "format": "opus",
            "sample_rate": 24000,
            "channels": 1,
            "frame_duration": 60,
        }
        await ws.send(b"opus-frame")
        await asyncio.sleep(0.1)
        assert proxy.frames == [(10, False)]
        assert proxy.pcm_flags[-1] is False
        await ws.close()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unknown_format_keeps_opus_server_params():
    proxy = RecordingProxy()
    manager = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await manager.start("127.0.0.1", 0)
    manager._test_port = manager._server.sockets[0].getsockname()[1]
    try:
        hello = dict(PCM_HELLO)
        hello["audio_params"] = {"format": "aac", "sample_rate": 16000}
        ws, resp = await _connect_and_hello(manager, hello)
        assert resp["audio_params"]["format"] == "opus"
        assert manager.connection is not None
        assert manager.connection.pcm_direct is False
        await ws.close()
    finally:
        await manager.stop()


# --- proxy PCM branch --------------------------------------------------------


class FakeBridge:
    def __init__(self) -> None:
        self.audio_sent: list[bytes] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_sent.append(pcm)


class FakeCodec:
    def __init__(self) -> None:
        self.decoded: list[bytes] = []

    def decode_device_frame(self, opus: bytes) -> bytes:
        self.decoded.append(opus)
        return b"DECODED-PCM"


def _proxy_for_pcm() -> tuple[GeminiVoiceProxy, FakeBridge, FakeCodec]:
    proxy = GeminiVoiceProxy()
    bridge = FakeBridge()
    codec = FakeCodec()
    proxy._bridge = bridge
    proxy._codec = codec
    proxy._send_to_device = lambda _data: None
    proxy.wake_gate_factory = lambda: None
    proxy._ready_event.set()
    return proxy, bridge, codec


@pytest.mark.asyncio
async def test_direct_pcm_is_forwarded_without_decoding():
    proxy, bridge, codec = _proxy_for_pcm()
    frame = b"\x10\x00" * (PCM_DEVICE_FRAME_BYTES // 2)
    assert await proxy.send_device_binary(frame, pcm=True) is True
    assert bridge.audio_sent == [frame]
    assert codec.decoded == []
    assert proxy._pcm_dropped_frames == 0


@pytest.mark.parametrize("size", [0, 2, 639, 641, 1280])
@pytest.mark.asyncio
async def test_invalid_pcm_frames_are_dropped_and_counted(size):
    proxy, bridge, codec = _proxy_for_pcm()
    accepted = await proxy.send_device_binary(b"\x00" * size, pcm=True)
    if size == 0:
        assert accepted is True  # empty frame is a no-op, not a protocol error
        assert proxy._pcm_dropped_frames == 0
    else:
        assert accepted is False
        assert proxy._pcm_dropped_frames == 1
    assert bridge.audio_sent == []
    assert codec.decoded == []


@pytest.mark.asyncio
async def test_opus_branch_still_decodes():
    proxy, bridge, codec = _proxy_for_pcm()
    assert await proxy.send_device_binary(b"opus-frame") is True
    assert codec.decoded == [b"opus-frame"]
    assert bridge.audio_sent == [b"DECODED-PCM"]


# --- review regressions ------------------------------------------------------


@pytest.mark.asyncio
async def test_second_hello_is_ignored_and_does_not_restart_the_proxy():
    proxy = RecordingProxy()
    manager = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await manager.start("127.0.0.1", 0)
    manager._test_port = manager._server.sockets[0].getsockname()[1]
    try:
        ws, first = await _connect_and_hello(manager, PCM_HELLO)
        assert first["audio_params"]["format"] == "pcm"
        # A second hello on the same socket must not rewrite the format or
        # start another cloud proxy.
        await ws.send(json.dumps(OPUS_HELLO))
        await asyncio.sleep(0.1)
        assert manager.connection is not None
        assert manager.connection.hello_received is True
        assert manager.connection.pcm_direct is True
        assert proxy.start_calls == 1
        await ws.close()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_pcm_server_hello_precedes_a_slow_cloud_start():
    class BlockingProxy(RecordingProxy):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def start(self, info, device_hello, send_to_device):
            self.start_calls += 1
            self.entered.set()
            await self.release.wait()
            return True

    proxy = BlockingProxy()
    manager = ESP32Manager(cloud_proxy_factory=lambda: proxy)
    await manager.start("127.0.0.1", 0)
    manager._test_port = manager._server.sockets[0].getsockname()[1]
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{manager._test_port}")
        await ws.send(json.dumps(PCM_HELLO))
        await asyncio.wait_for(proxy.entered.wait(), timeout=2.0)
        # The proxy is still blocked in its cold start, yet hello must arrive
        # inside the firmware's 5 s handshake window.
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        assert resp["audio_params"]["format"] == "pcm"
        proxy.release.set()
        ready=json.loads(await asyncio.wait_for(ws.recv(),timeout=2.0))
        assert ready['type']=='voice_ready'
        await ws.close()
    finally:
        proxy.release.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_pcm_backend_failure_is_not_reported_ready():
    class FailedProxy(RecordingProxy):
        async def start(self,*args):
            return False
    manager=ESP32Manager(cloud_proxy_factory=FailedProxy)
    await manager.start('127.0.0.1',0)
    port=manager._server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f'ws://127.0.0.1:{port}') as ws:
            await ws.send(json.dumps(PCM_HELLO))
            assert json.loads(await ws.recv())['type']=='hello'
            assert json.loads(await ws.recv())['type']=='voice_error'
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_legacy_cloud_proxy_rejects_pcm_without_decoding():
    proxy = CloudProxy()
    sent: list[bytes] = []

    async def fake_send(data):
        sent.append(data)

    async def connected():
        return True

    proxy._send_cloud = fake_send
    proxy.ensure_connected = connected
    frame = b"\x00\x01" * (PCM_DEVICE_FRAME_BYTES // 2)
    assert await proxy.send_device_binary(frame, pcm=True) is False
    assert sent == []
    # The old Opus path is unchanged.
    assert await proxy.send_device_binary(b"opus-frame") is True
    assert sent == [b"opus-frame"]


class GateSpy:
    def __init__(self) -> None:
        self.processed: list[bytes] = []
        self.closed = 0
        self.available = True
        self.is_listening = False

    def close(self) -> bool:
        self.closed += 1
        self.is_listening = False
        return True

    def process(self, pcm: bytes):
        self.processed.append(pcm)
        return type("Decision", (), {"forward_pcm": (pcm,), "woke": False,
                                     "closed": False, "bypassed": False})()


class ManualBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.running = True
        self.stream_ends = 0

    async def send_audio_stream_end(self) -> None:
        self.stream_ends += 1


@pytest.mark.asyncio
async def test_manual_hold_bypasses_the_wake_gate_until_stop():
    proxy = GeminiVoiceProxy()
    bridge = ManualBridge()
    gate = GateSpy()
    proxy._bridge = bridge
    proxy._codec = FakeCodec()
    proxy._wake_gate = gate
    proxy._wake_gate_absent = False
    proxy.wake_gate_factory = lambda: gate
    proxy._ready_event.set()
    proxy._send_to_device = lambda _data: None

    assert await proxy.send_device_json(
        {"type": "listen", "state": "start", "mode": "manual"}
    ) is True
    frame = b"\x10\x00" * (PCM_DEVICE_FRAME_BYTES // 2)
    assert await proxy.send_device_binary(frame, pcm=True) is True
    assert bridge.audio_sent == [frame]
    assert gate.processed == []  # the wake word never sees hold-to-talk audio
    assert gate.closed >= 1

    assert await proxy.send_device_json(
        {"type": "listen", "state": "stop", "mode": "manual"}
    ) is True
    assert proxy._manual_listening is False
    assert bridge.stream_ends == 1


def test_absent_wake_gate_is_not_rebuilt_per_frame():
    calls: list[int] = []

    def factory():
        calls.append(1)
        return None

    proxy = GeminiVoiceProxy(wake_gate_factory=factory)
    assert proxy._maybe_rebuild_wake_gate() is None
    for _ in range(100):
        assert proxy._maybe_rebuild_wake_gate() is None
    assert len(calls) == 1


# --- Stage D: PCM downlink ---------------------------------------------------


def _downlink_proxy() -> GeminiVoiceProxy:
    proxy = GeminiVoiceProxy()
    proxy.set_device_pcm(True)
    proxy.device_tts_start_delay_s = 0
    return proxy


def test_pcm_tts_frames_are_exactly_960_bytes_with_padded_tail():
    proxy = _downlink_proxy()
    assert proxy._tts_encode(b"\x01" * 960) == [b"\x01" * 960]
    # 1000 bytes: one full frame, then a 40-byte remainder.
    first = proxy._tts_encode(b"\x02" * 1000)
    assert len(first) == 1 and len(first[0]) == 960
    tail = proxy._tts_flush()
    assert len(tail) == 1 and len(tail[0]) == 960
    assert tail[0][:40] == b"\x02" * 40
    assert tail[0][40:] == b"\x00" * 920
    assert proxy._tts_flush() == []


def test_pcm_tts_frames_straddle_upstream_chunks():
    proxy = _downlink_proxy()
    assert proxy._tts_encode(b"\x03" * 480) == []
    frames = proxy._tts_encode(b"\x04" * 480)
    assert len(frames) == 1 and len(frames[0]) == 960
    assert frames[0][:480] == b"\x03" * 480
    assert frames[0][480:] == b"\x04" * 480


def test_opus_tts_encoding_is_unchanged():
    class EncodingCodec(FakeCodec):
        def __init__(self) -> None:
            super().__init__()
            self.encoded: list[bytes] = []

        def encode_pcm_24k(self, pcm: bytes) -> list[bytes]:
            self.encoded.append(pcm)
            return [b"opus-frame"]

    proxy = GeminiVoiceProxy()
    codec = EncodingCodec()
    proxy._codec = codec
    assert proxy._tts_encode(b"pcm") == [b"opus-frame"]
    assert codec.encoded == [b"pcm"]
    q = proxy._new_frame_queue()
    assert q.maxsize == 0  # Opus keeps the historical unbounded queue


@pytest.mark.asyncio
async def test_pcm_queue_backpressures_without_losing_audio():
    proxy = _downlink_proxy()
    queue = proxy._frame_queue
    for index in range(16):
        queue.put_nowait(bytes([index]))
    producer = asyncio.create_task(proxy._enqueue_frame(b"new"))
    try:
        await asyncio.sleep(0)
        assert not producer.done(), "full PCM queue must backpressure, not delete audio"
        assert queue.get_nowait() == b"\x00"
        queue.task_done()
        await asyncio.wait_for(producer, .2)
        assert [queue.get_nowait() for _ in range(16)] == [
            bytes([i]) for i in range(1, 16)
        ] + [b"new"]
        assert proxy._pcm_downlink_dropped_frames == 0
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_pcm_drain_is_paced_not_dumped():
    proxy = _downlink_proxy()
    proxy.device_burst_ms = 0  # pure metronome for a deterministic assertion
    proxy._frame_queue = proxy._new_frame_queue()
    proxy._drain_stop = asyncio.Event()
    sent: list[tuple[float, int]] = []
    loop = asyncio.get_running_loop()

    async def capture(frame):
        sent.append((loop.time(), len(frame)))

    proxy._send_to_device = capture
    for _ in range(5):
        proxy._frame_queue.put_nowait(b"\x00" * 960)
    proxy._drain_stop.set()
    started = loop.time()
    await proxy._drain()
    elapsed = loop.time() - started
    assert [size for _, size in sent] == [960] * 5
    # One immediate frame plus four 20 ms intervals.
    assert elapsed >= 0.07


@pytest.mark.asyncio
async def test_abort_clears_pcm_buffer_and_queue():
    class ResettingCodec(FakeCodec):
        def reset_encode_buffer(self) -> None:
            pass

    proxy = _downlink_proxy()
    proxy._codec = ResettingCodec()
    proxy._tts_active = True
    proxy._pcm_downlink_buffer.extend(b"\x05" * 100)
    proxy._frame_queue.put_nowait(b"\x00" * 960)
    stops: list[str] = []

    async def send(message):
        stops.append(message)

    proxy._send_to_device = send
    await proxy._abort_in_flight_tts()
    assert proxy._pcm_downlink_buffer == bytearray()
    assert proxy._frame_queue.empty()
    assert any('"state": "stop"' in message for message in stops)


def test_string_or_float_audio_params_never_enable_pcm():
    for bad in (
        {"format": "pcm", "sample_rate": "24000", "channels": 1,
         "frame_duration": 20},
        {"format": "pcm", "sample_rate": 24000.0, "channels": 1,
         "frame_duration": 20},
        {"format": "pcm", "sample_rate": 24000, "channels": True,
         "frame_duration": 20},
        {"format": "pcm", "sample_rate": 24000, "channels": 1,
         "frame_duration": "20"},
    ):
        assert parse_device_audio_params({"audio_params": bad}).direct_pcm is False


@pytest.mark.asyncio
async def test_pcm_connection_rejects_the_edge_tts_opus_path():
    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def speak_text(self, text):
            self.calls += 1
            return {"ok": True, "frames_sent": 1}

    proxy = _downlink_proxy()
    provider = Provider()
    proxy._edge_tts = provider
    await proxy._handle_gemini_text("hello")
    await asyncio.sleep(0)
    assert provider.calls == 0

    proxy._voice_tts_mode = lambda: "edge"
    result = await proxy.speak_text(
        "hello", session_id="s", prompt_audio_frames=[]
    )
    assert result["ok"] is False
    assert "PCM" in result["error"]

    # The Opus path still works for a non-PCM connection.
    opus_proxy = GeminiVoiceProxy()
    opus_provider = Provider()
    opus_proxy._edge_tts = opus_provider
    await opus_proxy._handle_gemini_text("hello")
    await asyncio.sleep(0)
    assert opus_provider.calls == 1


def test_raw_error_log_value_is_whitelisted():
    assert _safe_diagnostic("mcp.error") == "mcp.error"
    assert _safe_diagnostic("a" * 65) == "<omitted>"
    assert _safe_diagnostic('{"secret":"x"}') == "<omitted>"
    assert _safe_diagnostic("line\nbreak") == "<omitted>"


@pytest.mark.asyncio
async def test_pcm_large_burst_is_complete_and_stop_follows_last_frame():
    proxy = _downlink_proxy()
    proxy._codec = FakeCodec()
    proxy.device_prebuffer_ms = 0
    sent = []
    async def send(message):
        sent.append(message)
    proxy._send_to_device = send
    pcm = b''.join(bytes([i]) * 960 for i in range(64))
    await proxy._handle_gemini_audio(pcm)
    await asyncio.wait_for(proxy.end_tts(), 3)
    assert b''.join(x for x in sent if isinstance(x, bytes)) == pcm
    assert json.loads(sent[-1])['state'] == 'stop'
    assert proxy._pcm_downlink_dropped_frames == 0


@pytest.mark.asyncio
async def test_abort_cancels_full_queue_producer_and_new_turn_is_clean():
    proxy = _downlink_proxy()
    proxy._tts_active = True
    class ResetCodec(FakeCodec):
        def reset_encode_buffer(self):
            pass
    proxy._codec = ResetCodec()
    for _ in range(16):
        proxy._frame_queue.put_nowait(b'old')
    producer = asyncio.create_task(proxy._enqueue_frame(b'stale'))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not producer.done()
    await asyncio.wait_for(proxy._abort_in_flight_tts(), .3)
    await asyncio.gather(producer, return_exceptions=True)
    assert producer.cancelled()
    assert not proxy._pcm_put_tasks
    assert proxy._frame_queue.empty()
    await proxy._enqueue_frame(b'new')
    assert proxy._frame_queue.get_nowait() == b'new'

