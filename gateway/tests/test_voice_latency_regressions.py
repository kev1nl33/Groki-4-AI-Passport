# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Receive/control latency regressions with deterministic blocked audio sinks."""

import asyncio
import base64
from types import SimpleNamespace as NS

import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gbot", "gemini"])
async def test_slow_audio_sink_does_not_block_interruption(provider):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    interrupted = asyncio.Event()

    async def audio(_pcm):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def noop():
        pass

    async def notify():
        interrupted.set()

    if provider == "gbot":
        bridge = GbotVoiceBridge(
            on_audio=audio,
            on_turn_complete=noop,
            on_interrupt=notify,
            on_session_dead=noop,
        )
        await bridge._handle_event(
            {"type": "response.created", "response": {"id": "old"}}
        )

        def handle_audio():
            return bridge._handle_event(
                {
                    "type": "response.audio.delta",
                    "response_id": "old",
                    "delta": base64.b64encode(b"old").decode(),
                }
            )

        def control():
            return bridge._handle_event({"type": "input_audio_buffer.speech_started"})

        bridge._send = lambda _event: noop()
    else:
        bridge = GeminiLiveBridge(
            None, api_key="test", system_instruction="test", on_audio=audio
        )
        bridge._on_interrupt = notify

        def handle_audio():
            return bridge._handle_message(
                NS(
                    server_content=NS(
                        model_turn=NS(
                            parts=[
                                NS(inline_data=NS(data=b"old", mime_type="audio/pcm"))
                            ]
                        )
                    )
                )
            )

        def control():
            return bridge._handle_message(NS(server_content=NS(interrupted=True)))

    producer = asyncio.create_task(handle_audio())
    try:
        await asyncio.wait_for(started.wait(), 0.2)
        await asyncio.wait_for(producer, 0.05)
        await asyncio.wait_for(control(), 0.05)
        assert interrupted.is_set()
        assert cancelled.is_set()
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gbot", "gemini"])
async def test_previous_turn_drain_does_not_block_new_turn_control(provider):
    draining = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    sequence = []

    async def end():
        sequence.append("end-start")
        if not draining.is_set():
            draining.set()
            await release.wait()
        sequence.append("end-finish")
        if sequence.count("end-finish") == 2:
            finished.set()

    async def audio(_pcm):
        sequence.append("audio")

    async def noop():
        pass

    if provider == "gbot":
        bridge = GbotVoiceBridge(
            on_audio=audio,
            on_turn_complete=end,
            on_interrupt=noop,
            on_session_dead=noop,
        )

        async def turn(identifier, with_audio=False):
            await bridge._handle_event(
                {"type": "response.created", "response": {"id": identifier}}
            )
            if with_audio:
                await bridge._handle_event(
                    {
                        "type": "response.audio.delta",
                        "response_id": identifier,
                        "delta": base64.b64encode(b"new").decode(),
                    }
                )
            await bridge._handle_event(
                {"type": "response.done", "response": {"id": identifier}}
            )
    else:
        bridge = GeminiLiveBridge(
            None,
            api_key="test",
            system_instruction="test",
            on_audio=audio,
            on_turn_complete=end,
        )

        async def turn(identifier, with_audio=False):
            if with_audio:
                await bridge._handle_message(
                    NS(
                        server_content=NS(
                            model_turn=NS(
                                parts=[
                                    NS(
                                        inline_data=NS(
                                            data=b"new", mime_type="audio/pcm"
                                        )
                                    )
                                ]
                            )
                        )
                    )
                )
            await asyncio.wait_for(
                bridge._handle_message(NS(server_content=NS(turn_complete=True))), 0.05
            )

    try:
        await asyncio.wait_for(turn("old"), 0.05)
        await asyncio.wait_for(draining.wait(), 0.2)
        await asyncio.wait_for(turn("new", with_audio=True), 0.05)
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), 0.2)
        assert sequence == [
            "end-start",
            "end-finish",
            "audio",
            "end-start",
            "end-finish",
        ]
    finally:
        release.set()
        await bridge.stop()


@pytest.mark.asyncio
async def test_gemini_interruption_discards_queued_opus_and_preserves_new_turn():
    from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy

    sent = []
    first_frame = asyncio.Event()
    new_stop = asyncio.Event()

    class Codec:
        def encode_pcm_24k(self, pcm):
            return [pcm] * (20 if pcm == b"old" else 1)

        def flush(self):
            return []

        def reset_encode_buffer(self):
            pass

    async def send(value):
        sent.append(value)
        if value == b"old":
            first_frame.set()
        if value == '{"type": "tts", "state": "stop"}' and b"new" in sent:
            new_stop.set()

    proxy = GeminiVoiceProxy(
        device_prebuffer_ms=0,
        device_tts_start_delay_s=0,
        device_frame_pace_ms=60,
        device_burst_ms=0,
    )
    proxy._codec = Codec()
    proxy._send_to_device = send
    bridge = GeminiLiveBridge(
        None,
        api_key="test",
        system_instruction="test",
        on_audio=proxy._handle_gemini_audio,
        on_turn_complete=proxy.end_tts,
    )
    # Match the production proxy wiring and exercise the server event boundary.
    bridge._on_interrupt = proxy._abort_in_flight_tts
    proxy._bridge = bridge

    async def audio(value):
        await bridge._handle_message(
            NS(
                server_content=NS(
                    model_turn=NS(
                        parts=[NS(inline_data=NS(data=value, mime_type="audio/pcm"))]
                    )
                )
            )
        )

    try:
        await audio(b"old")
        await asyncio.wait_for(first_frame.wait(), 0.2)
        await asyncio.wait_for(
            bridge._handle_message(NS(server_content=NS(turn_complete=True))), 0.05
        )
        await asyncio.wait_for(
            bridge._handle_message(NS(server_content=NS(interrupted=True))), 0.05
        )
        stop_index = next(
            i
            for i, value in enumerate(sent)
            if value == '{"type": "tts", "state": "stop"}'
        )
        await audio(b"new")
        await asyncio.wait_for(
            bridge._handle_message(NS(server_content=NS(turn_complete=True))), 0.05
        )
        await asyncio.wait_for(new_stop.wait(), 0.5)
        assert b"old" not in sent[stop_index + 1 :]
        assert sent.count(b"new") == 1
        assert not proxy._tts_active
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_gbot_login_credential_and_plan_are_requested_concurrently(monkeypatch):
    import time
    from stackchan_mcp import gbot_voice_bridge as module

    called = set()
    both = asyncio.Event()

    async def noop(*_args):
        pass

    class Socket:
        closed = False
        send_json = noop
        close = noop

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    class Http:
        async def ws_connect(self, *_args, **_kwargs):
            return Socket()

        close = noop

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **_kwargs: Http())
    bridge = GbotVoiceBridge(
        on_audio=noop, on_turn_complete=noop, on_interrupt=noop, on_session_dead=noop
    )

    async def local(path, payload=None):
        called.add(path)
        if len(called) == 2:
            both.set()
        await both.wait()
        if path == "/credential":
            return {
                "credential": {
                    "clientSecret": "FAKE_TEST_SECRET",
                    "expiresAtUnixSeconds": time.time() + 60,
                    "model": "test-model",
                    "websocketUrl": "wss://api.x.ai/v1/realtime",
                }
            }
        return {"instructions": "test", "tools": []}

    bridge._local = local
    try:
        await asyncio.wait_for(bridge.start(), 0.05)
        assert called == {"/credential", "/session"}
    finally:
        await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["interrupt", "new_output"])
async def test_gemini_idle_timer_starts_after_playback_drains_and_never_after_interruption(
    invalidate,
):
    ended = asyncio.Event()
    drain_started = asyncio.Event()
    release = asyncio.Event()

    async def end_conversation():
        ended.set()

    async def drain():
        drain_started.set()
        await release.wait()

    esp32 = NS(on_end_conversation=end_conversation)
    bridge = GeminiLiveBridge(
        esp32,
        api_key="test",
        system_instruction="test",
        on_turn_complete=drain,
        conversation_idle_timeout_s=0.02,
    )
    try:
        await asyncio.wait_for(
            bridge._handle_message(NS(server_content=NS(turn_complete=True))), 0.05
        )
        await asyncio.wait_for(drain_started.wait(), 0.2)
        await asyncio.sleep(0.04)
        assert not ended.is_set(), "Playback still active; idle timeout must not run"
        if invalidate == "interrupt":
            await bridge._handle_message(NS(server_content=NS(interrupted=True)))
        else:
            await bridge._handle_message(
                NS(server_content=NS(output_transcription=NS(text="new answer")))
            )
        release.set()
        await asyncio.sleep(0.04)
        assert not ended.is_set(), "Cancelled old completion must not arm its timer"
        await asyncio.wait_for(
            bridge._handle_message(NS(server_content=NS(turn_complete=True))), 0.05
        )
        await asyncio.wait_for(ended.wait(), 0.2)
    finally:
        release.set()
        await bridge.stop()


@pytest.mark.asyncio
async def test_gbot_start_failure_cancels_pending_login_request_and_closes_http(
    monkeypatch,
):
    from stackchan_mcp import gbot_voice_bridge as module

    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    closed = asyncio.Event()

    class Http:
        async def close(self):
            closed.set()

    async def noop(*_args):
        pass

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **_kwargs: Http())
    bridge = GbotVoiceBridge(
        on_audio=noop, on_turn_complete=noop, on_interrupt=noop, on_session_dead=noop
    )

    async def local(path, payload=None):
        if path == "/credential":
            await sibling_started.wait()
            raise RuntimeError("FAKE_PRIVATE_UPSTREAM_DETAIL")
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    bridge._local = local
    with pytest.raises(
        RuntimeError, match="^Grok Bot voice startup failed \\(details redacted\\)$"
    ):
        await asyncio.wait_for(bridge.start(), 0.05)
    assert sibling_cancelled.is_set()
    assert closed.is_set()
    assert not bridge.running


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gbot", "gemini"])
async def test_shutdown_from_output_callback_does_not_wait_for_itself_via_receiver(
    provider,
):
    stopped = asyncio.Event()
    receiving = asyncio.Event()

    async def noop(*_args):
        pass

    async def end():
        await bridge.stop()
        stopped.set()

    if provider == "gbot":

        class Socket:
            async def close(self):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                receiving.set()
                await asyncio.Event().wait()

        bridge = GbotVoiceBridge(
            on_audio=noop, on_turn_complete=end, on_interrupt=noop, on_session_dead=noop
        )
        bridge._ws = Socket()
        bridge._task = asyncio.create_task(bridge._receive())

        async def complete():
            await bridge._handle_event({"type": "response.done"})
    else:

        class Session:
            async def close(self):
                pass

            async def receive(self):
                receiving.set()
                await asyncio.Event().wait()
                yield

        bridge = GeminiLiveBridge(
            None, api_key="test", system_instruction="test", on_turn_complete=end
        )
        bridge._session = Session()
        bridge._task = asyncio.create_task(bridge._receive_loop())

        async def complete():
            await bridge._handle_message(NS(server_content=NS(turn_complete=True)))

    try:
        await asyncio.wait_for(receiving.wait(), 0.2)
        await complete()
        await asyncio.wait_for(stopped.wait(), 0.2)
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_failed_gemini_playback_completion_does_not_arm_idle_timer():
    ended = asyncio.Event()

    async def end_conversation():
        ended.set()

    async def drain():
        raise RuntimeError("fake codec failure")

    bridge = GeminiLiveBridge(
        NS(on_end_conversation=end_conversation),
        api_key="test",
        system_instruction="test",
        on_turn_complete=drain,
        conversation_idle_timeout_s=0.02,
    )
    try:
        await bridge._handle_message(NS(server_content=NS(turn_complete=True)))
        await asyncio.sleep(0.04)
        assert not ended.is_set()
    finally:
        await bridge.stop()
