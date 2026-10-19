"""Tests for xiaozhi cloud voice proxy."""

import asyncio
import json

import pytest
import pytest_asyncio
import websockets

from stackchan_mcp.cloud_proxy import CloudConnectionInfo, CloudProxy


@pytest_asyncio.fixture
async def cloud_server():
    state = {"headers": None, "messages": [], "ws": None}
    ready = asyncio.Event()

    async def handler(ws):
        state["headers"] = ws.request.headers
        state["ws"] = ws
        ready.set()
        async for message in ws:
            state["messages"].append(message)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}", state, ready
    server.close()
    await server.wait_closed()


async def _start_proxy(url: str, device_messages: list[str | bytes], ready: asyncio.Event) -> CloudProxy:
    proxy = CloudProxy()
    info = CloudConnectionInfo(
        cloud_url=url,
        authorization="Bearer test-token",
        protocol_version="1",
        device_id="aa:bb:cc:dd:ee:01",
        client_id="client-uuid",
    )

    async def send_to_device(message: str | bytes) -> None:
        device_messages.append(message)

    ok = await proxy.start(
        info,
        {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        },
        send_to_device,
    )
    assert ok is True
    await asyncio.wait_for(ready.wait(), timeout=3)
    for _ in range(30):
        if proxy.connected:
            break
        await asyncio.sleep(0.01)
    return proxy


@pytest.mark.asyncio
async def test_cloud_proxy_connects_with_device_headers_and_sends_hello(cloud_server):
    url, state, ready = cloud_server
    proxy = await _start_proxy(url, [], ready)
    await asyncio.sleep(0.1)

    assert state["headers"].get("Authorization") == "Bearer test-token"
    assert state["headers"].get("Protocol-Version") == "1"
    assert state["headers"].get("Device-Id") == "aa:bb:cc:dd:ee:01"
    assert state["headers"].get("Client-Id") == "client-uuid"
    assert json.loads(state["messages"][0])["type"] == "hello"

    await proxy.stop()


@pytest.mark.asyncio
async def test_device_binary_frame_is_forwarded_to_cloud(cloud_server):
    url, state, ready = cloud_server
    proxy = await _start_proxy(url, [], ready)

    assert await proxy.send_device_binary(b"opus-frame") is True
    await asyncio.sleep(0.1)

    assert b"opus-frame" in state["messages"]
    await proxy.stop()


@pytest.mark.asyncio
async def test_cloud_json_and_binary_are_forwarded_to_device(cloud_server):
    url, state, ready = cloud_server
    device_messages: list[str | bytes] = []
    proxy = await _start_proxy(url, device_messages, ready)

    await state["ws"].send(json.dumps({"type": "hello", "transport": "websocket"}))
    await state["ws"].send(json.dumps({"type": "tts", "state": "start"}))
    await state["ws"].send(b"tts-opus")
    await asyncio.sleep(0.1)

    assert proxy.server_hello == {"type": "hello", "transport": "websocket"}
    assert json.loads(device_messages[0]) == {"type": "tts", "state": "start"}
    assert device_messages[1] == b"tts-opus"
    await proxy.stop()


@pytest.mark.asyncio
async def test_cloud_mcp_ids_are_remapped_and_restored(cloud_server):
    url, state, ready = cloud_server
    device_messages: list[str | bytes] = []
    proxy = await _start_proxy(url, device_messages, ready)

    await state["ws"].send(
        json.dumps(
            {
                "session_id": "cloud-session",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "self.display.set_avatar", "arguments": {"face": "happy"}},
                    "id": 7,
                },
            }
        )
    )
    await asyncio.sleep(0.1)

    forwarded = json.loads(device_messages[0])
    proxy_id = forwarded["payload"]["id"]
    assert proxy_id != 7
    assert proxy_id >= 1_000_000

    handled = await proxy.try_handle_device_mcp(
        {
            "session_id": "local-session",
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": proxy_id,
                "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
            },
        }
    )
    assert handled is True
    await asyncio.sleep(0.1)

    restored = json.loads(state["messages"][-1])
    assert restored["payload"]["id"] == 7
    assert restored["payload"]["result"]["content"][0]["text"] == "ok"
    await proxy.stop()


@pytest.mark.asyncio
async def test_cloud_tts_request_streams_audio_to_device(cloud_server):
    url, state, ready = cloud_server
    device_messages: list[str | bytes] = []
    proxy = await _start_proxy(url, device_messages, ready)

    async def cloud_replies():
        while len(state["messages"]) < 5:
            await asyncio.sleep(0.01)
        start = json.loads(state["messages"][1])
        assert start == {
            "session_id": "local-session",
            "type": "listen",
            "state": "start",
            "mode": "auto",
        }
        assert state["messages"][2] == b"prompt-opus-1"
        assert state["messages"][3] == b"prompt-opus-2"
        assert json.loads(state["messages"][4]) == {
            "session_id": "local-session",
            "type": "listen",
            "state": "stop",
        }
        await state["ws"].send(json.dumps({"type": "stt", "text": "请只朗读下面这句话"}))
        await state["ws"].send(json.dumps({"type": "tts", "state": "start"}))
        await state["ws"].send(
            json.dumps(
                {
                    "type": "tts",
                    "state": "sentence_start",
                    "text": "你好，我是机器人",
                }
            )
        )
        await state["ws"].send(b"cloud-opus")
        await state["ws"].send(json.dumps({"type": "tts", "state": "stop"}))

    reply_task = asyncio.create_task(cloud_replies())
    result = await proxy.speak_text(
        "你好，我是机器人",
        session_id="local-session",
        prompt_audio_frames=[b"prompt-opus-1", b"prompt-opus-2"],
    )
    await reply_task

    assert result == {
        "ok": True,
        "text": "你好，我是机器人",
        "emotion": None,
        "frames_sent": 1,
        "provider": "xiaozhi_cloud",
        "error": None,
    }
    assert json.loads(device_messages[0]) == {"type": "tts", "state": "start"}
    assert json.loads(device_messages[1]) == {
        "type": "tts",
        "state": "sentence_start",
        "text": "你好，我是机器人",
    }
    assert device_messages[2] == b"cloud-opus"
    assert json.loads(device_messages[3]) == {"type": "tts", "state": "stop"}
    await proxy.stop()


@pytest.mark.asyncio
async def test_cloud_tts_reconnects_if_cloud_socket_went_idle(cloud_server):
    url, state, ready = cloud_server
    device_messages: list[str | bytes] = []
    proxy = await _start_proxy(url, device_messages, ready)

    await state["ws"].close()
    for _ in range(50):
        if not proxy.connected:
            break
        await asyncio.sleep(0.01)
    assert proxy.connected is False
    initial_count = len(state["messages"])

    async def cloud_replies():
        while len(state["messages"]) < initial_count + 5:
            await asyncio.sleep(0.01)
        assert json.loads(state["messages"][initial_count])["type"] == "hello"
        assert json.loads(state["messages"][initial_count + 1]) == {
            "session_id": "local-session",
            "type": "listen",
            "state": "start",
            "mode": "auto",
        }
        await state["ws"].send(json.dumps({"type": "tts", "state": "start"}))
        await state["ws"].send(b"cloud-opus")
        await state["ws"].send(json.dumps({"type": "tts", "state": "stop"}))

    reply_task = asyncio.create_task(cloud_replies())
    result = await proxy.speak_text(
        "你好，我是机器人",
        session_id="local-session",
        prompt_audio_frames=[b"prompt-opus-1", b"prompt-opus-2"],
    )
    await reply_task

    assert result["ok"] is True
    assert result["frames_sent"] == 1
    await proxy.stop()


def test_cloud_connection_info_requires_auth_to_enable(monkeypatch):
    monkeypatch.delenv("XIAOZHI_CLOUD_TOKEN", raising=False)
    info = CloudConnectionInfo.from_headers({})
    assert info.enabled is False


@pytest.mark.asyncio
async def test_cloud_stt_is_intercepted_when_voice_mode_enabled():
    handled = []
    forwarded = []

    class FakeVoiceBridge:
        enabled = True

        async def handle_stt_message(self, message):
            handled.append(message)

    proxy = CloudProxy(voice_bridge=FakeVoiceBridge())

    async def send_to_device(message):
        forwarded.append(message)

    proxy._send_to_device = send_to_device

    await proxy._handle_cloud_json(json.dumps({"type": "stt", "text": "你好"}))

    assert handled == [{"type": "stt", "text": "你好"}]
    assert forwarded == []
