"""Gemini Live session keepalive scheduler (experimental silent frames).

All-zero PCM does not count as activity under automatic VAD, so it cannot
prevent the idle 1008 disconnect. Real user speech is instead marked with
manual VAD activity_start/activity_end (see gemini_live_bridge and
gemini_voice_proxy); an idle disconnect while DORMANT is recovered by session
resumption and automatic reconnect.

This module stays as an optional experiment: ``STACKCHAN_GEMINI_KEEPALIVE_S``
defaults to 0 (off). Only when it is set to a positive number is silent PCM
sent periodically while DORMANT, and it is not claimed to prevent 1008.

Sleeping and sending are injectable, so unit tests drive the scheduler with a
fake sleep and no real time.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_KEEPALIVE_INTERVAL_S = 90.0
DEFAULT_SILENCE_MS = 200
SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2


def silence_pcm(duration_ms: int = DEFAULT_SILENCE_MS) -> bytes:
    """Generate 16 kHz signed 16-bit mono silent PCM."""
    return b"\x00" * int(SAMPLE_RATE * BYTES_PER_SAMPLE * duration_ms / 1000)


def keepalive_interval_from_env() -> float:
    """Read STACKCHAN_GEMINI_KEEPALIVE_S; unset, 0 or negative turns the keepalive off."""
    raw = os.getenv("STACKCHAN_GEMINI_KEEPALIVE_S")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "invalid STACKCHAN_GEMINI_KEEPALIVE_S=%r; disabling keepalive",
            raw,
        )
        return 0.0


@dataclass
class SessionKeepalive:
    """Periodic silent-frame sender.

    ``should_send`` is evaluated every period: frames go out only while the gate is DORMANT and the session is online.
    A failed send is logged at debug level and the next round goes on; the bridge handles reconnects itself and the keepalive
    takes no part in error recovery.
    """

    send_silence: Callable[[bytes], Awaitable[None]]
    should_send: Callable[[], bool]
    interval_s: float = DEFAULT_KEEPALIVE_INTERVAL_S
    silence_ms: int = DEFAULT_SILENCE_MS
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    on_sent: Callable[[], None] | None = None
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or self.interval_s <= 0:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="gemini-session-keepalive",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        pcm = silence_pcm(self.silence_ms)
        while True:
            await self.sleep(self.interval_s)
            if not self.should_send():
                continue
            try:
                await self.send_silence(pcm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("keepalive silence send failed: %s", exc)
                continue
            if self.on_sent is not None:
                self.on_sent()
