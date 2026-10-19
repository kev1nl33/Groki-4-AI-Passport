"""Edge TTS downlink for Gemini TEXT-mode replies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .opus_codec import StackchanOpusCodec

logger = logging.getLogger(__name__)

SendToDevice = Callable[[str | bytes], Awaitable[None]]
StateCallback = Callable[[str], None]


class EdgeTTSProvider:
    """Speak text with Microsoft Edge TTS and stream Opus to StackChan.

    The Python ``edge-tts`` package streams MP3 from the service.  We pipe
    that stream through ffmpeg to produce 24 kHz mono signed-16-bit PCM, then
    reuse ``StackchanOpusCodec`` so the ESP32 receives the same 24 kHz / 60 ms
    Opus frames the existing Gemini downlink path used.
    """

    DEFAULT_VOICE = "zh-CN-XiaoyiNeural"

    def __init__(
        self,
        *,
        codec: StackchanOpusCodec,
        send_to_device: SendToDevice,
        voice: str | None = None,
        send_lock: asyncio.Lock | None = None,
        tts_start_delay_s: float = 0.08,
        frame_interval_s: float = 0.0,
        on_device_state: StateCallback | None = None,
        ffmpeg_bin: str | None = None,
    ) -> None:
        self.codec = codec
        self.send_to_device = send_to_device
        self.voice = voice or os.getenv("STACKCHAN_TTS_VOICE") or self.DEFAULT_VOICE
        self.send_lock = send_lock or asyncio.Lock()
        self.tts_start_delay_s = tts_start_delay_s
        self.frame_interval_s = frame_interval_s
        self.on_device_state = on_device_state
        self.ffmpeg_bin = ffmpeg_bin or os.getenv("STACKCHAN_FFMPEG_BIN", "ffmpeg")
        self._tts_active = False

    @property
    def speaking(self) -> bool:
        return self._tts_active

    async def speak_text(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty text", "frames_sent": 0}

        frames_sent = 0
        async with self.send_lock:
            try:
                async for pcm in self._iter_pcm_24k(text):
                    frames = self.codec.encode_pcm_24k(pcm)
                    if not frames:
                        continue
                    if not self._tts_active:
                        await self._begin_tts(text)
                    for frame in frames:
                        await self.send_to_device(frame)
                        frames_sent += 1
                        if self.frame_interval_s > 0:
                            await asyncio.sleep(self.frame_interval_s)

                tail = self.codec.flush()
                if tail and not self._tts_active:
                    await self._begin_tts(text)
                for frame in tail:
                    await self.send_to_device(frame)
                    frames_sent += 1
                    if self.frame_interval_s > 0:
                        await asyncio.sleep(self.frame_interval_s)
            finally:
                if self._tts_active:
                    await self._stop_tts()

        if frames_sent == 0:
            return {
                "ok": False,
                "error": "edge-tts produced no audio frames",
                "frames_sent": 0,
                "voice": self.voice,
            }
        logger.info("edge-tts spoke %d frames with voice=%s", frames_sent, self.voice)
        return {"ok": True, "frames_sent": frames_sent, "voice": self.voice}

    async def abort(self) -> None:
        self.codec.reset_encode_buffer()
        if self._tts_active:
            await self._stop_tts()

    async def _begin_tts(self, text: str) -> None:
        await self.send_to_device(json.dumps({"type": "tts", "state": "start"}))
        await self.send_to_device(
            json.dumps({"type": "tts", "state": "sentence_start", "text": text}, ensure_ascii=False)
        )
        self._tts_active = True
        if self.on_device_state is not None:
            self.on_device_state("speaking")
        if self.tts_start_delay_s > 0:
            await asyncio.sleep(self.tts_start_delay_s)

    async def _stop_tts(self) -> None:
        try:
            await self.send_to_device(json.dumps({"type": "tts", "state": "stop"}))
        finally:
            self._tts_active = False
            if self.on_device_state is not None:
                self.on_device_state("idle")

    async def _iter_pcm_24k(self, text: str) -> AsyncIterator[bytes]:
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mp3",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        writer_error: Exception | None = None

        async def feed_mp3() -> None:
            nonlocal writer_error
            assert proc.stdin is not None
            try:
                async for data in self._iter_edge_mp3(text):
                    proc.stdin.write(data)
                    await proc.stdin.drain()
            except Exception as exc:  # pragma: no cover - defensive subprocess path
                writer_error = exc
            finally:
                with suppress(BrokenPipeError, ConnectionResetError):
                    proc.stdin.close()
                    await proc.stdin.wait_closed()

        writer = asyncio.create_task(feed_mp3(), name="edge-tts-feed-ffmpeg")
        try:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
            await writer
            rc = await proc.wait()
            if writer_error is not None:
                raise RuntimeError(f"edge-tts stream failed: {writer_error}") from writer_error
            if rc != 0:
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                raise RuntimeError(
                    "ffmpeg failed while converting edge-tts MP3 to PCM: "
                    + stderr.decode("utf-8", errors="replace")[:300]
                )
        finally:
            if not writer.done():
                writer.cancel()
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:  # pragma: no cover - defensive cleanup
                    proc.kill()
                    await proc.wait()

    async def _iter_edge_mp3(self, text: str) -> AsyncIterator[bytes]:
        try:
            import edge_tts  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("edge-tts is not installed; run `pip install edge-tts`") from exc

        communicate = edge_tts.Communicate(text, self.voice)
        async for chunk in communicate.stream():
            if chunk.get("type") != "audio":
                continue
            data = chunk.get("data")
            if data:
                yield bytes(data)
