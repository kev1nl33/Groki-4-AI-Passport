"""Tests for StackChan background color MCP bridge."""

import json

import pytest
from mcp.types import CallToolRequest, ListToolsRequest

from stackchan_mcp.stdio_server import create_server


@pytest.mark.asyncio
async def test_list_tools_includes_set_background_color():
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tool = next((t for t in result.root.tools if t.name == "set_background_color"), None)
    assert tool is not None
    schema = tool.inputSchema
    assert schema["properties"]["r"]["minimum"] == 0
    assert schema["properties"]["r"]["maximum"] == 255
    assert schema["properties"]["duration_ms"]["default"] == 0
    assert set(schema["required"]) == {"r", "g", "b"}


@pytest.mark.asyncio
async def test_set_background_color_relays_to_firmware(monkeypatch):
    calls = []

    class FakeESP32:
        device_connected = True

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"ok": True, "r": 0, "g": 255, "b": 0, "duration_ms": 2000}),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={
                "name": "set_background_color",
                "arguments": {"r": 0, "g": 255, "b": 0, "duration_ms": 2000},
            },
        )
    )

    assert calls == [
        (
            "self.display.set_background_color",
            {"r": 0, "g": 255, "b": 0, "duration_ms": 2000},
        )
    ]
    assert json.loads(result.root.content[0].text)["ok"] is True


@pytest.mark.asyncio
async def test_set_background_color_defaults_duration(monkeypatch):
    calls = []

    class FakeESP32:
        device_connected = True

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "set_background_color", "arguments": {"r": 255, "g": 0, "b": 0}},
        )
    )

    assert calls == [
        (
            "self.display.set_background_color",
            {"r": 255, "g": 0, "b": 0, "duration_ms": 0},
        )
    ]
