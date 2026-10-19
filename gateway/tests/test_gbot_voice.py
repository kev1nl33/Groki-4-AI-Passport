# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Hardware-free checks for signed-in Bot audio and cancellation boundaries."""

import asyncio
import base64
import struct
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_voice_bridge import (
    GbotVoiceBridge,
    Pcm16To24,
    validate_bridge_url,
    validate_voice_url,
)
from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy


def bridge():
    return GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://evil.test",
        "http://127.0.0.1@evil.test",
        "http://localhost",
        "http://127.0.0.1/?token=x",
    ],
)
def test_bridge_must_be_loopback(url):
    with pytest.raises(ValueError):
        validate_bridge_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ws://api.x.ai",
        "wss://evil.test",
        "wss://api.x.ai.evil.test",
        "wss://secret@api.x.ai",
        "wss://api.x.ai:8443",
    ],
)
def test_temporary_secret_only_goes_to_xai(url):
    with pytest.raises(ValueError):
        validate_voice_url(url)


def test_streaming_resampling_matches_single_chunk():
    pcm = struct.pack("<1600h", *range(1600))
    whole = Pcm16To24().convert(pcm)
    resampler = Pcm16To24()
    chunks = b"".join(
        resampler.convert(pcm[i : i + 320]) for i in range(0, len(pcm), 320)
    )
    assert whole == chunks
    assert abs(len(whole) - len(pcm) * 1.5) <= 4


@pytest.mark.asyncio
async def test_interrupt_discards_late_old_audio_and_accepts_next_turn():
    b = bridge()
    b._send = AsyncMock()
    await b._handle_event({"type": "response.created", "response": {"id": "old"}})
    delta = {
        "type": "response.audio.delta",
        "response_id": "old",
        "delta": base64.b64encode(b"\x00\x00" * 240).decode(),
    }
    await b._handle_event(delta)
    assert b._on_audio.await_count == 1
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    b._on_interrupt.assert_awaited_once()
    await b._handle_event(delta)
    await b._handle_event({"type": "response.done", "response": {"id": "old"}})
    assert b._on_audio.await_count == 1
    assert b._turn_task is None
    await b._handle_event({"type": "response.created", "response": {"id": "next"}})
    await b._handle_event(delta)  # old response arriving even after next starts
    assert b._on_audio.await_count == 1
    await b._handle_event({**delta, "response_id": "next"})
    assert b._on_audio.await_count == 2


@pytest.mark.asyncio
async def test_speech_started_cancels_drain_without_blocking_receiver():
    b = bridge()
    b._send = AsyncMock()
    started = asyncio.Event()

    async def drain():
        started.set()
        await asyncio.Event().wait()

    b._on_turn_complete = drain
    await b._handle_event({"type": "response.created", "response": {"id": "old"}})
    await b._handle_event({"type": "response.done", "response": {"id": "old"}})
    await started.wait()
    task = b._turn_task
    await asyncio.wait_for(
        b._handle_event({"type": "input_audio_buffer.speech_started"}), 0.2
    )
    assert task.cancelled()
    b._on_interrupt.assert_awaited_once()


@pytest.mark.asyncio
async def test_pcm_budget_overflow_and_vad_release_active_audio_then_accept_next():
    b = bridge()
    b._send = AsyncMock()
    b._output.set_pcm_budget(True)
    b._output.MAX_PCM_BYTES = 8
    started = asyncio.Event()
    async def blocked_audio(data):
        started.set()
        await asyncio.Event().wait()
    b._on_audio = blocked_audio
    await b._handle_event({'type': 'response.created', 'response': {'id': 'budget'}})
    delta = {'type': 'response.audio.delta', 'response_id': 'budget',
             'delta': base64.b64encode(b'12345678').decode()}
    await b._handle_event(delta)
    await started.wait()
    assert b._output.pending_audio_bytes == 8
    await asyncio.wait_for(b._handle_event(delta), .2)
    assert b._output.pending_audio_bytes == 0
    b._on_interrupt.assert_awaited_once()
    b._on_audio = AsyncMock()
    await b._handle_event({'type': 'response.created', 'response': {'id': 'new'}})
    await b._handle_event({**delta, 'response_id': 'new'})
    b._on_audio.assert_awaited_once_with(b'12345678')
    await b._handle_event({'type': 'input_audio_buffer.speech_started'})
    assert b._output.pending_audio_bytes == 0


@pytest.mark.asyncio
async def test_manual_release_commits_once_without_waiting_for_silence():
    b = bridge()
    b.set_manual_turn_mode(True)
    b._send = AsyncMock()
    await b.begin_manual_turn()
    await b.send_audio(b'\0\0' * 3200)  # product minimum: 200 ms at 16 kHz
    await b.send_audio_stream_end()
    await b.send_audio_stream_end()
    kinds = [call.args[0]['type'] for call in b._send.call_args_list]
    assert kinds.count('input_audio_buffer.commit') == 1
    assert kinds.count('response.create') == 1
    assert kinds.index('input_audio_buffer.commit') < kinds.index('response.create')
    await b._handle_event({'type': 'input_audio_buffer.committed', 'item_id': 'late'})
    assert sum(c.args[0]['type'] == 'response.create' for c in b._send.call_args_list) == 1


@pytest.mark.asyncio
async def test_empty_short_and_auto_audio_end_never_commit():
    b = bridge()
    b._send = AsyncMock()
    await b.send_audio_stream_end()
    b._send.assert_not_awaited()
    b.set_manual_turn_mode(True)
    for pcm in (b'', b'\0\0' * 320):
        await b.begin_manual_turn()
        await b.send_audio(pcm)
        assert await b.send_audio_stream_end() is False
    kinds = [c.args[0]['type'] for c in b._send.call_args_list]
    assert 'input_audio_buffer.commit' not in kinds
    assert 'response.create' not in kinds


@pytest.mark.asyncio
async def test_manual_busy_begin_error_and_next_turn_recovery():
    b=bridge();b.set_manual_turn_mode(True);b._send=AsyncMock()
    b._response_requested=True
    await b.begin_manual_turn()
    b._send.assert_not_awaited()
    await b._handle_event({'type':'error','error':{'code':'invalid_request_error','param':'audio'}})
    assert not b._response_requested and not b._manual_open
    b._on_session_dead.assert_awaited_once()
    await b.begin_manual_turn()
    await b._handle_event({'type':'input_audio_buffer.speech_started'})
    await b._handle_event({'type':'input_audio_buffer.speech_stopped'})
    assert b._manual_open
    await b.send_audio(b'\0\0'*3200)
    await b.send_audio_stream_end()
    assert sum(c.args[0]['type']=='response.create' for c in b._send.call_args_list)==1


@pytest.mark.asyncio
async def test_proxy_gbot_default_vad_short_stop_and_valid_stop(monkeypatch):
    monkeypatch.setenv('STACKCHAN_MANUAL_VAD','1')
    b=bridge();b.set_manual_turn_mode(True);b._send=AsyncMock()
    proxy=GbotVoiceProxy();proxy._bridge=b;proxy._ready_event.set();proxy._codec=object()
    b.running=True;b._connected_event.set()
    proxy._send_to_device=AsyncMock();proxy._device_pcm_downlink=True
    proxy._set_leds_safe=AsyncMock()
    for pcm in (b'\0'*640,b'\0'*6400):
        await proxy.send_device_json({'type':'listen','state':'start','mode':'manual'})
        await b.send_audio(pcm)
        await proxy.send_device_json({'type':'listen','state':'stop'})
        await proxy.send_device_json({'type':'listen','state':'stop'})
    messages=[c.args[0] for c in proxy._send_to_device.call_args_list]
    assert any('input_short' in m for m in messages)
    kinds=[c.args[0]['type'] for c in b._send.call_args_list]
    assert kinds.count('input_audio_buffer.commit')==1
    assert kinds.count('response.create')==1


@pytest.mark.asyncio
async def test_manual_error_drops_late_response_audio_and_done():
    b=bridge();b.set_manual_turn_mode(True);b._send=AsyncMock()
    await b._handle_event({'type':'response.created','response':{'id':'old'}})
    await b._handle_event({'type':'error','error':{'code':'server_error'}})
    delta={'type':'response.audio.delta','response_id':'old','delta':base64.b64encode(b'xx').decode()}
    await b._handle_event(delta)
    await b._handle_event({'type':'response.done','response':{'id':'old'}})
    b._on_audio.assert_not_awaited()
    b._on_turn_complete.assert_not_awaited()
    await b._handle_event({'type':'response.created','response':{'id':'new'}})
    await b._handle_event({**delta,'response_id':'new'})
    b._on_audio.assert_awaited_once_with(b'xx')


@pytest.mark.asyncio
async def test_default_never_executes_harness_tools(monkeypatch):
    monkeypatch.delenv("STACKCHAN_GBOT_VOICE_TOOLS", raising=False)
    b = bridge()
    b._tool = AsyncMock()
    await b._handle_event(
        {"type": "response.function_call_arguments.done", "name": "send"}
    )
    assert not b._tools
    b._tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_start_does_not_require_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    fake = bridge()
    fake.start = AsyncMock()
    fake.wait_connected = AsyncMock(return_value=True)
    fake.running = True
    fake._connected_event.set()
    proxy = GbotVoiceProxy(
        bridge_factory=lambda **kwargs: fake, codec_factory=lambda: object()
    )
    assert await proxy.start(type("Info", (), {"device_id": "test"})(), {}, AsyncMock())
    assert proxy.connected
    assert proxy.server_hello["provider"] == "gbot_voice"
    await proxy.stop()


@pytest.mark.asyncio
async def test_interrupt_clears_proxy_pcm_queue_and_sends_stop():
    class Codec:
        def reset_encode_buffer(self):
            pass

    proxy = GbotVoiceProxy(codec_factory=Codec)
    proxy._codec = Codec()
    proxy._send_to_device = AsyncMock()
    proxy._tts_active = True
    proxy._pcm_prebuffer.extend(b"stale-pcm")
    proxy._frame_queue.put_nowait(b"stale-opus")
    proxy._signal_speaking_leds = lambda *args: None
    proxy._drain_task = asyncio.create_task(asyncio.Event().wait())
    await proxy._abort_in_flight_tts()
    assert proxy._frame_queue.empty()
    assert not proxy._pcm_prebuffer
    assert not proxy._tts_active
    assert proxy._drain_task is None
    proxy._send_to_device.assert_awaited_once_with('{"type": "tts", "state": "stop"}')


@pytest.mark.asyncio
async def test_startup_error_redacts_upstream_secrets(caplog):
    b = bridge()
    b._local = AsyncMock(side_effect=RuntimeError("SECRET_FIXTURE"))
    with pytest.raises(RuntimeError, match="details redacted") as caught:
        await b.start()
    assert "SECRET_FIXTURE" not in str(caught.value)
    assert "SECRET_FIXTURE" not in caplog.text
    assert "stage=account_requests error_type=RuntimeError" in caplog.text
    assert b.metrics["startup_stage"] == "account_requests"
    assert b.metrics["startup_error_type"] == "RuntimeError"
    assert b._http.closed


def test_model_is_bound_to_signed_in_credential():
    from stackchan_mcp.gbot_voice_bridge import voice_url_with_model

    assert voice_url_with_model(
        "wss://api.x.ai/v1/realtime?model=old", "signed/model"
    ) == ("wss://api.x.ai/v1/realtime?model=signed%2Fmodel")


@pytest.mark.asyncio
async def test_first_speech_start_does_not_cancel_or_count_interruption():
    b = bridge()
    b._send = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    assert b.metrics["speech_starts"] == 1
    assert b.metrics["interruptions"] == 0
    b._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_metrics_only_store_allowlisted_enums():
    b = bridge()
    await b._handle_event(
        {
            "type": "error",
            "error": {
                "code": "SECRET_CODE",
                "param": "SECRET_PARAM",
                "message": "SECRET_BODY",
            },
        }
    )
    assert b.metrics["server_errors"] == {"other:other": 1}


@pytest.mark.asyncio
async def test_late_old_done_does_not_finish_new_response():
    b = bridge()
    b._send = AsyncMock()
    await b._handle_event({"type": "response.created", "response": {"id": "old"}})
    await b.interrupt()
    await b._handle_event({"type": "response.created", "response": {"id": "next"}})
    await b._handle_event({"type": "response.done", "response": {"id": "old"}})
    assert b._response_in_progress
    assert b._active_response == "next"
    assert b._turn_task is None


@pytest.mark.asyncio
async def test_native_registration_failure_keeps_voice_start_running(monkeypatch):
    import time
    from types import SimpleNamespace
    from stackchan_mcp import gbot_voice_bridge as module

    b = bridge()
    ws = SimpleNamespace(closed=False, send_json=AsyncMock(), close=AsyncMock())
    http = SimpleNamespace(ws_connect=AsyncMock(return_value=ws), close=AsyncMock())
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **kwargs: http)

    async def local(path, payload=None):
        if path == "/credential":
            return {
                "credential": {
                    "websocketUrl": "wss://api.x.ai/v1/realtime",
                    "model": "fixture",
                    "clientSecret": "PRIVATE_FIXTURE",
                    "expiresAtUnixSeconds": time.time() + 60,
                }
            }
        return {"instructions": "fixture", "tools": []}

    b._local = local

    def configure(session):
        b._desktop_enabled = True
        return []

    b._configure_tools = configure

    async def receive():
        b._connected_event.set()
        await asyncio.Event().wait()

    b._receive = receive
    monkeypatch.setattr(
        module.NativeVoiceChannel,
        "_presence",
        AsyncMock(side_effect=RuntimeError("offline")),
    )
    await asyncio.wait_for(b.start(), 0.2)
    assert await b.wait_connected(0.2)
    assert b.running and not b._native.ready
    await b.stop()
    assert b._native._task.done()
