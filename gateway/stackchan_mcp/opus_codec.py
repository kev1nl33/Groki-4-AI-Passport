"""Opus encode/decode helpers for the StackChan voice path.

The device speaks Opus on the wire in two flavours, both 60 ms / mono:

  * Device → gateway: 16 kHz Opus (microphone capture matching HelloMessage's
    AudioParams default).
  * Gateway → device: 24 kHz Opus (LCD speaker decode path, matching the
    HelloResponse the gateway advertises).

The Gemini Live API speaks 16 kHz PCM in / 24 kHz PCM out. The numbers line
up exactly: decode the device's 60 ms 16 kHz Opus packet and the resulting
PCM goes straight to ``send_realtime_input``, and Gemini's 24 kHz PCM bursts
break cleanly into 60 ms 24 kHz Opus frames that the device's audio decoder
already knows how to play.

opuslib is a ctypes wrapper around system libopus. The constructors only
allocate on first use so unit tests that don't touch real audio can import
this module without libopus on the host.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


_libopus_patched = False


def _patch_find_library_for_brew() -> None:
    """Make ``find_library('opus')`` see brew's libopus on macOS Tahoe.

    macOS 11+ dyld cache no longer carries /opt/homebrew/lib, so
    ``ctypes.util.find_library('opus')`` returns None even when
    ``brew install opus`` was run. opuslib raises at import time when
    it can't locate the library, which kills the whole voice backend.
    Monkey-patch find_library so opuslib's import succeeds.
    """
    global _libopus_patched
    if _libopus_patched or sys.platform != "darwin":
        _libopus_patched = True
        return
    if ctypes.util.find_library("opus") is not None:
        _libopus_patched = True
        return
    candidates = [
        os.environ.get("STACKCHAN_LIBOPUS_PATH"),
        "/opt/homebrew/lib/libopus.0.dylib",
        "/opt/homebrew/lib/libopus.dylib",
        "/usr/local/lib/libopus.0.dylib",
        "/usr/local/lib/libopus.dylib",
    ]
    found = next((p for p in candidates if p and Path(p).exists()), None)
    if found is None:
        logger.warning(
            "libopus not in dyld cache and no brew copy found; "
            "set STACKCHAN_LIBOPUS_PATH or brew install opus",
        )
        _libopus_patched = True
        return
    original = ctypes.util.find_library

    def patched(name: str):
        if name == "opus":
            return found
        return original(name)

    ctypes.util.find_library = patched
    _libopus_patched = True
    logger.info("libopus path resolved via brew fallback: %s", found)

DEVICE_INPUT_SAMPLE_RATE = 16000
DEVICE_OUTPUT_SAMPLE_RATE = 24000
FRAME_DURATION_MS = 60
INPUT_FRAME_SAMPLES = DEVICE_INPUT_SAMPLE_RATE * FRAME_DURATION_MS // 1000
OUTPUT_FRAME_SAMPLES = DEVICE_OUTPUT_SAMPLE_RATE * FRAME_DURATION_MS // 1000
INPUT_FRAME_BYTES = INPUT_FRAME_SAMPLES * 2  # int16 mono
OUTPUT_FRAME_BYTES = OUTPUT_FRAME_SAMPLES * 2
DEFAULT_BITRATE: int | None = None


class _OpusDecoderLike(Protocol):
    def decode(self, opus_data: bytes, frame_size: int) -> bytes: ...


class _OpusEncoderLike(Protocol):
    def encode(self, pcm_data: bytes, frame_size: int) -> bytes: ...


class StackchanOpusCodec:
    """Stateful codec sized for the StackChan device protocol.

    One instance per voice session: the encoder/decoder carry internal state
    (look-ahead, packet-loss prediction) and must not be shared across
    sessions or restarted mid-utterance.
    """

    def __init__(
        self,
        *,
        bitrate: int | None = DEFAULT_BITRATE,
        decoder: _OpusDecoderLike | None = None,
        encoder: _OpusEncoderLike | None = None,
    ) -> None:
        self._decoder: _OpusDecoderLike | None = decoder
        self._encoder: _OpusEncoderLike | None = encoder
        self._bitrate = bitrate
        # Buffer leftover PCM bytes that don't fill a full 60 ms frame so
        # the next encode_pcm_24k() call picks them up.
        self._encode_buffer = bytearray()

    def _ensure_decoder(self) -> _OpusDecoderLike:
        if self._decoder is None:
            _patch_find_library_for_brew()
            from opuslib import Decoder  # noqa: PLC0415

            self._decoder = Decoder(DEVICE_INPUT_SAMPLE_RATE, 1)
        return self._decoder

    def _ensure_encoder(self) -> _OpusEncoderLike:
        if self._encoder is None:
            _patch_find_library_for_brew()
            # Downlink is assistant speech/TTS, not microphone voice activity.
            # APPLICATION_VOIP can emit 2-byte comfort-noise frames for this
            # path on some libopus/opuslib builds, which makes ESP32 enter
            # Speaking with no audible sound.
            from opuslib import APPLICATION_AUDIO, Encoder  # noqa: PLC0415

            enc = Encoder(DEVICE_OUTPUT_SAMPLE_RATE, 1, APPLICATION_AUDIO)
            # Do not force opuslib's bitrate in the default path. On the
            # macOS/libopus build used for gateway development, assigning
            # ``Encoder.bitrate = 32000`` makes non-silent Gemini PCM encode
            # into 2-byte comfort-noise packets, so the ESP32 enters Speaking
            # but the speaker plays silence. Libopus' default VBR produces
            # normal speech-sized packets for this 24 kHz TTS stream.
            if self._bitrate is not None:
                try:
                    enc.bitrate = self._bitrate
                except Exception:
                    logger.warning("opuslib encoder did not accept bitrate=%d", self._bitrate)
            self._encoder = enc
        return self._encoder

    def decode_device_frame(self, opus_frame: bytes) -> bytes:
        """Decode one device-side 60 ms 16 kHz Opus packet to PCM bytes.

        Returns int16 mono PCM bytes ready to forward to Gemini Live's
        ``audio/pcm;rate=16000`` input. Drops the packet (returns b'') if
        the decoder rejects it — this is the right behaviour for live
        streaming where one corrupt frame must not cancel the whole turn.
        """
        if not opus_frame:
            return b""
        try:
            return bytes(
                self._ensure_decoder().decode(opus_frame, INPUT_FRAME_SAMPLES)
            )
        except Exception as exc:
            logger.warning("Opus decode failed for %d-byte frame: %s", len(opus_frame), exc)
            return b""

    def encode_pcm_24k(self, pcm: bytes) -> list[bytes]:
        """Encode 24 kHz mono int16 PCM into 60 ms Opus frames.

        Buffers leftover samples so successive calls handle arbitrary
        Gemini chunk sizes without dropping any audio at the seams.
        Call ``flush()`` at end-of-turn to drain a partial frame padded
        with silence.
        """
        if not pcm:
            return []
        self._encode_buffer.extend(pcm)
        out: list[bytes] = []
        while len(self._encode_buffer) >= OUTPUT_FRAME_BYTES:
            chunk = bytes(self._encode_buffer[:OUTPUT_FRAME_BYTES])
            del self._encode_buffer[:OUTPUT_FRAME_BYTES]
            try:
                frame = bytes(
                    self._ensure_encoder().encode(chunk, OUTPUT_FRAME_SAMPLES)
                )
            except Exception as exc:
                logger.warning("Opus encode failed for 60ms 24kHz chunk: %s", exc)
                continue
            out.append(frame)
        return out

    def flush(self) -> list[bytes]:
        """Emit any leftover partial frame zero-padded to 60 ms.

        Use at end-of-turn so the device doesn't lose the tail of an
        utterance (e.g. Gemini ends on 39 ms of PCM, we round it up to
        60 ms and the speaker plays the whole thing).
        """
        if not self._encode_buffer:
            return []
        padding = OUTPUT_FRAME_BYTES - len(self._encode_buffer)
        if padding > 0:
            self._encode_buffer.extend(b"\x00" * padding)
        chunk = bytes(self._encode_buffer)
        self._encode_buffer.clear()
        try:
            frame = bytes(self._ensure_encoder().encode(chunk, OUTPUT_FRAME_SAMPLES))
        except Exception as exc:
            logger.warning("Opus flush encode failed: %s", exc)
            return []
        return [frame]

    def reset_encode_buffer(self) -> None:
        """Drop any in-flight PCM tail without encoding.

        Use when a turn is interrupted (user starts talking mid-reply) so
        the next utterance doesn't start with leftover samples from the
        cancelled one.
        """
        self._encode_buffer.clear()


def make_codec(**kwargs: Any) -> StackchanOpusCodec:
    """Factory hook so tests can inject fakes via the call site."""
    return StackchanOpusCodec(**kwargs)
