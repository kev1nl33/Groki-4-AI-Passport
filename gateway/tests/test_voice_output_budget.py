# SPDX-License-Identifier: MIT
import asyncio
import pytest
from stackchan_mcp.voice_output_queue import VoiceOutputQueue


@pytest.mark.asyncio
async def test_output_budget_counts_active_bytes_and_recovers_after_cancel():
    output = VoiceOutputQueue()
    output.set_pcm_budget(True)
    gate = asyncio.Event()
    first = output.submit(gate.wait, audio_bytes=output.MAX_PCM_BYTES)
    with pytest.raises(BufferError):
        output.submit(gate.wait, audio_bytes=1)
    await asyncio.sleep(0)
    assert output.pending_audio_bytes == output.MAX_PCM_BYTES
    await output.cancel()
    assert first.done()
    assert output.pending_audio_bytes == 0
    new = output.submit(lambda: asyncio.sleep(0), audio_bytes=1)
    await new
    assert output.pending_audio_bytes == 0


@pytest.mark.asyncio
async def test_output_budget_callback_limit_and_error_release():
    output = VoiceOutputQueue()
    output.set_pcm_budget(True)
    gate = asyncio.Event()
    for _ in range(output.MAX_CALLBACKS):
        output.submit(gate.wait, audio_bytes=1)
    with pytest.raises(BufferError):
        output.submit(gate.wait, audio_bytes=0)
    await output.cancel()
    assert output.pending_audio_bytes == 0
    async def fail():
        raise ValueError('synthetic')
    await output.submit(fail, audio_bytes=10)
    assert output.pending_audio_bytes == 0
