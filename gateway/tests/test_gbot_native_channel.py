# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""The authenticated helper boundary, using client-native update envelopes."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp.gbot_native_channel import NativeVoiceChannel, update_event


def test_work_landed_is_native_data_and_cannot_grant_new_user_authority():
    event, is_user = update_event(
        {
            "channel": "voice-call-nudge",
            "payload": {
                "agentId": "fixture-agent",
                "callId": "fixture-call",
                "kind": "outcome",
                "update": "Finished fixture task.",
            },
        },
        "fixture-agent",
        "fixture-call",
    )
    assert event["type"] == "async_update.add"
    assert event["async_update"]["topic"] == "work_landed"
    assert event["async_update"]["payload"][0]["message"] == {
        "type": "text",
        "content": "Finished fixture task.",
    }
    assert not is_user


def test_only_filtered_current_user_message_becomes_a_user_update():
    message = {
        "channel": "typed-message",
        "payload": {
            "agentId": "fixture-agent",
            "callId": "fixture-call",
            "entryId": "entry-7",
            "text": "Say hello.",
            "atMs": 123456789,
        },
    }
    event, is_user = update_event(message, "fixture-agent", "fixture-call")
    assert is_user
    assert event["async_update"]["topic"] == "user_message"
    assert event["event_id"] == "written-turn-entry-7"
    assert event["async_update"]["payload"][0]["timestampMs"] == 123456789
    assert update_event(message, "another-agent", "fixture-call") is None
    assert update_event(message, "fixture-agent", "another-call") is None


@pytest.mark.asyncio
async def test_native_channel_deduplicates_typed_and_drops_after_close():
    callback = AsyncMock()
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", callback
    )
    message = {
        "channel": "typed-message",
        "payload": {
            "agentId": "agent",
            "callId": "call",
            "entryId": "one",
            "text": "fixture",
            "atMs": 10,
        },
    }
    await channel.accept_event(message)
    await channel.accept_event(message)
    assert callback.await_count == 1
    await channel.close()
    await channel.accept_event(
        {**message, "payload": {**message["payload"], "entryId": "two"}}
    )
    assert callback.await_count == 1


@pytest.mark.asyncio
async def test_native_presence_and_sse_lifecycle():
    from aiohttp import ClientSession, web
    import json

    presence = []
    release = asyncio.Event()

    async def register(request):
        presence.append(await request.json())
        return web.json_response({"ok": True})

    async def events(request):
        assert request.query == {"agentId": "agent", "callId": "call"}
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        payload = {
            "channel": "voice-call-nudge",
            "payload": {
                "agentId": "agent",
                "callId": "call",
                "kind": "outcome",
                "update": "fixture result",
            },
        }
        await stream.write(("data: " + json.dumps(payload) + "\n\n").encode())
        await release.wait()
        return stream

    app = web.Application()
    app.router.add_post("/native/presence", register)
    app.router.add_get("/native/events", events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    got = asyncio.Event()
    received = []

    async def callback(event, is_user):
        received.append((event, is_user))
        got.set()

    try:
        async with ClientSession() as http:
            channel = NativeVoiceChannel(
                http, f"http://127.0.0.1:{port}", "agent", "call", callback
            )
            await channel.start()
            await asyncio.wait_for(got.wait(), 1)
            assert channel.connected.is_set()
            await channel.close()
        assert [item["isOnTheLine"] for item in presence] == [True, False]
        assert all(item["acceptsOverheard"] is False for item in presence)
        assert received[0][0]["async_update"]["topic"] == "work_landed"
        assert received[0][1] is False
        assert not channel.connected.is_set()
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_unavailable_presence_does_not_block_channel_start_and_stop_cancels():
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", AsyncMock()
    )
    channel._presence = AsyncMock(side_effect=RuntimeError("offline"))
    await asyncio.wait_for(channel.start(), 0.1)
    await asyncio.sleep(0)
    assert not channel.ready
    await channel.close()
    assert channel._task.done()


@pytest.mark.asyncio
async def test_presence_retry_recovers_readiness_without_restarting_voice():
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", AsyncMock()
    )
    channel._retry_delay = 0.001
    calls = 0

    async def presence(on):
        nonlocal calls
        if on:
            calls += 1
            if calls == 1:
                raise RuntimeError("offline")

    async def listen():
        channel.connected.set()
        await asyncio.Event().wait()

    channel._presence = presence
    channel._listen = listen
    await channel.start()
    await asyncio.wait_for(channel.connected.wait(), 0.2)
    assert channel.ready and calls == 2
    await channel.close()
    assert not channel.ready and channel._task.done()


@pytest.mark.asyncio
async def test_stop_cancels_inflight_registration_before_any_late_ready():
    channel = NativeVoiceChannel(
        None, "http://127.0.0.1:18771", "agent", "call", AsyncMock()
    )
    entered = asyncio.Event()

    async def presence(on):
        if on:
            entered.set()
            await asyncio.Event().wait()

    channel._presence = presence
    await channel.start()
    await entered.wait()
    await asyncio.wait_for(channel.close(), 0.1)
    assert channel._task.cancelled()
    assert not channel.ready
