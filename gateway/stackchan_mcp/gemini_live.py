"""End-to-end voice entry point for StackChan + Gemini Live.

Run:

    GEMINI_API_KEY=... python -m stackchan_mcp.gemini_live

The CLI starts the gateway (USB + WS), opens a Gemini Live session, captures
audio from the Mac's default microphone (no DJI Mic required), and plays
Gemini's audio response through the Mac speakers. Function calls from Gemini
(move_head / set_avatar / set_all_leds) dispatch through the same USB path
TrackingBridge uses, so the head turns or the face flips at ~10 ms latency.

Why Mac speakers and not the StackChan speaker:

  • Gemini Live emits 24 kHz mono PCM. Routing into the device speaker would
    need on-the-fly Opus encoding + 24→16 kHz resample because the firmware
    decoder runs at 16 kHz on protocol v1. That is feasible (see
    stackchan_mcp/tts/orchestrator.py for the existing TTS push path) but
    introduces non-trivial latency and a sync mismatch with the StackChan
    voice link. For the first end-to-end demo we play through the Mac
    speakers so the round-trip stays at ~1 s and the script has no extra
    runtime dependencies. Routing to the device speaker is a tracked
    follow-up (--speaker stackchan would call into a stream-PCM helper).

Stop with Ctrl-C; the gateway shuts down cleanly so the next launch finds
the USB port free.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import queue
import signal
import sys
from typing import Any

from .gateway import get_gateway
from .gemini_live_bridge import (
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    GeminiLiveBridge,
)

logger = logging.getLogger(__name__)


# Mic capture: Gemini Live expects 16 kHz mono signed-16 PCM.
INPUT_SAMPLE_RATE = 16_000
INPUT_CHANNELS = 1
INPUT_DTYPE = "int16"
INPUT_BLOCK_MS = 100  # 100 ms chunks keep responsiveness while batching writes
INPUT_BLOCKSIZE = INPUT_SAMPLE_RATE * INPUT_BLOCK_MS // 1000  # 1600 samples

# Speaker playback: Gemini emits 24 kHz mono PCM.
OUTPUT_SAMPLE_RATE = 24_000
OUTPUT_CHANNELS = 1
OUTPUT_DTYPE = "int16"


class MicrophoneCapture:
    """Pull 16 kHz PCM from the default Mac input device and feed Gemini Live."""

    def __init__(self, bridge: GeminiLiveBridge) -> None:
        self._bridge = bridge
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._stream: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            logger.debug("mic status: %s", status)
        try:
            self._queue.put_nowait(bytes(indata))
        except queue.Full:
            # Drop on overflow rather than block in the audio thread.
            logger.warning("mic queue full; dropping a frame")

    async def start(self) -> None:
        import sounddevice as sd  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._stream = sd.RawInputStream(
            samplerate=INPUT_SAMPLE_RATE,
            blocksize=INPUT_BLOCKSIZE,
            dtype=INPUT_DTYPE,
            channels=INPUT_CHANNELS,
            callback=self._callback,
        )
        self._stream.start()
        self._task = asyncio.create_task(self._pump(), name="mic-pump")

    async def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                pcm = await asyncio.to_thread(self._queue.get, True, 0.2)
            except queue.Empty:
                continue
            if pcm is None:
                break
            try:
                await self._bridge.send_audio(pcm)
            except Exception:
                logger.exception("send_audio failed")

    async def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("mic stream close failed")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


class SpeakerPlayback:
    """Push Gemini's 24 kHz PCM into the default Mac output device."""

    def __init__(self) -> None:
        self._stream: Any | None = None

    async def start(self) -> None:
        import sounddevice as sd  # noqa: PLC0415

        self._stream = sd.RawOutputStream(
            samplerate=OUTPUT_SAMPLE_RATE,
            dtype=OUTPUT_DTYPE,
            channels=OUTPUT_CHANNELS,
        )
        self._stream.start()

    async def feed(self, pcm: bytes) -> None:
        if self._stream is None or not pcm:
            return
        # sounddevice .write is blocking but cheap; run in a thread so the
        # asyncio loop keeps draining function calls and other audio chunks.
        await asyncio.to_thread(self._stream.write, pcm)

    async def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("speaker stream close failed")
            self._stream = None


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print(
            "ERROR: 设置 GEMINI_API_KEY 或 GOOGLE_API_KEY 环境变量后再跑。",
            file=sys.stderr,
        )
        return 2

    gateway = get_gateway()
    await gateway.start()

    speaker = SpeakerPlayback()
    await speaker.start()

    async def snap() -> None:
        try:
            await gateway.tracking_bridge.snap_to_last_position()
        except Exception:
            logger.exception("snap_to_last_position failed")

    bridge = GeminiLiveBridge(
        gateway.esp32,
        api_key=api_key,
        model=args.model,
        voice=args.voice,
        response_modality="AUDIO",
        on_audio=speaker.feed,
        snap_to_face=snap if args.snap_on_wake else None,
        usb_transport=gateway.usb_transport,
    )
    await bridge.start()
    # Give the session a couple of seconds to handshake before declaring ready.
    await bridge.wait_connected(timeout=10.0)
    if not bridge.running:
        print("ERROR: Gemini Live session did not start; see logs.", file=sys.stderr)
        await gateway.stop()
        return 3

    mic = MicrophoneCapture(bridge)
    await mic.start()

    print()
    print("=" * 60)
    print("StackChan + Gemini Live 已就绪。对着 Mac 说话，机器人会回应。")
    print(f"模型: {args.model}    声音: {args.voice}")
    print("function call (move_head/set_avatar/set_all_leds) 走 USB 通道，")
    print("语音回应从 Mac 喇叭出（v1 设计——v2 走机器人喇叭）。")
    print("Ctrl-C 退出。")
    print("=" * 60)
    print(flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Some platforms don't support signal handlers; Ctrl-C still
            # raises KeyboardInterrupt which the outer except catches.
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[stackchan-gemini-live] 关闭中...", flush=True)
        await mic.stop()
        await bridge.stop()
        await speaker.stop()
        await gateway.stop()

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stackchan-gemini-live",
        description=(
            "对着机器人说话，Gemini Live 处理语音并回应；同时控制机器人硬件。"
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini Live model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"Gemini prebuilt voice (default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--no-snap-on-wake",
        dest="snap_on_wake",
        action="store_false",
        help="启动时不要 snap 到最后人脸位置（默认会 snap）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
