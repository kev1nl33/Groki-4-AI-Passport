"""Tests for the optional scripted demo reactions."""

import asyncio

import pytest

from stackchan_mcp.demo_reactions import (
    FACE_LINES,
    TOUCH_LINES,
    VISUAL_TOOL_TIMEOUT_S,
    DemoReactions,
)
from stackchan_mcp.touch_bridge import TouchEvent


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeESP32:
    device_connected = True

    def __init__(self) -> None:
        self.tools: list[tuple[str, dict]] = []
        self.spoken: list[tuple[str, str | None]] = []

    async def call_tool(self, name, arguments):
        self.tools.append((name, arguments))
        return {"ok": True}, None

    async def speak(self, text: str, emotion: str | None = None):
        self.spoken.append((text, emotion))
        return {"ok": True, "text": text}, None


class FakeUSB:
    connected = True

    def __init__(self) -> None:
        self.tools: list[tuple[str, dict, float | None]] = []

    async def call_tool(self, name, arguments, *, timeout_s=None):
        self.tools.append((name, arguments, timeout_s))
        return {"ok": True}


async def drain(reactions: DemoReactions) -> None:
    tasks = list(reactions._tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_face_enter_reacts_once_after_absence_and_cooldown():
    clock = FakeClock()
    esp32 = FakeESP32()
    usb = FakeUSB()
    reactions = DemoReactions(
        esp32,
        usb_transport=usb,
        clock=clock,
        face_absent_s=3.0,
        face_cooldown_s=20.0,
        enabled=True,
    )

    assert reactions.maybe_face_entered({"confidence": 0.9}) is True
    reactions.spawn_face_reaction()
    await drain(reactions)
    assert usb.tools[0][0] == "self.display.set_avatar"
    assert usb.tools[0][1] == {"face": "happy"}
    assert esp32.spoken[0][0] in FACE_LINES

    clock.advance(1.0)
    assert reactions.maybe_face_entered({"confidence": 0.9}) is False
    clock.advance(4.0)
    assert reactions.maybe_face_entered({"confidence": 0.9}) is False
    clock.advance(20.0)
    assert reactions.maybe_face_entered({"confidence": 0.9}) is True


@pytest.mark.asyncio
async def test_head_tap_gets_immediate_happy_face_and_scripted_line():
    clock = FakeClock()
    esp32 = FakeESP32()
    usb = FakeUSB()
    reactions = DemoReactions(
        esp32,
        usb_transport=usb,
        clock=clock,
        touch_cooldown_s=1.0,
        enabled=True,
    )
    event = TouchEvent(
        event="tap",
        age_ms=20,
        event_time_s=0.0,
        detected_at_s=0.02,
        zone1=True,
    )

    assert reactions.maybe_head_tap(event) is True
    reactions.spawn_touch_reaction()
    await drain(reactions)

    assert usb.tools[0][0] == "self.display.set_avatar"
    assert usb.tools[0][1] == {"face": "happy"}
    assert usb.tools[0][2] == VISUAL_TOOL_TIMEOUT_S
    assert VISUAL_TOOL_TIMEOUT_S < 0.1
    assert esp32.spoken[0][0] in TOUCH_LINES

    clock.advance(0.5)
    assert reactions.maybe_head_tap(event) is False
    clock.advance(0.6)
    assert reactions.maybe_head_tap(event) is True


def test_low_confidence_face_does_not_enter():
    reactions = DemoReactions(FakeESP32(), clock=FakeClock(), enabled=True)
    assert reactions.maybe_face_entered({"confidence": 0.1}) is False


def test_arm_face_entry_clears_cooldown_but_requires_real_entry():
    clock = FakeClock()
    reactions = DemoReactions(
        FakeESP32(),
        clock=clock,
        face_absent_s=3.0,
        face_cooldown_s=20.0,
        enabled=True,
    )

    assert reactions.maybe_face_entered({"confidence": 0.9}) is True
    clock.advance(1.0)
    assert reactions.maybe_face_entered({"confidence": 0.9}) is False

    reactions.arm_face_entry()

    assert reactions.maybe_face_entered({"confidence": 0.9}) is False
    clock.advance(3.1)
    assert reactions.maybe_face_entered({"confidence": 0.9}) is True


@pytest.mark.asyncio
async def test_demo_reactions_default_disabled_no_side_effects(monkeypatch):
    monkeypatch.delenv("STACKCHAN_DEMO_REACTIONS", raising=False)
    clock = FakeClock()
    esp32 = FakeESP32()
    usb = FakeUSB()
    reactions = DemoReactions(esp32, usb_transport=usb, clock=clock)

    event = TouchEvent(
        event="tap",
        age_ms=10,
        event_time_s=0.0,
        detected_at_s=0.01,
    )

    assert reactions.enabled is False
    assert reactions.maybe_face_entered({"confidence": 0.9}) is False
    assert reactions.maybe_head_tap(event) is False
    reactions.spawn_face_reaction()
    reactions.spawn_touch_reaction()
    reactions.spawn_intro()
    reactions.arm_face_entry()
    await drain(reactions)

    assert usb.tools == []
    assert esp32.tools == []
    assert esp32.spoken == []


def test_intro_line_uses_the_public_robot_name():
    from stackchan_mcp.demo_reactions import INTRO_LINE

    assert "Groki" in INTRO_LINE
    assert "Grok，" not in INTRO_LINE
