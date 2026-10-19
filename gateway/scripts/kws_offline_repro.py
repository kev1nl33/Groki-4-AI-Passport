#!/usr/bin/env python3
"""Check the wake word KWS offline.

Usage:

    uv run --extra wakeword python scripts/kws_offline_repro.py --generate-say
    uv run --extra wakeword python scripts/kws_offline_repro.py --wav /path/to/16k.wav

Only ``wake_gate.SherpaOnnxKeywordSpotter`` itself is checked; no gateway and no device are involved.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import wave
from pathlib import Path

from stackchan_mcp.wake_gate import (
    SherpaOnnxKeywordSpotter,
    _keyword_from_env,
    _phrase_from_env,
)


def _kws_float_from_env(name: str) -> float | None:
    """Read the environment variable of the same name, so KWS parameters can be overridden from the environment."""
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _generate_say_wav(text: str, voice: str, tmpdir: Path) -> Path:
    aiff = tmpdir / "wake.aiff"
    wav = tmpdir / "wake-16k.wav"
    subprocess.run(["say", "-v", voice, "-r", "150", "-o", str(aiff), f"   {text}   "], check=True)
    subprocess.run(
        [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            "-c",
            "1",
            str(aiff),
            str(wav),
        ],
        check=True,
    )
    return wav


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        if (channels, sample_rate, sample_width) != (1, 16_000, 2):
            raise SystemExit(
                f"only 16 kHz 16-bit mono WAV is accepted, got "
                f"{channels}ch {sample_rate}Hz {sample_width * 8}-bit"
            )
        return wav.readframes(wav.getnframes())


def detect_wake(
    pcm: bytes,
    *,
    chunk_ms: int,
    kws_score: float | None = None,
    kws_threshold: float | None = None,
) -> int | None:
    # Same wake word the gateway uses (STACKCHAN_WAKE_PHRASE / _KEYWORD).
    spotter_kwargs: dict[str, float | str] = {
        "keyword": _keyword_from_env(),
        "phrase": _phrase_from_env(),
    }
    if kws_score is not None:
        spotter_kwargs["keywords_score"] = kws_score
    if kws_threshold is not None:
        spotter_kwargs["keywords_threshold"] = kws_threshold
    spotter = SherpaOnnxKeywordSpotter(**spotter_kwargs)
    chunk_bytes = int(16_000 * 2 * chunk_ms / 1000)
    for idx, offset in enumerate(range(0, len(pcm), chunk_bytes)):
        if spotter.detect(pcm[offset : offset + chunk_bytes]):
            return idx
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--generate-say", action="store_true")
    parser.add_argument("--text", default=_phrase_from_env())
    parser.add_argument("--voice", default="Moira")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument(
        "--kws-score",
        type=float,
        default=_kws_float_from_env("STACKCHAN_KWS_SCORE"),
    )
    parser.add_argument(
        "--kws-threshold",
        type=float,
        default=_kws_float_from_env("STACKCHAN_KWS_THRESHOLD"),
    )
    args = parser.parse_args()

    if not args.wav and not args.generate_say:
        parser.error("pass --wav or --generate-say")

    with tempfile.TemporaryDirectory(prefix="stackchan-kws-") as tmp:
        wav_path = args.wav or _generate_say_wav(args.text, args.voice, Path(tmp))
        # One second of trailing silence lets the streaming model flush the
        # last frames; without it a wake phrase at the very end is missed.
        pcm = _read_wav(wav_path) + b"\0" * 32_000
        hit_at = detect_wake(
            pcm,
            chunk_ms=args.chunk_ms,
            kws_score=args.kws_score,
            kws_threshold=args.kws_threshold,
        )

    if hit_at is None:
        print(f"miss: {args.text!r}")
        return 1
    print(f"hit: {args.text!r}, chunk={hit_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
