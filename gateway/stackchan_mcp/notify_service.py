"""Bounded, loopback-only announcement jobs; device owner grants each lease."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)
NOTIFY_MAX_CHARS = 100
NOTIFY_QUEUE_LIMIT = 3
NOTIFY_TTL_S = 60.0
NOTIFY_MIN_INTERVAL_S = 2.0
NOTIFY_QUIET_WINDOW_S = 1.5
NOTIFY_AWAIT_GRACE_S = 10.0  # compatibility only; never used to infer idle
NOTIFY_LOOP_PERIOD_S = 0.5

@dataclass
class NotifyItem:
    id: int
    text: str
    flash: bool
    expires_at: float
    submitted_at: float

@dataclass
class SubmitResult:
    ok: bool
    status: str
    error: str | None = None
    item_id: int | None = None
    queue_depth: int = 0

    def to_dict(self):
        return dict(ok=self.ok, status=self.status, error=self.error,
                    id=self.item_id, queue_depth=self.queue_depth)

class NotifyService:
    def __init__(self, gateway, status=None, *, clock=time.monotonic,
                 max_chars=100, queue_limit=3, ttl_s=60.0, min_interval_s=2.0,
                 quiet_window_s=1.5, await_grace_s=10.0, max_duration_s=30.0):
        self._gateway = gateway
        self._clock = clock
        self._max_chars = max_chars
        self._queue_limit = queue_limit
        self._ttl_s = ttl_s
        self._min_interval_s = min_interval_s
        self._max_duration_s = min(30.0, max_duration_s)
        self._queue = []
        self._jobs = {}
        self._next_id = 1
        self._last_delivery_at = None
        self._delivering = False
        self._wakeup = None
        self._expired_count = self._failed_count = 0

    @property
    def expired_count(self):
        return self._expired_count

    @property
    def failed_count(self):
        return self._failed_count

    def _connection(self):
        return getattr(getattr(self._gateway, "esp32", None), "connection", None)

    def device_online(self):
        return bool(getattr(getattr(self._gateway, "esp32", None), "device_connected", False)) and self._connection() is not None

    def _proxy(self):
        return getattr(self._connection(), "cloud_proxy", None)

    def session_ready(self):
        proxy = self._proxy()
        return self.device_online() and bool(getattr(proxy, "connected", False)) and callable(getattr(proxy, "speak_text", None))

    def ready(self):
        return self.session_ready() and not self._delivering and (
            self._last_delivery_at is None or self._clock()-self._last_delivery_at >= self._min_interval_s)

    def job(self, item_id):
        return self._jobs.get(item_id)

    def _set_job(self, item, state):
        self._jobs[item.id] = dict(id=item.id, status=state,
                                  ok=state == "done")
        # Bounded metadata only; no text retained after delivery/expiry.
        while len(self._jobs) > 128:
            del self._jobs[next(iter(self._jobs))]

    def submit(self, text, flash=False):
        self._drop_expired(self._clock())
        if not isinstance(text, str) or not text.strip():
            return SubmitResult(False, "", "empty")
        if len(text) > self._max_chars:
            return SubmitResult(False, "", "too_long")
        if not self.device_online():
            return SubmitResult(False, "", "device_offline")
        if not self.session_ready():
            return SubmitResult(False, "", "no_voice_session")
        if len(self._queue) >= self._queue_limit:
            return SubmitResult(False, "", "queue_full")
        now = self._clock()
        item = NotifyItem(self._next_id, text.strip(), bool(flash), now+self._ttl_s, now)
        self._next_id += 1
        self._queue.append(item)
        self._set_job(item, "queued")
        self._wake()
        return SubmitResult(True, "queued", item_id=item.id, queue_depth=len(self._queue))

    def _drop_expired(self, now):
        for item in tuple(self._queue):
            if item.expires_at <= now and self._jobs[item.id]["status"] == "queued":
                self._queue.remove(item)
                self._expired_count += 1
                self._set_job(item, "expired")

    async def _send(self, connection, item, state):
        await connection._ws_send(json.dumps(dict(type="notify", state=state,
                                                  id=item.id, flash=item.flash)))

    async def _ack(self, connection, replies):
        while self._connection() is connection and self.session_ready():
            try:
                return await asyncio.wait_for(replies.get(), .1)
            except asyncio.TimeoutError:
                pass
        raise ConnectionError("device_disconnected")

    async def _cancel(self, connection, proxy, item):
        # Stop real cloud output and its paced tail before releasing the lease.
        try:
            await asyncio.wait_for(proxy.cancel_announcement(item.id), 2)
        finally:
            if self._connection() is connection:
                await asyncio.wait_for(self._send(connection, item, "cancel"), 1)

    async def _deliver(self, item):
        connection, proxy = self._connection(), self._proxy()
        replies = asyncio.Queue()
        connection.notify_replies = (item.id, replies)
        self._delivering = True
        speak = ack = None
        granted = False
        retry = False
        terminal = "failed"
        try:
            await self._send(connection, item, "start")
            state = await asyncio.wait_for(self._ack(connection, replies), 2)
            if state == "busy":
                retry = True
                return  # Device owns the idle decision; retry until TTL.
            if state != "granted":
                terminal = "cancelled"
                return
            granted = True
            self._set_job(item, "playing")
            speak = asyncio.create_task(proxy.speak_text(item.text,
                session_id=f"notify-{item.id}", prompt_audio_frames=[], emotion=None))
            ack = asyncio.create_task(self._ack(connection, replies))
            async with asyncio.timeout(self._max_duration_s):
                done, _ = await asyncio.wait((speak, ack), return_when=asyncio.FIRST_COMPLETED)
                if ack in done:
                    terminal = ack.result()
                    if terminal != "done":
                        await self._cancel(connection, proxy, item)
                        return
                result = await speak
                if not result.get("ok"):
                    # The device sends abort before its ID-tagged cancel ack.
                    # That abort can finish this job's speak task first; retain
                    # its explicit cancellation rather than count it as failure.
                    terminal = "cancelled" if result.get("error") == "cancelled" else "failed"
                    await self._cancel(connection, proxy, item)
                    return
                terminal = await ack  # Actual device PLAY_DONE, never tts.stop.
        except asyncio.CancelledError:
            if granted:
                try:
                    await self._cancel(connection, proxy, item)
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.warning("notify delivery failed id=%d type=%s", item.id, type(exc).__name__)
            if granted:
                try:
                    await self._cancel(connection, proxy, item)
                except Exception as cancel_exc:
                    logger.warning("notify cancel failed id=%d type=%s", item.id, type(cancel_exc).__name__)
        finally:
            for task in (speak, ack):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (speak, ack) if t is not None), return_exceptions=True)
            connection.notify_replies = None
            self._delivering = False
            if not retry:
                if item in self._queue:
                    self._queue.remove(item)
                self._set_job(item, terminal if terminal in {"done", "cancelled"} else "failed")
                self._failed_count += terminal not in {"done", "cancelled"}
                self._last_delivery_at = self._clock()
            self._wake()

    async def tick(self):
        self._drop_expired(self._clock())
        if self._queue and self.ready():
            await self._deliver(self._queue[0])

    def _wake(self):
        if self._wakeup is not None:
            self._wakeup.set()

    async def run(self):
        self._wakeup = asyncio.Event()
        while True:
            # Fixed retry cadence also prevents a busy owner from a hot loop.
            await asyncio.sleep(NOTIFY_LOOP_PERIOD_S)
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("notify tick failed type=%s", type(exc).__name__)
