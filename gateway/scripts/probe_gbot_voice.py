#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Gateway-only Grok Bot voice probe. Does not connect to robot or its gateway.

Input WAV files must be signed 16-bit mono PCM at 16 or 24 kHz. Output is local
24 kHz PCM WAV. Supply speech recordings/synthetic fixtures, not real secrets.
"""
import argparse
import asyncio
import json
import os
import struct
from pathlib import Path
import time
import wave

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge


def read_pcm(path):
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        if rate not in {16000, 24000} or (wav.getnchannels(), wav.getsampwidth()) != (1, 2):
            raise ValueError("Input must be mono PCM16 WAV at 16000 or 24000 Hz")
        pcm = wav.readframes(wav.getnframes())
    if rate == 16000:
        return pcm
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    values = []
    for numerator in range(0, 2 * (len(samples) - 1), 3):
        index, fraction = divmod(numerator, 2)
        values.append(round((samples[index] * (2 - fraction) + samples[index + 1] * fraction) / 2))
    return struct.pack(f"<{len(values)}h", *values)


async def run(args):
    if os.getenv("STACKCHAN_GBOT_VOICE_TOOLS") == "1":
        raise ValueError("Gateway probe requires harness tools disabled")
    initial = read_pcm(args.input_wav) if args.input_wav else None
    interruption = read_pcm(args.interrupt_wav) if args.interrupt_wav else None
    output = bytearray()
    transcripts = []
    interrupted_response = None
    interrupt_started_at = float("inf")
    interruption_observed = False
    first_audio = asyncio.Event()
    completed = asyncio.Event()
    dead = asyncio.Event()
    events = []
    started = time.monotonic()

    def record(kind):
        events.append({"kind": kind, "seconds": round(time.monotonic() - started, 3)})

    async def on_audio(pcm):
        if not first_audio.is_set():
            record("first_output_audio")
            first_audio.set()
        output.extend(pcm)

    async def on_complete():
        record("response_complete")
        completed.set()

    async def on_interrupt():
        record("server_speech_started")

    async def on_transcript(event):
        transcripts.append({"type": event.get("type"),
                            "response_id": event.get("response_id"),
                            "text": event.get("transcript", "")})

    async def on_dead():
        record("disconnected")
        dead.set()

    bridge = GbotVoiceBridge(local_tools=False, on_audio=on_audio, on_turn_complete=on_complete,
                             on_interrupt=on_interrupt, on_session_dead=on_dead,
                             on_transcript=on_transcript)

    async def send_pcm(pcm):
        # Mic packets and silence continue during output to exercise server VAD.
        for index in range(0, len(pcm), 1920):
            if dead.is_set():
                raise RuntimeError("Voice disconnected during input")
            await bridge.send_audio(pcm[index:index + 1920])
            await asyncio.sleep(0.06)
        for _ in range(20):
            await bridge.send_audio(bytes(1920))
            await asyncio.sleep(0.06)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        await bridge.start()
        if not await bridge.wait_connected(15):
            raise RuntimeError("Voice session was not acknowledged")
        record("session_ready")
        if initial:
            record("initial_fixture_start")
            await send_pcm(initial)
        else:
            await bridge.send_text("请用中文从一数到三十，语速慢一点。")
        await asyncio.wait_for(first_audio.wait(), 30)
        if interruption:
            await asyncio.sleep(0.3)
            before = bridge.metrics["interruptions"]
            interrupted_response = bridge._active_response
            interrupt_started_at = time.monotonic()
            completed.clear()
            record("interrupt_fixture_start")
            await send_pcm(interruption)
            record("interrupt_fixture_end")
            # Wait for recognition/new response; initial speech_started does not count.
            deadline = time.monotonic() + 20
            while bridge.metrics["interruptions"] <= before and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            interruption_observed = bridge.metrics["interruptions"] > before
            record("interrupt_verified" if interruption_observed else "interrupt_not_observed")
        try:
            await asyncio.wait_for(completed.wait(), 30)
        except asyncio.TimeoutError:
            record("response_timeout")
        await asyncio.sleep(2)
    except Exception as error:
        record("probe_failed_" + type(error).__name__)
    finally:
        await bridge.stop()
        with wave.open(str(args.output_dir / "received.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        new_reply_audio = any(response_id != interrupted_response and count > 0
                              and bridge.response_started_at.get(response_id, 0) > interrupt_started_at
                              for response_id, count in bridge.audio_bytes_by_response.items())
        semantic_match = any("interruption successful" in entry["text"].lower()
                             for entry in transcripts
                             if entry["type"].startswith("response.")
                             and entry["response_id"] != interrupted_response
                             and bridge.response_started_at.get(entry["response_id"], 0) > interrupt_started_at)
        summary = {"scope": "gateway_only_no_robot",
                   "interruption_scope": "upstream_cancel_only_queue_clear_covered_by_unit_test",
                   "new_reply_audio_after_interruption": new_reply_audio,
                   "interruption_reply_semantic_match": semantic_match,
                   "fixture_transcripts": transcripts, "synthetic_or_recorded_input": bool(initial),
                   "interrupt_fixture_supplied": bool(interruption),
                   "metrics": bridge.metrics, "events": events,
                   "received_audio_seconds": round(len(output) / 48000, 3)}
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
    passed = bool(output) and not any(e["kind"].startswith("probe_failed") for e in events)
    if interruption:
        passed = passed and interruption_observed and new_reply_audio and semantic_match
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-wav", type=Path)
    parser.add_argument("--interrupt-wav", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
