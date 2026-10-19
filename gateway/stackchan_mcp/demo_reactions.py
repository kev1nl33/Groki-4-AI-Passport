"""Optional scripted reactions (STACKCHAN_DEMO_REACTIONS).

This is intentionally not a personality architecture. It only turns the two
physical signals that already work (new face, head tap) into immediate
spoken feedback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .touch_bridge import TouchEvent

logger = logging.getLogger(__name__)
VISUAL_TOOL_TIMEOUT_S = 0.08

FACE_LINES = (
    "嗨，又见面啦。",
    "你好呀。",
)
TOUCH_LINES = (
    "嘿，我在呢。",
    "被你发现啦。",
    "有什么事吗？",
)
INTRO_LINE = "我是 Groki，你写代码的时候我就在旁边。"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class DemoReactions:
    """Minimal scripted reactions to a new face and a head tap."""

    esp32: Any
    usb_transport: Any | None = None
    enabled: bool = field(
        default_factory=lambda: _env_bool("STACKCHAN_DEMO_REACTIONS", False)
    )
    face_absent_s: float = field(
        default_factory=lambda: float(os.getenv("STACKCHAN_DEMO_FACE_ABSENT_S", "3.0"))
    )
    face_cooldown_s: float = field(
        default_factory=lambda: float(os.getenv("STACKCHAN_DEMO_FACE_COOLDOWN_S", "20.0"))
    )
    touch_cooldown_s: float = field(
        default_factory=lambda: float(os.getenv("STACKCHAN_DEMO_TOUCH_COOLDOWN_S", "1.0"))
    )
    clock: Callable[[], float] = time.monotonic
    _last_face_seen_s: float | None = None
    _last_face_reaction_s: float | None = None
    _last_touch_reaction_s: float | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _speech_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def maybe_face_entered(self, detection: dict[str, Any]) -> bool:
        """Return True once when a face appears after being absent."""
        if not self.enabled:
            return False
        try:
            confidence = float(detection.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.5:
            return False

        now = self.clock()
        was_absent = (
            self._last_face_seen_s is None
            or now - self._last_face_seen_s >= self.face_absent_s
        )
        cooldown_ok = (
            self._last_face_reaction_s is None
            or now - self._last_face_reaction_s >= self.face_cooldown_s
        )
        self._last_face_seen_s = now
        if not (was_absent and cooldown_ok):
            return False
        self._last_face_reaction_s = now
        return True

    def maybe_head_tap(self, event: TouchEvent) -> bool:
        """Return True for a debounced top/head tap."""
        if not self.enabled or event.event != "tap":
            return False
        # The firmware's three Si12T zones are all on the head, and
        # TouchEventBridge has already debounced the tap. Do not re-invent
        # touch classification here.
        now = self.clock()
        if (
            self._last_touch_reaction_s is not None
            and now - self._last_touch_reaction_s < self.touch_cooldown_s
        ):
            return False
        self._last_touch_reaction_s = now
        return True

    def spawn_face_reaction(self) -> None:
        if not self.enabled:
            return
        self._spawn(self._react_to_face(random.choice(FACE_LINES)))

    def spawn_touch_reaction(self) -> None:
        if not self.enabled:
            return
        self._spawn(self._react_to_touch(random.choice(TOUCH_LINES)))

    def spawn_intro(self) -> None:
        if not self.enabled:
            return
        self._spawn(self._speak(INTRO_LINE, emotion="happy"))

    def arm_face_entry(self) -> None:
        """Clear cooldown, but still require a real absence→presence entry."""
        if not self.enabled:
            return
        self._last_face_reaction_s = None

    async def stop(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro, name="stackchan-demo-reaction")
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("demo reaction failed")

    async def _react_to_face(self, line: str) -> None:
        await self._call_tool("self.display.set_avatar", {"face": "happy"})
        await self._speak(line, emotion="happy")

    async def _react_to_touch(self, line: str) -> None:
        # Immediate visual feedback. Speech may take longer, but the face flip
        # lands through USB when available so the touch feels responsive.
        await self._call_tool("self.display.set_avatar", {"face": "happy"})
        await self._speak(line, emotion="happy")

    async def _speak(self, text: str, *, emotion: str | None = None) -> None:
        async with self._speech_lock:
            if not getattr(self.esp32, "device_connected", False):
                logger.info("demo reaction skipped speech: ESP32 offline")
                return
            result, error = await self.esp32.speak(text, emotion)
            if error:
                logger.warning("demo reaction speech failed: %s", error)
            else:
                logger.info("demo reaction spoke: %s", result)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> None:
        usb = self.usb_transport
        if usb is not None and getattr(usb, "connected", False):
            try:
                await usb.call_tool(name, arguments, timeout_s=VISUAL_TOOL_TIMEOUT_S)
                return
            except Exception as exc:
                logger.debug("demo reaction USB %s failed: %s", name, exc)
        if not getattr(self.esp32, "device_connected", False):
            return
        _result, error = await self.esp32.call_tool(name, arguments)
        if error:
            logger.debug("demo reaction tool %s failed: %s", name, error)
