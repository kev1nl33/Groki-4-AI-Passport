"""Tests for the StackChan touch event polling bridge."""

import json

import pytest

from stackchan_mcp.touch_bridge import TouchEventBridge


class FakeESP32:
    def __init__(self, states):
        self.device_connected = True
        self._states = list(states)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name != "self.touch.get_touch_state":
            return {"content": [{"type": "text", "text": "true"}]}, None
        state = self._states.pop(0)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(state),
                }
            ],
        }, None


@pytest.mark.asyncio
async def test_poll_once_emits_new_tap_once():
    """A fresh tap is emitted once even if the next poll sees the same event."""
    esp32 = FakeESP32(
        [
            {"available": True, "last_event": "tap", "last_event_age_ms": 50},
            {"available": True, "last_event": "tap", "last_event_age_ms": 550},
        ]
    )
    bridge = TouchEventBridge(esp32, enable_default_actions=False)
    events = []
    bridge.add_listener(lambda event: events.append(event))

    await bridge.poll_once(now_s=100.00)
    await bridge.poll_once(now_s=100.50)

    assert esp32.calls == [
        ("self.touch.get_touch_state", {}),
        ("self.touch.get_touch_state", {}),
    ]
    assert [event.event for event in events] == ["tap"]
    assert events[0].age_ms == 50


@pytest.mark.asyncio
async def test_poll_once_emits_repeated_same_gesture_when_age_resets():
    """Two separate taps are emitted when the firmware event age resets."""
    esp32 = FakeESP32(
        [
            {"available": True, "last_event": "tap", "last_event_age_ms": 50},
            {"available": True, "last_event": "tap", "last_event_age_ms": 40},
        ]
    )
    bridge = TouchEventBridge(esp32, enable_default_actions=False)
    events = []
    bridge.add_listener(lambda event: events.append(event))

    await bridge.poll_once(now_s=100.00)
    await bridge.poll_once(now_s=101.00)

    assert [event.event for event in events] == ["tap", "tap"]


@pytest.mark.asyncio
async def test_poll_once_ignores_stale_or_idle_events():
    """Idle and stale touch states should not notify Claude Code."""
    esp32 = FakeESP32(
        [
            {"available": True, "last_event": "idle", "last_event_age_ms": -1},
            {"available": True, "last_event": "stroke", "last_event_age_ms": 5000},
        ]
    )
    bridge = TouchEventBridge(esp32, max_event_age_ms=1500, enable_default_actions=False)
    events = []
    bridge.add_listener(lambda event: events.append(event))

    await bridge.poll_once(now_s=100.00)
    await bridge.poll_once(now_s=100.50)

    assert events == []


@pytest.mark.asyncio
async def test_tap_drops_pause_marker_without_visual_double_handling(tmp_path):
    """Firmware HandleTap already runs happy face + idle revert. The gateway
    must only drop the pause marker (so the hook backs off the head for a
    few seconds) and must NOT send a second set_avatar / set_head_angles —
    that's the "two action layers racing" the official-alignment goal
    explicitly forbids.
    """
    esp32 = FakeESP32([
        {"available": True, "last_event": "tap", "last_event_age_ms": 20},
    ])
    pause_path = tmp_path / "touch-pause-until.json"
    bridge = TouchEventBridge(esp32, action_pause_path=pause_path, action_hold_s=3.0)

    await bridge.poll_once(now_s=100.00)

    assert esp32.calls == [("self.touch.get_touch_state", {})]
    marker = json.loads(pause_path.read_text())
    assert marker["event"] == "tap"
    assert marker["until"] > 0
