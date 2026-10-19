"""stdio MCP server for MCP client.

Exposes stackchan tools via the MCP Python SDK's stdio transport.
Each tool call is relayed to the connected ESP32 device.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import logging
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .gateway import get_gateway
from .tts import synthesize_and_send

logger = logging.getLogger(__name__)

DEVICE_TOOL_TIMEOUT_S = 2.0


def _usb_connected(gateway: Any) -> bool:
    usb = getattr(gateway, "usb_transport", None)
    return usb is not None and bool(getattr(usb, "connected", False))


def _status_with_transports(gateway: Any) -> dict[str, Any]:
    if hasattr(gateway.esp32, "get_status_with_transports"):
        return gateway.esp32.get_status_with_transports()
    status = dict(gateway.esp32.get_status())
    usb = getattr(gateway, "usb_transport", None)
    usb_connected = usb is not None and bool(getattr(usb, "connected", False))
    ws_connected = bool(status.get("connected"))
    status["ws_connected"] = ws_connected
    status["usb_connected"] = usb_connected
    if usb_connected and not ws_connected:
        status["connected"] = True
        status["transport"] = "usb"
    elif ws_connected:
        status["transport"] = "websocket"
    else:
        status["transport"] = None
    return status


def _format_tool_result(result: Any) -> list[TextContent]:
    if isinstance(result, dict):
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            if texts:
                return [TextContent(type="text", text="\n".join(texts))]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return [TextContent(type="text", text=str(result))]


async def _call_device_tool(
    gateway: Any,
    name: str,
    arguments: dict[str, Any],
) -> tuple[Any, dict[str, Any] | None]:
    usb = getattr(gateway, "usb_transport", None)
    if usb is not None and getattr(usb, "connected", False):
        try:
            data = await usb.call_tool(name, arguments, timeout_s=DEVICE_TOOL_TIMEOUT_S)
            return data.get("result", data), None
        except Exception as exc:
            if not getattr(gateway.esp32, "device_connected", False):
                return None, {"code": -32000, "message": str(exc)}
            logger.debug("stdio USB %s failed; falling back to WS: %s", name, exc)

    return await gateway.esp32.call_tool(name, arguments)


def create_server() -> Server:
    """Create and configure the MCP server with tool handlers."""
    server = Server("stackchan-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available stackchan tools.

        Tools prefixed with ESP32 names (self.*) are relayed to the device.
        get_status is handled locally by the gateway.
        """
        return [
            Tool(
                name="get_status",
                description=(
                    "Get the gateway's connection status: whether ESP32 is connected, "
                    "device info, and list of available device tools."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="set_voice_mode",
                description=(
                    "Enable or disable STT-to-cmux voice input. When enabled, "
                    "cloud STT text is treated as untrusted user input, prefixed "
                    "with [voice], and sent only to a validated Claude Code surface."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean", "description": "True to enable voice input mode."},
                        "surface": {
                            "type": "string",
                            "description": "Optional cmux target surface ref, e.g. surface:263.",
                        },
                    },
                    "required": ["enabled"],
                },
            ),
            Tool(
                name="get_device_info",
                description=(
                    "Get real-time device information from ESP32: "
                    "battery level, speaker volume, screen brightness, network status, etc."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="take_photo",
                description=(
                    "Take a photo with the robot's camera and ask a question about it. "
                    "The device captures an image and returns an AI-generated description."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Question to ask about the photo (e.g. 'What do you see?')",
                        },
                    },
                    "required": ["question"],
                },
            ),
            Tool(
                name="set_volume",
                description="Set the speaker volume (0-100).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "volume": {
                            "type": "integer",
                            "description": "Volume level (0-100)",
                        },
                    },
                    "required": ["volume"],
                },
            ),
            Tool(
                name="set_brightness",
                description="Set the screen brightness (0-100).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "brightness": {
                            "type": "integer",
                            "description": "Brightness level (0-100)",
                        },
                    },
                    "required": ["brightness"],
                },
            ),
            Tool(
                name="set_background_color",
                description=(
                    "Set the LCD task-state background color. "
                    "Use red for errors, green for pass/done, yellow for confirmation. "
                    "duration_ms is optional; when omitted or 0 the color stays until "
                    "the next set_background_color call."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "r": {"type": "integer", "minimum": 0, "maximum": 255},
                        "g": {"type": "integer", "minimum": 0, "maximum": 255},
                        "b": {"type": "integer", "minimum": 0, "maximum": 255},
                        "duration_ms": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 600000,
                            "default": 0,
                        },
                    },
                    "required": ["r", "g", "b"],
                },
            ),
            Tool(
                name="speak",
                description=(
                    "Make StackChan speak a short sentence. The gateway synthesizes "
                    "24kHz Opus audio with the configured voice engine, sends "
                    "tts/start, streams Opus frames, then sends tts/stop. "
                    "emotion optionally changes the visible expression first."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Sentence to speak.",
                        },
                        "emotion": {
                            "type": "string",
                            "description": "Optional expression/emotion, such as happy, sad, or thinking.",
                        },
                    },
                    "required": ["text"],
                },
            ),
            Tool(
                name="move_head",
                description=(
                    "Move the robot's head to the specified angles. "
                    "yaw: horizontal (-90 to 90), pitch: vertical (0 to 60)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "yaw": {
                            "type": "integer",
                            "minimum": -90,
                            "maximum": 90,
                            "description": "Horizontal angle in degrees (-90 to 90)",
                        },
                        "pitch": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 60,
                            "description": "Vertical angle in degrees (0 to 60)",
                        },
                        "speed": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 1000,
                            "default": 150,
                            "description": "Movement speed (100-1000, 150 natural)",
                        },
                    },
                    "required": ["yaw", "pitch"],
                },
            ),
            Tool(
                name="get_head_angles",
                description="Get the robot's current head angles: yaw and pitch in degrees.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="gpio_test",
                description="Test GPIO6 pin by toggling HIGH/LOW 5 times. Check if servo reacts.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="uart_diag",
                description="Send raw servo bytes via UART and report write result.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="check_vm_en",
                description=(
                    "Diagnostic: read PY32 REG_GPIO_O_L and report whether VM EN "
                    "(pin 0 = servo power) is currently HIGH. Returns "
                    "{io_expander_present, i2c_read_ok, raw, vm_en_high}."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="set_avatar",
                description=(
                    "Switch the avatar face shown on the LCD. "
                    "Choose one of the supported avatar logical states; this is "
                    "the robot's actual visible expression, not just a label. "
                    "Pass 'off' to hide the avatar and disable blink, exposing the "
                    "underlying xiaozhi-esp32 screens (WiFi config UI, OTA, settings); "
                    "any other face brings the avatar back and restores blink."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "face": {
                            "type": "string",
                            "enum": [
                                "idle",
                                "happy",
                                "thinking",
                                "working",
                                "working_typing",
                                "juggling",
                                "sweeping",
                                "building",
                                "debugger",
                                "conducting",
                                "idle_reading",
                                "error",
                                "attention",
                                "notification",
                                "carrying",
                                "yawning",
                                "dozing",
                                "collapsing",
                                "sleeping",
                                "waking",
                                "sad",
                                "surprised",
                                "embarrassed",
                                "off",
                            ],
                            "description": (
                                "One of the supported avatar logical states, or off."
                            ),
                        },
                    },
                    "required": ["face"],
                },
            ),
            Tool(
                name="install_avatar_assets",
                description=(
                    "Download generated RGB565 avatar animation files from an "
                    "HTTP directory to the StackChan SD card."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base_url": {
                            "type": "string",
                            "description": (
                                "HTTP directory containing IDLE.RGB, HAPPY.RGB, "
                                "and the other generated 8.3 RGB files."
                            ),
                        },
                    },
                    "required": ["base_url"],
                },
            ),
            Tool(
                name="set_mouth",
                description=(
                    "Set the avatar mouth shape for lip-sync. "
                    "The shape is held until the next set_avatar / set_mouth call, "
                    "or until an autonomous blink restores the resting face. "
                    "Calling this while a set_mouth_sequence is in flight "
                    "interrupts the sequence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mouth": {
                            "type": "string",
                            "enum": ["closed", "half", "open", "e", "u"],
                            "description": "One of: closed, half, open, e, u.",
                        },
                    },
                    "required": ["mouth"],
                },
            ),
            Tool(
                name="set_mouth_sequence",
                description=(
                    "Queue a lip-sync sequence and play it on the device. "
                    "Each step holds 'shape' for 'duration_ms' before "
                    "advancing. The firmware walks the queue locally so "
                    "there is no per-step network RTT (use this instead of "
                    "issuing many set_mouth calls back-to-back from a TTS "
                    "loop). Returns immediately with the queued step count "
                    "and estimated total duration. Calling set_mouth, "
                    "set_avatar, or this tool again interrupts the in-flight "
                    "sequence and replaces it. Autonomous blink is paused "
                    "while a sequence is playing and resumed when it ends. "
                    "The final shape is held until the next "
                    "set_mouth / set_avatar call, or until an autonomous "
                    "blink restores the resting face — this is the same "
                    "Phase 2 trade-off that applies to set_mouth, since the "
                    "blink animation ends by repainting the full face. If "
                    "the final shape must persist visually, disable blink "
                    "with set_blink(false) before the sequence (or append a "
                    "closed step if you just want the mouth to close at "
                    "the end)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 256,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "shape": {
                                        "type": "string",
                                        "enum": ["closed", "half", "open", "e", "u"],
                                        "description": (
                                            "Mouth shape for this step. "
                                            "One of: closed, half, open, e, u."
                                        ),
                                    },
                                    "duration_ms": {
                                        "type": "integer",
                                        "minimum": 10,
                                        "maximum": 10000,
                                        "description": (
                                            "How long to hold this shape "
                                            "before advancing, in ms (10..10000)."
                                        ),
                                    },
                                },
                                "required": ["shape", "duration_ms"],
                            },
                            "description": (
                                "Ordered list of mouth shapes with hold "
                                "durations (1..256 steps)."
                            ),
                        },
                    },
                    "required": ["steps"],
                },
            ),
            Tool(
                name="set_blink",
                description=(
                    "Enable or disable autonomous avatar animation. "
                    "When enabled, the current face plays its frame sequence "
                    "continuously, including breathing, blinking, and state motion."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "description": "True to start blinking, false to stop.",
                        },
                    },
                    "required": ["enabled"],
                },
            ),
            Tool(
                name="get_touch_state",
                description=(
                    "Read the head-touch (Si12T) sensor state and the most recent "
                    "gesture event (tap/stroke/idle). Returns per-zone booleans, "
                    "the raw output byte, and how long ago the last event fired."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="set_led",
                description=(
                    "Set a single RGB LED on the StackChan base. There are 12 LEDs "
                    "arranged in two rows of 6 (index 0..11). Updates immediately."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "LED index (0..11)",
                            "minimum": 0,
                            "maximum": 11,
                        },
                        "r": {"type": "integer", "description": "Red 0..255", "minimum": 0, "maximum": 255},
                        "g": {"type": "integer", "description": "Green 0..255", "minimum": 0, "maximum": 255},
                        "b": {"type": "integer", "description": "Blue 0..255", "minimum": 0, "maximum": 255},
                    },
                    "required": ["index", "r", "g", "b"],
                },
            ),
            Tool(
                name="set_all_leds",
                description="Set all 12 RGB LEDs on the StackChan base to the same color. Updates immediately.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "r": {"type": "integer", "description": "Red 0..255", "minimum": 0, "maximum": 255},
                        "g": {"type": "integer", "description": "Green 0..255", "minimum": 0, "maximum": 255},
                        "b": {"type": "integer", "description": "Blue 0..255", "minimum": 0, "maximum": 255},
                    },
                    "required": ["r", "g", "b"],
                },
            ),
            Tool(
                name="set_leds",
                description=(
                    "Set multiple RGB LEDs in one shot. 'colors' is an array of "
                    "[r,g,b] triples starting at index 0 (e.g. [[255,0,0],[0,255,0]]). "
                    "Up to 12 entries; extras are ignored, missing entries keep their "
                    "previous color. Use this for animations / patterns to avoid 12x "
                    "I2C round-trips."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "colors": {
                            "type": "array",
                            "description": "Array of [r,g,b] triples, each 0..255",
                            "items": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 0, "maximum": 255},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                            "minItems": 1,
                            "maxItems": 12,
                        },
                    },
                    "required": ["colors"],
                },
            ),
            Tool(
                name="clear_leds",
                description="Turn off all 12 RGB LEDs on the StackChan base.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="say",
                description=(
                    "Speak the given text on the device speaker via gateway-side "
                    "TTS (Phase 4, Issue #70). The gateway synthesises audio, "
                    "encodes it to Opus, and pushes frames over the existing "
                    "WebSocket — the device firmware does not change. Engine is "
                    "selectable via 'voice' (default 'voicevox'). "
                    "NOTE: this build ships the framework only; concrete engines "
                    "(VOICEVOX, Irodori) land in follow-up PRs and require the "
                    "matching optional extra (e.g. "
                    "'pip install stackchan-mcp[tts-voicevox]'). Calling this tool "
                    "before an engine is registered returns a clear error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to speak. Must be non-empty.",
                        },
                        "voice": {
                            "type": "string",
                            "description": (
                                "Engine identifier (e.g. 'voicevox', 'irodori'). "
                                "Default 'voicevox'."
                            ),
                            "default": "voicevox",
                        },
                        "speaker_id": {
                            "type": "integer",
                            "description": (
                                "Engine-specific speaker identifier "
                                "(e.g. a VOICEVOX speaker ID)."
                            ),
                        },
                        "reference_audio": {
                            "type": "string",
                            "description": (
                                "Path to a reference audio file used by "
                                "voice-cloning engines (e.g. Irodori). "
                                "Ignored by engines that do not support it."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        """Handle a tool call by relaying to ESP32."""
        arguments = arguments or {}
        gw = get_gateway()

        if name == "get_status":
            # get_status is handled locally — no ESP32 needed
            status = _status_with_transports(gw)
            return [TextContent(type="text", text=json.dumps(status, indent=2))]

        if name == "set_voice_mode":
            result = gw.voice_input_bridge.set_enabled(
                bool(arguments.get("enabled")),
                surface=str(arguments.get("surface") or "") or None,
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        if name == "say":
            # TTS runs on the gateway side. The orchestrator validates
            # arguments, looks up an engine, synthesises PCM, encodes
            # Opus, and pushes frames through the WebSocket binary
            # channel that the device's audio decoder consumes. Errors
            # are surfaced as clean MCP error JSON rather than letting
            # tracebacks leak into the agent's transcript.
            try:
                result = await synthesize_and_send(arguments, gateway=gw)
            except (ValueError, NotImplementedError, RuntimeError) as exc:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)}),
                    )
                ]
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "speak":
            if not gw.esp32.device_connected:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "No WebSocket ESP32 device connected. Please check the device."}),
                    )
                ]
            result, error = await gw.esp32.speak(
                str(arguments.get("text", "")),
                str(arguments.get("emotion")) if arguments.get("emotion") else None,
            )
            if error:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": error.get("message", str(error)), "result": result}),
                    )
                ]
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        if not (gw.esp32.device_connected or _usb_connected(gw)):
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "No ESP32 device connected. Please check the device."}),
                )
            ]

        # Map MCP client tool names to ESP32 MCP tool names (self.* prefix)
        tool_map: dict[str, tuple[str, dict[str, Any]]] = {
            "get_device_info": (
                "self.get_device_status",
                {},
            ),
            "take_photo": (
                "self.camera.take_photo",
                arguments,
            ),
            "set_volume": (
                "self.audio_speaker.set_volume",
                arguments,
            ),
            "set_brightness": (
                "self.screen.set_brightness",
                arguments,
            ),
            "set_background_color": (
                "self.display.set_background_color",
                {
                    "r": arguments.get("r"),
                    "g": arguments.get("g"),
                    "b": arguments.get("b"),
                    "duration_ms": arguments.get("duration_ms", 0),
                },
            ),
            "move_head": (
                "self.robot.set_head_angles",
                {
                    "yaw": max(-90, min(90, int(arguments.get("yaw", 0)))),
                    "pitch": max(0, min(60, int(arguments.get("pitch", 0)))),
                    "speed": max(100, min(1000, int(arguments.get("speed", 150)))),
                },
            ),
            "get_head_angles": (
                "self.robot.get_head_angles",
                {},
            ),
            "gpio_test": (
                "self.robot.gpio_test",
                {},
            ),
            "uart_diag": (
                "self.robot.uart_diag",
                {},
            ),
            "check_vm_en": (
                "self.robot.check_vm_en",
                {},
            ),
            "set_avatar": (
                "self.display.set_avatar",
                arguments,
            ),
            "install_avatar_assets": (
                "self.display.install_avatar_assets",
                arguments,
            ),
            "set_mouth": (
                "self.display.set_mouth",
                arguments,
            ),
            # The MCP Property type system on ESP32 only supports
            # string/integer/boolean, so we serialise the steps array to
            # a JSON string here. The firmware decodes it via cJSON.
            "set_mouth_sequence": (
                "self.display.set_mouth_sequence",
                {"steps_json": json.dumps(arguments.get("steps", []))},
            ),
            "set_blink": (
                "self.display.set_blink",
                arguments,
            ),
            "get_touch_state": (
                "self.touch.get_touch_state",
                {},
            ),
            "set_led": (
                "self.led.set_color",
                arguments,
            ),
            "set_all_leds": (
                "self.led.set_all",
                arguments,
            ),
            # Firmware accepts colors as a JSON-encoded string (the on-device
            # MCP layer has no array property type), so re-pack the Python
            # list here. The schema we exposed above still lets the LLM
            # think in real arrays.
            "set_leds": (
                "self.led.set_many",
                {"colors": json.dumps(arguments.get("colors", []))},
            ),
            "clear_leds": (
                "self.led.clear",
                {},
            ),
        }

        if name not in tool_map:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}),
                )
            ]

        esp32_name, esp32_args = tool_map[name]
        result, error = await _call_device_tool(gw, esp32_name, esp32_args)

        if error:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": error.get("message", str(error))}),
                )
            ]

        # result from ESP32 is MCP format: {"content": [...], "isError": bool}
        return _format_tool_result(result)

    return server


async def run_stdio_server() -> None:
    """Run the MCP server on stdio."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        logger.info("stdio MCP server starting")
        await _run_server_with_touch_notifications(server, read_stream, write_stream)


async def _run_server_with_touch_notifications(
    server: Server,
    read_stream: Any,
    write_stream: Any,
) -> None:
    """Run the MCP server and forward touch events as MCP log notifications."""
    async with AsyncExitStack() as stack:
        lifespan_context = await stack.enter_async_context(server.lifespan(server))
        session = await stack.enter_async_context(
            ServerSession(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
        )

        gateway = get_gateway()

        async def notify_touch(event: Any) -> None:
            await session.send_log_message(
                level="notice",
                data=event.to_payload(),
                logger="stackchan.touch",
            )

        unregister_touch = gateway.touch_bridge.add_listener(notify_touch)

        # Idempotent: cli._run also starts idle_behavior, but stand-alone stdio
        # entry points (tests, attach-mode reuse) won't go through cli, so the
        # stdio path starts it too. IdleBehavior.start() skips if already running.
        await gateway.idle_behavior.start()

        async with anyio.create_task_group() as tg:
            try:
                async for message in session.incoming_messages:
                    tg.start_soon(
                        server._handle_message,
                        message,
                        session,
                        lifespan_context,
                        False,
                    )
            finally:
                unregister_touch()
                await gateway.idle_behavior.stop()
                tg.cancel_scope.cancel()
