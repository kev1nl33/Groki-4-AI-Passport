"""Opus audio frame forwarding for the StackChan voice proxy.

The gateway does not decode Opus here.  It forwards binary frames between the
ESP32 and the xiaozhi cloud WebSocket while keeping the local MCP channel alive.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import aiohttp


if TYPE_CHECKING:
    from .esp32_client import ESP32Manager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFrameInfo:
    """Metadata known for an Opus frame travelling through the gateway."""

    session_id: str
    direction: str
    size: int


@dataclass(frozen=True)
class SynthesizedSpeech:
    """A completed speech synthesis result ready for ESP32 playback."""

    opus_frames: list[bytes]
    sample_rate: int = 24000
    frame_duration_ms: int = 60
    provider: str = "unknown"


@dataclass(frozen=True)
class SpeakResult:
    """Summary returned to MCP clients after a speak request."""

    ok: bool
    text: str
    emotion: str | None
    frames_sent: int
    provider: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "text": self.text,
            "emotion": self.emotion,
            "frames_sent": self.frames_sent,
            "provider": self.provider,
        }
        if self.error:
            result["error"] = self.error
        return result


async def handle_audio_frame(
    data: bytes,
    session_id: str,
    *,
    cloud_proxy: Any | None = None,
    pcm: bool = False,
) -> bool:
    """Forward an incoming ESP32 audio frame to the cloud proxy.

    ``pcm=True`` is used only when the device negotiated the Stage C direct
    PCM format; the proxy then forwards the bytes without Opus decoding.
    Returns True when the frame was accepted by the cloud WebSocket.  When the
    cloud side is absent or disconnected, the frame is dropped deliberately so
    local MCP remains responsive.
    """
    if cloud_proxy is None:
        logger.debug("audio_frame session=%s bytes=%d dropped: no cloud proxy", session_id, len(data))
        return False
    if pcm:
        forwarded = await cloud_proxy.send_device_binary(data, pcm=True)
    else:
        forwarded = await cloud_proxy.send_device_binary(data)
    if not forwarded:
        logger.debug("audio_frame session=%s bytes=%d dropped: cloud not connected", session_id, len(data))
    return forwarded


async def forward_cloud_audio_frame(data: bytes, send_to_device: Any) -> bool:
    """Forward a cloud Opus frame to the ESP32 WebSocket."""
    await send_to_device(data)
    return True


async def send_audio_frame(data: bytes) -> bytes:
    """Prepare an outgoing Opus frame.

    Frames are already Opus-encoded and pass through unchanged.  A TTS engine can replace
    this with an encoder or packet wrapper without touching callers.
    """
    return data




async def push_opus_frames(
    esp32: "ESP32Manager",
    frames: Iterable[bytes],
) -> int:
    """Push Opus frames to the connected ESP32.

    Returns the number of frames sent so the caller can report this to the MCP
    client. Raises :class:`ConnectionError` if the device disconnects mid-stream.
    """
    sent = 0
    for frame in frames:
        await esp32.send_audio_frame(frame)
        sent += 1
    return sent


async def speak_text(
    send_to_device: Any,
    text: str,
    *,
    emotion: str | None = None,
    set_avatar: Any | None = None,
    cloud_proxy: Any | None = None,
    session_id: str = "",
    require_cloud: bool = False,
) -> SpeakResult:
    """Synthesize text and stream it to ESP32 using the xiaozhi TTS protocol."""
    normalized_text = text.strip()
    if not normalized_text:
        return SpeakResult(
            ok=False,
            text=text,
            emotion=emotion,
            frames_sent=0,
            provider="none",
            error="text is empty",
        )

    if require_cloud and cloud_proxy is not None and getattr(cloud_proxy, "connected", False):
        try:
            prompt_audio = await synthesize_cloud_prompt(normalized_text)
            result = await cloud_proxy.speak_text(
                normalized_text,
                session_id=session_id,
                prompt_audio_frames=prompt_audio.opus_frames,
                emotion=emotion,
            )
        except Exception as exc:
            logger.warning("xiaozhi cloud speak failed: %s", exc)
            result = {
                "ok": False,
                "frames_sent": 0,
                "provider": "xiaozhi_cloud",
                "error": str(exc),
            }
        return SpeakResult(
            ok=bool(result.get("ok")),
            text=normalized_text,
            emotion=emotion,
            frames_sent=int(result.get("frames_sent", 0)),
            provider=str(result.get("provider", "xiaozhi_cloud")),
            error=result.get("error"),
        )

    if require_cloud:
        return SpeakResult(
            ok=False,
            text=normalized_text,
            emotion=emotion,
            frames_sent=0,
            provider="xiaozhi_cloud",
            error="xiaozhi cloud TTS is unavailable",
        )

    try:
        speech = await synthesize_speech(normalized_text, emotion=emotion)
    except Exception as exc:
        logger.warning("speak synthesis failed: %s", exc)
        return SpeakResult(
            ok=False,
            text=normalized_text,
            emotion=emotion,
            frames_sent=0,
            provider=_selected_provider_name(),
            error=str(exc),
        )

    if not speech.opus_frames:
        return SpeakResult(
            ok=False,
            text=normalized_text,
            emotion=emotion,
            frames_sent=0,
            provider=speech.provider,
            error="speech engine returned no opus frames",
        )

    started = False
    try:
        if emotion and set_avatar is not None:
            try:
                await set_avatar(emotion)
            except Exception as exc:
                logger.warning("set_avatar(%s) failed, falling back to llm emotion: %s", emotion, exc)
                await send_to_device(json.dumps({"type": "llm", "emotion": emotion}, ensure_ascii=False))
        elif emotion:
            await send_to_device(json.dumps({"type": "llm", "emotion": emotion}, ensure_ascii=False))
        await send_to_device(json.dumps({"type": "tts", "state": "start"}, ensure_ascii=False))
        started = True
        await send_to_device(
            json.dumps(
                {"type": "tts", "state": "sentence_start", "text": normalized_text},
                ensure_ascii=False,
            )
        )
        for frame in speech.opus_frames:
            await send_to_device(await send_audio_frame(frame))
            await asyncio.sleep(0.04)
        await send_to_device(json.dumps({"type": "tts", "state": "stop"}, ensure_ascii=False))
    except Exception as exc:
        if started:
            try:
                await send_to_device(json.dumps({"type": "tts", "state": "stop"}, ensure_ascii=False))
            except Exception:
                pass
        return SpeakResult(
            ok=False,
            text=normalized_text,
            emotion=emotion,
            frames_sent=0,
            provider=speech.provider,
            error=f"failed to stream speech to device: {exc}",
        )

    return SpeakResult(
        ok=True,
        text=normalized_text,
        emotion=emotion,
        frames_sent=len(speech.opus_frames),
        provider=speech.provider,
    )


async def synthesize_speech(text: str, *, emotion: str | None = None) -> SynthesizedSpeech:
    """Run the configured pluggable voice engine and return raw Opus frames.

    Provider selection:
    - STACKCHAN_TTS_URL: POST to a voice-model HTTP endpoint.
    - STACKCHAN_TTS_COMMAND: run a local command that writes Ogg Opus or JSON.
    - macOS fallback: use `say` + `ffmpeg` when both are installed.
    """
    if os.getenv("STACKCHAN_TTS_URL"):
        return await _synthesize_http(text, emotion=emotion)
    if os.getenv("STACKCHAN_TTS_COMMAND"):
        return await _synthesize_command(text, emotion=emotion)
    if _macos_say_available():
        return await _synthesize_macos_say(text)
    raise RuntimeError(
        "No speech engine configured. Set STACKCHAN_TTS_URL or STACKCHAN_TTS_COMMAND."
    )


DEFAULT_CLOUD_TTS_PROMPT_TEMPLATE = "Repeat after me: {text}. Say only: {text}."


async def synthesize_cloud_prompt(text: str) -> SynthesizedSpeech:
    """Encode a short spoken prompt that asks xiaozhi cloud to speak the text.

    Tenclass xiaozhi cloud accepts user audio over the WebSocket and returns the
    configured assistant TTS voice.  It rejects long text in `listen/detect` and
    does not expose a reliable direct text-to-TTS command on the device socket,
    so the gateway feeds a concise prompt as upstream Opus audio and forwards
    only the cloud TTS audio back to StackChan.

    XIAOZHI_CLOUD_TTS_PROMPT_TEMPLATE overrides the prompt; ``{text}`` is
    replaced by the text as is. For a Chinese-speaking cloud assistant, for
    example: ``跟我说：{text}。只说这句：{text}``.
    """
    template = os.getenv("XIAOZHI_CLOUD_TTS_PROMPT_TEMPLATE", DEFAULT_CLOUD_TTS_PROMPT_TEMPLATE)
    prompt = template.format(text=text)
    return await _synthesize_macos_say(prompt, sample_rate=16000)


def _selected_provider_name() -> str:
    if os.getenv("STACKCHAN_TTS_URL"):
        return "http"
    if os.getenv("STACKCHAN_TTS_COMMAND"):
        return "command"
    if _macos_say_available():
        return "macos_say"
    return "none"


async def _synthesize_http(text: str, *, emotion: str | None = None) -> SynthesizedSpeech:
    url = os.environ["STACKCHAN_TTS_URL"]
    headers: dict[str, str] = {}
    token = os.getenv("STACKCHAN_TTS_TOKEN")
    if token:
        headers["Authorization"] = token if " " in token else f"Bearer {token}"
    timeout = aiohttp.ClientTimeout(total=float(os.getenv("STACKCHAN_TTS_TIMEOUT_SECONDS", "20")))
    payload = {
        "text": text,
        "emotion": emotion,
        "format": "opus",
        "sample_rate": 24000,
        "frame_duration_ms": 60,
    }
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as response:
            body = await response.read()
            if response.status >= 400:
                raise RuntimeError(f"TTS HTTP {response.status}: {body[:200].decode(errors='replace')}")
            content_type = response.headers.get("Content-Type", "")
            frames = _frames_from_voice_response(body, content_type)
            return SynthesizedSpeech(opus_frames=frames, provider="http")


async def _synthesize_command(text: str, *, emotion: str | None = None) -> SynthesizedSpeech:
    env = {
        **os.environ,
        "STACKCHAN_TTS_TEXT": text,
        "STACKCHAN_TTS_EMOTION": emotion or "",
        "STACKCHAN_TTS_SAMPLE_RATE": "24000",
        "STACKCHAN_TTS_FRAME_MS": "60",
    }
    proc = await asyncio.create_subprocess_shell(
        os.environ["STACKCHAN_TTS_COMMAND"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(text.encode("utf-8")),
        timeout=float(os.getenv("STACKCHAN_TTS_TIMEOUT_SECONDS", "20")),
    )
    if proc.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip() or f"TTS command exited {proc.returncode}")
    frames = _frames_from_voice_response(stdout, os.getenv("STACKCHAN_TTS_COMMAND_FORMAT", "audio/ogg"))
    return SynthesizedSpeech(opus_frames=frames, provider="command")


async def _synthesize_macos_say(text: str, *, sample_rate: int = 24000) -> SynthesizedSpeech:
    with tempfile.TemporaryDirectory(prefix="stackchan-tts-") as tmp:
        tmp_path = Path(tmp)
        aiff_path = tmp_path / "speech.aiff"
        say = shutil.which("say")
        ffmpeg = shutil.which("ffmpeg")
        if not say or not ffmpeg:
            raise RuntimeError("macOS say/ffmpeg fallback is unavailable")
        say_proc = await asyncio.create_subprocess_exec(
            say,
            "-o",
            str(aiff_path),
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _say_out, say_err = await say_proc.communicate()
        if say_proc.returncode:
            raise RuntimeError(say_err.decode(errors="replace").strip() or "say failed")

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(aiff_path),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-application",
            "voip",
            "-frame_duration",
            "60",
            "-b:a",
            "32000",
            "-vbr",
            "off",
            "-f",
            "opus",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ogg_bytes, ffmpeg_err = await ffmpeg_proc.communicate()
        if ffmpeg_proc.returncode:
            raise RuntimeError(ffmpeg_err.decode(errors="replace").strip() or "ffmpeg failed")
    return SynthesizedSpeech(
        opus_frames=_opus_packets_from_ogg(ogg_bytes),
        sample_rate=sample_rate,
        provider="macos_say",
    )


def _macos_say_available() -> bool:
    return bool(shutil.which("say") and shutil.which("ffmpeg"))


def _frames_from_voice_response(body: bytes, content_type: str) -> list[bytes]:
    if not body:
        return []
    if "json" in content_type.lower():
        parsed = json.loads(body.decode("utf-8"))
        frames_b64 = parsed.get("opus_frames") or parsed.get("frames") or []
        return [base64.b64decode(frame) for frame in frames_b64]
    if body.startswith(b"OggS"):
        return _opus_packets_from_ogg(body)
    if os.getenv("STACKCHAN_TTS_RESPONSE_FORMAT") == "raw_opus_frame":
        return [body]
    raise RuntimeError("TTS response must be Ogg Opus, JSON opus_frames, or raw_opus_frame")


def _opus_packets_from_ogg(data: bytes) -> list[bytes]:
    """Extract raw Opus packets from an Ogg Opus byte stream."""
    packets: list[bytes] = []
    packet = bytearray()
    pos = 0
    while pos < len(data):
        if data[pos : pos + 4] != b"OggS":
            raise RuntimeError("invalid Ogg Opus stream")
        if pos + 27 > len(data):
            raise RuntimeError("truncated Ogg page header")
        segments_count = data[pos + 26]
        table_start = pos + 27
        table_end = table_start + segments_count
        if table_end > len(data):
            raise RuntimeError("truncated Ogg segment table")
        segment_sizes = data[table_start:table_end]
        body_start = table_end
        body_end = body_start + sum(segment_sizes)
        if body_end > len(data):
            raise RuntimeError("truncated Ogg page body")

        cursor = body_start
        for segment_size in segment_sizes:
            packet.extend(data[cursor : cursor + segment_size])
            cursor += segment_size
            if segment_size < 255:
                complete = bytes(packet)
                packet.clear()
                if complete and not (complete.startswith(b"OpusHead") or complete.startswith(b"OpusTags")):
                    packets.append(complete)
        pos = body_end
    if packet:
        raise RuntimeError("truncated Ogg Opus packet")
    return packets
