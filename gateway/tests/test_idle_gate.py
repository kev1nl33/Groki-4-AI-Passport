import pytest

from stackchan_mcp.idle_gate import IdleGate


class FakeESP32:
    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}, None


class FakeUSB:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments, *, timeout_s=None):
        self.calls.append((name, arguments))
        return {"ok": True}


def _lock_calls(calls):
    return [call for call in calls if call[0] == "self.robot.set_modify_lock"]


@pytest.mark.asyncio
async def test_idle_gate_unlocks_after_all_quiet_conditions():
    now = 100.0
    esp32 = FakeESP32()
    gate = IdleGate(esp32, quiet_seconds=5.0, clock=lambda: now)

    now = 106.0
    sent = await gate.evaluate_once()

    assert sent is True
    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": False})
    ]


@pytest.mark.asyncio
async def test_idle_gate_face_seen_does_not_count_as_user_activity():
    now = 100.0
    esp32 = FakeESP32()
    gate = IdleGate(esp32, quiet_seconds=5.0, clock=lambda: now)
    now = 106.0
    gate.notify_face_detected()

    await gate.evaluate_once()

    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": False})
    ]


@pytest.mark.asyncio
async def test_idle_gate_auto_head_locks_without_resetting_user_quiet_timer():
    now = 100.0
    esp32 = FakeESP32()
    gate = IdleGate(esp32, quiet_seconds=5.0, auto_head_hold_s=0.75, clock=lambda: now)
    now = 106.0
    await gate.evaluate_once()
    esp32.calls.clear()

    gate.notify_auto_head_command()
    await gate.evaluate_once()

    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": True})
    ]
    esp32.calls.clear()
    now = 106.8
    await gate.evaluate_once()
    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": False})
    ]


@pytest.mark.asyncio
async def test_idle_gate_locks_on_head_command_after_unlock():
    now = 100.0
    esp32 = FakeESP32()
    gate = IdleGate(esp32, quiet_seconds=5.0, clock=lambda: now)
    now = 106.0
    await gate.evaluate_once()
    esp32.calls.clear()

    gate.notify_head_command()
    await gate.evaluate_once()

    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": True})
    ]


@pytest.mark.asyncio
async def test_idle_gate_requires_idle_device_state():
    now = 100.0
    esp32 = FakeESP32()
    gate = IdleGate(esp32, quiet_seconds=5.0, clock=lambda: now)
    now = 106.0
    gate.notify_device_state("speaking")

    await gate.evaluate_once()

    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": True})
    ]


@pytest.mark.asyncio
async def test_idle_gate_waits_after_voice_returns_to_idle():
    now = 100.0
    esp32 = FakeESP32()
    gate = IdleGate(esp32, quiet_seconds=5.0, clock=lambda: now)
    now = 106.0
    await gate.evaluate_once()
    esp32.calls.clear()

    gate.notify_device_state("speaking")
    now = 107.0
    gate.notify_device_state("idle")
    await gate.evaluate_once()

    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": True})
    ]
    esp32.calls.clear()
    now = 112.1
    await gate.evaluate_once()
    assert _lock_calls(esp32.calls) == [
        ("self.robot.set_modify_lock", {"locked": False})
    ]


@pytest.mark.asyncio
async def test_idle_gate_prefers_usb():
    now = 100.0
    esp32 = FakeESP32()
    usb = FakeUSB()
    gate = IdleGate(esp32, usb_transport=usb, quiet_seconds=5.0, clock=lambda: now)
    now = 106.0

    await gate.evaluate_once()

    assert _lock_calls(usb.calls) == [
        ("self.robot.set_modify_lock", {"locked": False})
    ]
    assert esp32.calls == []
