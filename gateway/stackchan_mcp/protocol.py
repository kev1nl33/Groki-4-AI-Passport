"""xiaozhi-esp32 protocol definitions.

Defines message formats for communication between the gateway and ESP32 device.
Based on: xiaozhi-esp32/docs/mcp-protocol.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Transport-level messages (hello handshake)
# ---------------------------------------------------------------------------


class AudioParams(BaseModel):
    format: str = "opus"
    sample_rate: int = 16000
    channels: int = 1
    frame_duration: int = 60


# Stage C direct-PCM contract: the device sends 20 ms / 16 kHz / mono int16
# frames (640 bytes) and the gateway must not Opus-decode them.
PCM_DEVICE_FRAME_BYTES = 640
PCM_DEVICE_SAMPLE_RATE = 16000
PCM_DEVICE_FRAME_DURATION_MS = 20
PCM_SERVER_SAMPLE_RATE = 24000
PCM_DOWNLINK_FRAME_BYTES = 960          # 24000 Hz * 20 ms * 16 bit mono
PCM_DOWNLINK_FRAME_DURATION_MS = 20
PCM_DOWNLINK_QUEUE_FRAMES = 16          # ~320 ms bounded egress queue


@dataclass(frozen=True)
class DeviceAudioFormat:
    """Uplink audio format declared by one device hello.

    ``direct_pcm`` is True only for the exact Stage C PCM tuple; every other
    value keeps the historical Opus decode path. Unknown formats are reported
    as ``opus`` so no caller accidentally treats them as raw PCM.
    """

    format: str
    sample_rate: int
    channels: int
    frame_duration: int
    direct_pcm: bool


def _int_param(params: dict[str, Any], name: str, default: int) -> int | None:
    """Accept JSON integers only; strings/floats/bools are invalid, not defaults."""
    if name not in params:
        return default
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_device_audio_params(hello: dict[str, Any]) -> DeviceAudioFormat:
    """Parse ``hello.audio_params`` with an explicit legacy fallback.

    Supported uplink formats are Opus (default, existing behaviour) and the
    exact PCM tuple. PCM is enabled only when every parameter matches; a PCM
    declaration with any other rate/channel/frame size stays on the Opus path
    rather than mixing sample formats on the wire.
    """
    raw = hello.get("audio_params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    fmt = str(params.get("format", "opus") or "opus").strip().lower()
    rate_raw = _int_param(params, "sample_rate", 16000)
    channels_raw = _int_param(params, "channels", 1)
    frame_raw = _int_param(params, "frame_duration", 60)
    rate = 16000 if rate_raw is None else rate_raw
    channels = 1 if channels_raw is None else channels_raw
    frame = 60 if frame_raw is None else frame_raw
    direct = (
        fmt == "pcm"
        and rate_raw is not None
        and channels_raw is not None
        and frame_raw is not None
        and rate == PCM_DEVICE_SAMPLE_RATE
        and channels == 1
        and frame == PCM_DEVICE_FRAME_DURATION_MS
    )
    if fmt not in {"pcm", "opus"}:
        fmt = "opus"
    return DeviceAudioFormat(fmt, rate, channels, frame, direct)


class HelloMessage(BaseModel):
    """ESP32 -> Gateway: hello (connection announcement)."""

    type: str = "hello"
    version: int = 1
    features: dict[str, Any] = Field(default_factory=lambda: {"mcp": True})
    transport: str = "websocket"
    audio_params: AudioParams = Field(default_factory=AudioParams)
    session_id: str | None = None


class HelloResponse(BaseModel):
    """Gateway -> ESP32: hello response."""

    type: str = "hello"
    version: int = 1
    transport: str = "websocket"
    session_id: str | None = None
    # ESP32 defaults to 24 kHz / 60 ms for downlink TTS if omitted.
    # Be explicit so the gateway, cloud proxy, and firmware agree.
    audio_params: AudioParams = Field(
        default_factory=lambda: AudioParams(sample_rate=24000, frame_duration=60)
    )


# ---------------------------------------------------------------------------
# MCP message wrapper (over transport)
# ---------------------------------------------------------------------------


class McpMessage(BaseModel):
    """MCP message wrapper for transport.

    All MCP communication is wrapped in this envelope.
    """

    session_id: str = ""
    type: str = "mcp"
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------


def make_jsonrpc_request(method: str, params: dict[str, Any], req_id: int) -> dict[str, Any]:
    """Create a JSON-RPC 2.0 request payload."""
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": req_id,
    }


def make_mcp_message(
    session_id: str, method: str, params: dict[str, Any], req_id: int
) -> dict[str, Any]:
    """Create a full MCP transport message with JSON-RPC payload."""
    return {
        "session_id": session_id,
        "type": "mcp",
        "payload": make_jsonrpc_request(method, params, req_id),
    }


def parse_jsonrpc_response(payload: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    """Parse a JSON-RPC 2.0 response.

    Returns (result, error) — one of them will be None.
    """
    if "error" in payload:
        return None, payload["error"]
    return payload.get("result"), None
