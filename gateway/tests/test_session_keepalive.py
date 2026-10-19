"""Tests for the Gemini session keepalive scheduler and the active-drop warning."""

import asyncio

import pytest

from stackchan_mcp.debug_status import DebugStatus
from stackchan_mcp.session_keepalive import (
    SessionKeepalive,
    keepalive_interval_from_env,
    silence_pcm,
)


class StepSleep:
    """Fake clock: each step() releases one sleep period, so the test controls the pace."""

    def __init__(self) -> None:
        self.permits: asyncio.Queue[None] = asyncio.Queue()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self.permits.get()

    async def step(self) -> None:
        self.permits.put_nowait(None)
        # Let the keepalive loop finish sending and go back to sleep.
        for _ in range(5):
            await asyncio.sleep(0)


def test_silence_pcm_is_16khz_int16_zeroes():
    pcm = silence_pcm(200)
    assert len(pcm) == 16_000 * 2 * 200 // 1000  # 6400 bytes
    assert pcm == b"\x00" * len(pcm)


def test_interval_from_env(monkeypatch):
    monkeypatch.delenv("STACKCHAN_GEMINI_KEEPALIVE_S", raising=False)
    assert keepalive_interval_from_env() == 0.0
    monkeypatch.setenv("STACKCHAN_GEMINI_KEEPALIVE_S", "30")
    assert keepalive_interval_from_env() == 30.0
    monkeypatch.setenv("STACKCHAN_GEMINI_KEEPALIVE_S", "0")
    assert keepalive_interval_from_env() == 0.0
    monkeypatch.setenv("STACKCHAN_GEMINI_KEEPALIVE_S", "not-a-number")
    assert keepalive_interval_from_env() == 0.0


@pytest.mark.asyncio
async def test_keepalive_sends_silence_each_interval_when_dormant():
    sleep = StepSleep()
    sent: list[bytes] = []
    st = DebugStatus()

    async def send(pcm: bytes) -> None:
        sent.append(pcm)

    ka = SessionKeepalive(
        send_silence=send,
        should_send=lambda: True,
        interval_s=90.0,
        sleep=sleep,
        on_sent=st.on_keepalive_sent,
    )
    ka.start()
    try:
        await sleep.step()
        await sleep.step()
        assert len(sent) == 2
        assert sent[0] == silence_pcm()
        assert sleep.delays[:2] == [90.0, 90.0]
        assert st.snapshot()["gemini"]["keepalive_count"] == 2
    finally:
        await ka.stop()
    assert ka.running is False


@pytest.mark.asyncio
async def test_keepalive_skips_when_should_send_false():
    sleep = StepSleep()
    sent: list[bytes] = []
    gate_dormant = False

    async def send(pcm: bytes) -> None:
        sent.append(pcm)

    ka = SessionKeepalive(
        send_silence=send,
        should_send=lambda: gate_dormant,
        sleep=sleep,
    )
    ka.start()
    try:
        await sleep.step()
        assert sent == []
        gate_dormant = True
        await sleep.step()
        assert len(sent) == 1
    finally:
        await ka.stop()


@pytest.mark.asyncio
async def test_keepalive_survives_send_failure():
    sleep = StepSleep()
    attempts: list[int] = []
    sent_ok: list[int] = []

    async def flaky_send(_pcm: bytes) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("socket closed")

    ka = SessionKeepalive(
        send_silence=flaky_send,
        should_send=lambda: True,
        sleep=sleep,
        on_sent=lambda: sent_ok.append(1),
    )
    ka.start()
    try:
        await sleep.step()  # first send fails
        assert sent_ok == []
        await sleep.step()  # the loop survives; the second send succeeds
        assert len(attempts) == 2
        assert len(sent_ok) == 1
    finally:
        await ka.stop()


@pytest.mark.asyncio
async def test_keepalive_disabled_by_nonpositive_interval():
    async def send(_pcm: bytes) -> None:
        pass

    ka = SessionKeepalive(send_silence=send, should_send=lambda: True, interval_s=0.0)
    ka.start()
    assert ka.running is False


@pytest.mark.asyncio
async def test_keepalive_start_is_idempotent():
    sleep = StepSleep()

    async def send(_pcm: bytes) -> None:
        pass

    ka = SessionKeepalive(send_silence=send, should_send=lambda: True, sleep=sleep)
    ka.start()
    task = ka._task
    ka.start()
    assert ka._task is task
    await ka.stop()
