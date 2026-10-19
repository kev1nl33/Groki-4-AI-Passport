"""Regression tests for gemini speak_text frame accounting."""

from __future__ import annotations

import asyncio

import pytest

from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy


@pytest.mark.asyncio
async def test_speak_text_reports_frames_sent_from_device_writes():
    proxy = GeminiVoiceProxy(
        esp32_ref=lambda: None,
        device_tts_start_delay_s=0.0,
        device_frame_interval_s=0.0,
        device_prebuffer_ms=0.0,
        device_frame_pace_ms=0.0,
    )
    proxy._speak_text_active = True
    proxy._speak_text_frames = 0

    async def fake_send(_frame: bytes) -> None:
        return None

    proxy._send_to_device = fake_send
    proxy._send_lock = asyncio.Lock()
    proxy._frame_queue = asyncio.Queue()
    proxy._drain_stop = asyncio.Event()

    await proxy._frame_queue.put(b"\x00" * 10)
    proxy._drain_stop.set()
    await proxy._drain()

    assert proxy._speak_text_frames == 1