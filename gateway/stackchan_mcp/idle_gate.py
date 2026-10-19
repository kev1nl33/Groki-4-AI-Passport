"""Gateway gate for firmware-side official idle modifiers.

The firmware owns the actual idle motion through M5's IdleMotionModifier.
This gateway only decides when those modifiers are allowed to run.  Any
external interaction locks modifiers immediately; five quiet seconds with no
manual head command, no touch, no voice activity, and an idle voice state
unlocks them again. Automatic head controllers such as face tracking can hold
the lock briefly without resetting the user-interaction quiet timer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any, Callable

logger = logging.getLogger(__name__)


class IdleGate:
    """Drive ``self.robot.set_modify_lock`` from gateway-side activity."""

    USB_TIMEOUT_S = 0.5
    LOCK_TOOL = "self.robot.set_modify_lock"

    def __init__(
        self,
        esp32: Any,
        *,
        usb_transport: Any | None = None,
        quiet_seconds: float = 5.0,
        auto_head_hold_s: float = 0.75,
        poll_interval_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.esp32 = esp32
        self.usb_transport = usb_transport
        self.quiet_seconds = quiet_seconds
        self.auto_head_hold_s = auto_head_hold_s
        self.poll_interval_s = poll_interval_s
        self._clock = clock
        now = self._clock()
        # Start locked until the first full quiet window passes. This keeps
        # boot / camera warm-up from fighting the user's first interaction.
        self._last_face_seen_s = now
        self._last_head_command_s = now
        self._last_touch_s = now
        self._last_voice_activity_s = now
        self._last_auto_head_command_s = 0.0
        self._device_state = "idle"
        self._last_sent_lock: bool | None = None
        self._task: asyncio.Task[None] | None = None
        self._send_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def locked(self) -> bool | None:
        return self._last_sent_lock

    def notify_face_detected(self) -> None:
        """Record presence only; a camera hit is not a user interaction."""
        self._last_face_seen_s = self._clock()

    def notify_auto_head_command(self) -> None:
        """Lock modifiers while an automatic controller owns the head.

        Face tracking can emit many set_head_angles calls per second. Those
        should stop firmware idle modifiers from fighting the tracker, but
        must not reset the "user has been quiet for 5 s" timer.
        """
        self._last_auto_head_command_s = self._clock()
        self._request_lock(True)

    def notify_head_command(self) -> None:
        self._last_head_command_s = self._clock()
        self._request_lock(True)

    def notify_touch(self) -> None:
        self._last_touch_s = self._clock()
        self._request_lock(True)

    def notify_device_state(self, state: str) -> None:
        normalized = (state or "").strip().lower()
        if not normalized:
            return
        now = self._clock()
        self._device_state = normalized
        self._last_voice_activity_s = now
        if normalized != "idle":
            self._request_lock(True)

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="stackchan-idle-gate")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._send_task is not None:
            self._send_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._send_task
            self._send_task = None
        # Fail open on gateway shutdown: if the device is still connected,
        # leave firmware-side idle free to run without the gateway.
        await self._set_modify_lock(False)

    async def evaluate_once(self) -> bool:
        """Evaluate the four idle conditions and send lock if needed."""
        should_unlock = self._should_unlock()
        target_locked = not should_unlock
        return await self._set_modify_lock(target_locked)

    def _should_unlock(self) -> bool:
        now = self._clock()
        quiet = self.quiet_seconds
        return (
            self._device_state == "idle"
            and now - self._last_head_command_s >= quiet
            and now - self._last_touch_s >= quiet
            and now - self._last_voice_activity_s >= quiet
            and now - self._last_auto_head_command_s >= self.auto_head_hold_s
            and self._device_reachable()
        )

    def _device_reachable(self) -> bool:
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            return True
        return getattr(self.esp32, "device_connected", False)

    def _request_lock(self, locked: bool) -> None:
        if self._last_sent_lock is locked:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()
        self._send_task = loop.create_task(
            self._set_modify_lock(locked),
            name="stackchan-idle-gate-send-lock",
        )

    async def _run(self) -> None:
        # Send an initial lock as soon as the gateway comes up; unlock only
        # after evaluate_once sees a full quiet window.
        await self._set_modify_lock(True)
        while True:
            await asyncio.sleep(self.poll_interval_s)
            await self.evaluate_once()

    async def _set_modify_lock(self, locked: bool) -> bool:
        if self._last_sent_lock is locked:
            return True
        args = {"locked": locked}
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            try:
                await usb.call_tool(self.LOCK_TOOL, args, timeout_s=self.USB_TIMEOUT_S)
                self._last_sent_lock = locked
                logger.info("idle gate modify_lock=%s via USB", locked)
                return True
            except Exception as exc:
                logger.debug("idle gate USB set_modify_lock failed: %s", exc)

        if getattr(self.esp32, "device_connected", False):
            try:
                result = await self.esp32.call_tool(self.LOCK_TOOL, args)
            except Exception as exc:
                logger.debug("idle gate WS set_modify_lock failed: %s", exc)
                return False
            error = result[1] if isinstance(result, tuple) and len(result) == 2 else None
            if error:
                logger.debug("idle gate WS set_modify_lock returned error: %s", error)
                return False
            self._last_sent_lock = locked
            logger.info("idle gate modify_lock=%s via WS", locked)
            return True

        return False
