"""MCP tool definitions for StackChan.

These definitions describe the ESP32 device's tool interface.
Used by the local stub router (mcp_router.py) for testing.
The stdio MCP server (stdio_server.py) defines its own tool list for MCP client.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Tool parameter schemas
# ---------------------------------------------------------------------------


class SetHeadAnglesParams(BaseModel):
    yaw: int = Field(ge=-90, le=90, description="Yaw angle in degrees (-90 to 90)")
    pitch: int = Field(ge=0, le=60, description="Pitch angle in degrees (0 to 60)")
    speed: int = Field(default=150, ge=100, le=1000, description="Movement speed (100-1000, 150 natural)")

    @field_validator("yaw", mode="before")
    @classmethod
    def _clamp_yaw(cls, value: object) -> int:
        angle = int(value)
        return max(-90, min(90, angle))

    @field_validator("pitch", mode="before")
    @classmethod
    def _clamp_pitch(cls, value: object) -> int:
        angle = int(value)
        return max(0, min(60, angle))

    @field_validator("speed", mode="before")
    @classmethod
    def _clamp_speed(cls, value: object) -> int:
        speed = int(value)
        return max(100, min(1000, speed))


class SetLedColorParams(BaseModel):
    r: int = Field(ge=0, le=255, description="Red (0-255)")
    g: int = Field(ge=0, le=255, description="Green (0-255)")
    b: int = Field(ge=0, le=255, description="Blue (0-255)")


class SetVolumeParams(BaseModel):
    volume: int = Field(ge=0, le=100, description="Volume level (0-100)")


# ---------------------------------------------------------------------------
# Tool registry (ESP32 device tools — used by local stub router)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "self.robot.get_head_angles",
        "description": "Get current head servo angles (yaw, pitch).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "self.robot.set_head_angles",
        "description": "Set head servo angles.",
        "inputSchema": SetHeadAnglesParams.model_json_schema(),
    },
    {
        "name": "self.robot.set_led_color",
        "description": "Set LED color (RGB).",
        "inputSchema": SetLedColorParams.model_json_schema(),
    },
    {
        "name": "self.audio_speaker.set_volume",
        "description": "Set speaker volume (0-100).",
        "inputSchema": SetVolumeParams.model_json_schema(),
    },
    {
        "name": "self.camera.take_photo",
        "description": "Take a photo with the device camera. Returns JPEG image.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "self.get_device_status",
        "description": "Get device status (battery, connection, angles).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
