# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Ordered output callbacks, independent of realtime control-message reception."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class VoiceOutputQueue:
    """Preserve audio/end ordering; cancel the old generation on interruption.

    A response completing normally must drain, rather than cancel, its audio.
    The receiver never awaits this drain. Only interruption or shutdown discards
    pending callbacks, before the sink is reset for the next response.
    """

    MAX_PCM_BYTES = 24000 * 2 * 120  # Gateway RAM, not device RAM: 120 s.
    MAX_CALLBACKS = 4096

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._pcm_budget = False
        self._audio_bytes: dict[asyncio.Task, int] = {}
        self._tail: asyncio.Task[None] | None = None
        self._generation = 0

    def set_pcm_budget(self, enabled: bool) -> None:
        self._pcm_budget = enabled

    @property
    def pending_audio_bytes(self) -> int:
        return sum(self._audio_bytes.values())

    def submit(self, callback: Callable[[], Awaitable[None]], *,
               audio_bytes: int = 0) -> asyncio.Task[None]:
        if self._pcm_budget and (
            len(self._tasks) >= self.MAX_CALLBACKS or
            self.pending_audio_bytes + audio_bytes > self.MAX_PCM_BYTES
        ):
            raise BufferError("PCM response exceeds gateway output budget")
        previous = self._tail
        generation = self._generation

        async def run() -> None:
            try:
                if previous is not None:
                    # Shield prevents recursively cancelling an older callback.
                    await asyncio.shield(previous)
                if generation == self._generation:
                    try:
                        await callback()
                    except Exception:
                        logger.warning("Voice output callback failed (details redacted)")
            finally:
                self._audio_bytes.pop(asyncio.current_task(), None)

        task = asyncio.create_task(run(), name="voice-output")
        self._tasks.add(task)
        self._audio_bytes[task] = audio_bytes
        self._tail = task
        def release(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            self._audio_bytes.pop(done, None)  # Also covers cancellation before start.
        task.add_done_callback(release)
        return task

    @property
    def busy(self) -> bool:
        return self._tail is not None and not self._tail.done()

    async def cancel(self) -> None:
        self._generation += 1
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        self._tail = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
