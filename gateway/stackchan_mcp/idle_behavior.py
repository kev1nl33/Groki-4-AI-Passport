"""Gateway-owned idle head motion for StackChan."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


class IdleBehavior:
    """Move the robot subtly while it is idle.

    The hook-side idle worker can run in a different process, so the gateway owns
    this behavior and exposes a small activity hook that pauses motion after MCP,
    hook, touch, and speak commands.
    """

    def __init__(
        self,
        esp32: Any,
        *,
        min_interval_s: float | None = None,
        max_interval_s: float | None = None,
        pause_seconds: float | None = None,
        rng: random.Random | None = None,
        usb_transport: Any | None = None,
    ) -> None:
        self.esp32 = esp32
        self.min_interval_s = min_interval_s if min_interval_s is not None else float(os.getenv("STACKCHAN_IDLE_MIN_S", "4"))
        self.max_interval_s = max_interval_s if max_interval_s is not None else float(os.getenv("STACKCHAN_IDLE_MAX_S", "8"))
        self.pause_seconds = pause_seconds if pause_seconds is not None else float(os.getenv("STACKCHAN_IDLE_PAUSE_S", "3"))
        # Default 0: the firmware's IdleMotionModifier (ported from M5's official one) owns idle motion.
        # The gateway-side idle is kept as a fallback / debug aid and is off by default, so two sources
        # never send set_head_angles and fight over the spring animation. Set STACKCHAN_GATEWAY_IDLE_ENABLED=1 to use it.
        self.enabled = os.getenv("STACKCHAN_GATEWAY_IDLE_ENABLED", "0").lower() not in {"0", "false", "no", "off"}
        self._rng = rng or random.Random()
        self._task: asyncio.Task[None] | None = None
        self._paused_until = 0.0
        # Same handoff as TrackingBridge: when wired, idle head writes go
        # through USB at ~10 ms instead of ~240 ms over WS.
        self.usb_transport = usb_transport
        self._defer_until = 0.0
        self._last_yaw = 0.0
        self._last_pitch = 15.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def notify_activity(self) -> None:
        """Pause idle motion after an explicit command or speech event."""
        self._paused_until = max(self._paused_until, time.monotonic() + self.pause_seconds)

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._task = asyncio.create_task(self._run(), name="stackchan-idle-behavior")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._next_interval())
            while True:
                if self._is_paused() or not self._device_reachable():
                    break
                defer_s = self._defer_until - time.monotonic()
                if defer_s > 0:
                    await asyncio.sleep(defer_s)
                    continue
                try:
                    sent = await self.step_once()
                except Exception:
                    logger.exception("idle step_once failed; will retry next interval")
                    break
                if not sent and self._defer_until > time.monotonic():
                    continue
                break

    def _next_interval(self) -> float:
        low = min(self.min_interval_s, self.max_interval_s)
        high = max(self.min_interval_s, self.max_interval_s)
        return self._rng.uniform(low, high)

    def _is_paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def _device_reachable(self) -> bool:
        """USB or WS up = device can take a head command."""
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            return True
        return getattr(self.esp32, "device_connected", False)

    async def step_once(self) -> bool:
        """Run one official-like idle step. Returns True when a command was sent."""
        if self._is_paused() or not self._device_reachable():
            return False
        if await self._head_is_moving():
            # Official IdleMotionModifier delays 500 ms instead of cutting off
            # the current move. Keep that behavior even though idle lives in
            # the gateway in our architecture.
            self._defer_until = time.monotonic() + 0.5
            logger.debug("idle head motion deferred: head is still moving")
            return False

        args = self._choose_idle_motion()
        await self._send_head_angles(args)
        self._last_yaw = float(args["yaw"])
        self._last_pitch = float(args["pitch"])
        logger.debug(
            "idle head motion action=%s yaw=%s pitch=%s speed=%s",
            args.pop("_action", "unknown"),
            args["yaw"],
            args["pitch"],
            args["speed"],
        )
        return True

    def _choose_idle_motion(self) -> dict[str, Any]:
        """Mirror official IdleMotionModifier action mix in degree units."""
        action = self._rng.randrange(100)
        if action < 50:
            # 50%: look around. M5's official idle_motion.h::perform_idle_motion calls
            # motion.lookAtNormalized(target_x, target_y, speed), which maps -1..1
            # linearly onto the servo limits. Here yaw is limited to ±90° and pitch to 0..60°,
            # so yaw = x × 90 and pitch maps y in [-1,1] onto [0,60].
            # A larger factor gets clamped at the limits, so the head sits at the edges
            # instead of the small look-around the official motion has.
            target_x = self._rng.uniform(-0.4, 0.4)
            target_y = self._rng.uniform(-0.95, 0.2)
            yaw = round(target_x * 90)
            pitch = round(((target_y + 1.0) / 2.0) * 60)
            speed = self._rng.randint(150, 300)
            label = "look_around"
        elif action < 80:
            # 30%: small glance around the current pose (official diff: yaw ±15°, pitch ±8°)
            yaw = round(self._last_yaw + self._rng.uniform(-15, 15))
            pitch = round(self._last_pitch + self._rng.uniform(-8, 8))
            speed = self._rng.randint(100, 250)
            label = "observe"
        elif action < 90:
            # 10%: quick glance
            yaw = self._rng.randint(-50, 50)
            pitch = self._rng.randint(10, 40)
            speed = self._rng.randint(250, 400)
            label = "glance"
        else:
            # 10%: back to centre
            yaw = 0
            pitch = self._rng.randint(5, 40)
            speed = self._rng.randint(100, 300)
            label = "home"

        return {
            "yaw": max(-90, min(90, int(yaw))),
            "pitch": max(0, min(60, int(pitch))),
            "speed": max(100, min(1000, int(speed))),
            "_action": label,
        }

    async def _head_is_moving(self) -> bool:
        status = await self._read_head_status()
        if not isinstance(status, dict):
            return False
        try:
            self._last_yaw = float(status.get("yaw", self._last_yaw))
            self._last_pitch = float(status.get("pitch", self._last_pitch))
        except (TypeError, ValueError):
            pass
        return bool(
            status.get("moving")
            or status.get("yaw_moving")
            or status.get("pitch_moving")
        )

    async def _read_head_status(self) -> dict[str, Any] | None:
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            try:
                result = await usb.call_tool(
                    "self.robot.get_head_angles",
                    {},
                    timeout_s=0.5,
                )
                status = self._extract_head_status(result)
                if status is not None:
                    return status
            except Exception as exc:
                logger.debug("idle USB get_head_angles failed: %s", exc)

        if getattr(self.esp32, "device_connected", False):
            try:
                result = await self.esp32.call_tool("self.robot.get_head_angles", {})
                if isinstance(result, tuple) and len(result) == 2:
                    payload, error = result
                    if error:
                        return None
                    return self._extract_head_status(payload)
                return self._extract_head_status(result)
            except Exception as exc:
                logger.debug("idle WS get_head_angles failed: %s", exc)
        return None

    @classmethod
    def _extract_head_status(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if any(key in value for key in ("yaw", "pitch", "moving", "yaw_moving", "pitch_moving")):
                return value
            if "result" in value:
                parsed = cls._extract_head_status(value["result"])
                if parsed is not None:
                    return parsed
            content = value.get("content")
            if isinstance(content, list):
                for item in content:
                    parsed = cls._extract_head_status(item)
                    if parsed is not None:
                        return parsed
            text = value.get("text")
            if isinstance(text, str):
                return cls._extract_head_status(text)
        if isinstance(value, str):
            try:
                return cls._extract_head_status(json.loads(value))
            except Exception:
                return None
        return None

    async def _send_head_angles(self, args: dict[str, Any]) -> None:
        """USB-first head write, fallback to WS esp32.call_tool."""
        args = {key: value for key, value in args.items() if not key.startswith("_")}
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            try:
                await usb.call_tool(
                    "self.robot.set_head_angles",
                    args,
                    timeout_s=0.5,
                )
                return
            except Exception as exc:
                logger.warning(
                    "idle USB set_head_angles failed (%s); falling back to WS",
                    exc,
                )
        await self.esp32.call_tool("self.robot.set_head_angles", args)
