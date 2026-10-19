"""Codec contract tests — no real libopus required.

The decoder/encoder are mocked because pytest must run on hosts without
libopus installed. The point of these tests is the framing logic on top
of opuslib, not opuslib itself.
"""

from __future__ import annotations

import math
import struct

import pytest

from stackchan_mcp.opus_codec import (
    INPUT_FRAME_BYTES,
    INPUT_FRAME_SAMPLES,
    OUTPUT_FRAME_BYTES,
    OUTPUT_FRAME_SAMPLES,
    StackchanOpusCodec,
    _patch_find_library_for_brew,
)


class FakeDecoder:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int]] = []

    def decode(self, opus_data: bytes, frame_size: int) -> bytes:
        self.calls.append((opus_data, frame_size))
        # libopus would emit `frame_size * 2` bytes of int16 PCM.
        return b"\x01\x00" * frame_size


class FakeEncoder:
    bitrate = 32000

    def __init__(self, fail_on: int | None = None) -> None:
        self.calls: list[tuple[bytes, int]] = []
        self._fail_on = fail_on

    def encode(self, pcm_data: bytes, frame_size: int) -> bytes:
        self.calls.append((pcm_data, frame_size))
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise RuntimeError("simulated libopus failure")
        # libopus returns variable-size Opus frames. Stuff in something
        # deterministic for assertions.
        return b"OPUS" + bytes([frame_size & 0xFF])


# --- Frame sizing constants ---------------------------------------------------


def test_frame_constants_match_60ms_at_device_rates():
    assert INPUT_FRAME_SAMPLES == 960   # 16 kHz × 60 ms
    assert OUTPUT_FRAME_SAMPLES == 1440  # 24 kHz × 60 ms
    assert INPUT_FRAME_BYTES == 1920    # int16 × 960
    assert OUTPUT_FRAME_BYTES == 2880   # int16 × 1440


# --- Decode -------------------------------------------------------------------


def test_decode_device_frame_returns_pcm_bytes():
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=FakeEncoder())
    pcm = codec.decode_device_frame(b"opus-bytes-123")
    assert len(pcm) == INPUT_FRAME_BYTES
    assert isinstance(pcm, bytes)


def test_decode_empty_frame_is_dropped():
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=FakeEncoder())
    assert codec.decode_device_frame(b"") == b""


def test_decode_swallows_corrupt_frame():
    """One bad packet must not crash the live stream."""

    class BadDecoder:
        def decode(self, opus_data, frame_size):
            raise RuntimeError("corrupt frame")

    codec = StackchanOpusCodec(decoder=BadDecoder(), encoder=FakeEncoder())
    assert codec.decode_device_frame(b"\xff" * 64) == b""


# --- Encode -------------------------------------------------------------------


def test_encode_pcm_24k_splits_into_60ms_frames():
    enc = FakeEncoder()
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=enc)
    # Three exact frames worth of PCM.
    pcm = b"\x00\x01" * (OUTPUT_FRAME_SAMPLES * 3)
    frames = codec.encode_pcm_24k(pcm)
    assert len(frames) == 3
    assert len(enc.calls) == 3
    # Each encoder call must see exactly one 60 ms chunk.
    for chunk, frame_size in enc.calls:
        assert frame_size == OUTPUT_FRAME_SAMPLES
        assert len(chunk) == OUTPUT_FRAME_BYTES


def test_encode_pcm_24k_buffers_leftover_between_calls():
    enc = FakeEncoder()
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=enc)
    half = b"\x00" * (OUTPUT_FRAME_BYTES // 2)
    # First half — no frame can be emitted yet.
    assert codec.encode_pcm_24k(half) == []
    # Second half — buffer fills, one frame comes out.
    frames = codec.encode_pcm_24k(half)
    assert len(frames) == 1
    assert len(enc.calls) == 1


def test_encode_pcm_24k_empty_input_returns_empty():
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=FakeEncoder())
    assert codec.encode_pcm_24k(b"") == []


def test_encode_pcm_24k_swallows_single_frame_error():
    """One bad encode shouldn't cancel the rest of the utterance."""
    enc = FakeEncoder(fail_on=2)
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=enc)
    pcm = b"\x00\x01" * (OUTPUT_FRAME_SAMPLES * 3)
    frames = codec.encode_pcm_24k(pcm)
    # Frame 1 and 3 survive, frame 2 dropped.
    assert len(frames) == 2


# --- Flush --------------------------------------------------------------------


def test_flush_emits_padded_partial_frame():
    enc = FakeEncoder()
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=enc)
    partial = b"\x00" * (OUTPUT_FRAME_BYTES // 3)
    codec.encode_pcm_24k(partial)
    out = codec.flush()
    assert len(out) == 1
    # Encoder saw a full 60 ms chunk after zero-padding.
    assert len(enc.calls) == 1
    final_chunk, _ = enc.calls[0]
    assert len(final_chunk) == OUTPUT_FRAME_BYTES


def test_flush_with_empty_buffer_is_noop():
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=FakeEncoder())
    assert codec.flush() == []


def test_reset_encode_buffer_drops_in_flight_pcm():
    enc = FakeEncoder()
    codec = StackchanOpusCodec(decoder=FakeDecoder(), encoder=enc)
    codec.encode_pcm_24k(b"\x00" * (OUTPUT_FRAME_BYTES // 2))
    codec.reset_encode_buffer()
    # No frame even after flush — buffer was dropped, not padded.
    assert codec.flush() == []


def test_real_output_encoder_preserves_non_silent_pcm_when_libopus_available():
    """Gemini downlink TTS must not become 2-byte comfort-noise frames."""
    _patch_find_library_for_brew()
    try:
        from opuslib import Decoder  # noqa: PLC0415
    except Exception as exc:
        pytest.skip(f"opuslib not importable: {exc}")

    pcm = b"".join(
        struct.pack("<h", int(24000 * math.sin(2 * math.pi * 440 * n / 24000)))
        for n in range(OUTPUT_FRAME_SAMPLES)
    )
    codec = StackchanOpusCodec()
    try:
        frames = codec.encode_pcm_24k(pcm)
        decoder = Decoder(24000, 1)
        decoded = decoder.decode(frames[0], OUTPUT_FRAME_SAMPLES)
    except Exception as exc:
        pytest.skip(f"libopus not available: {exc}")

    assert frames
    assert len(frames[0]) > 20
    samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
    assert max(abs(sample) for sample in samples) > 1000
