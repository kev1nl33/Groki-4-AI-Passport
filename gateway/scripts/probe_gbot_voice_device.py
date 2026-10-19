#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""No-hardware Opus loopback through GbotVoiceProxy; does not start a gateway."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import wave

from probe_gbot_voice import read_pcm
from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy
from stackchan_mcp.opus_codec import _patch_find_library_for_brew


async def run(args):
    if os.getenv("STACKCHAN_GBOT_VOICE_TOOLS") == "1":
        raise ValueError("Device loopback requires tools disabled")
    initial = read_pcm(args.input_wav)
    followup = read_pcm(args.interrupt_wav)
    _patch_find_library_for_brew()
    from opuslib import APPLICATION_AUDIO, Decoder, Encoder
    microphone = Encoder(16000, 1, APPLICATION_AUDIO)
    speaker = Decoder(24000, 1)
    output = bytearray()
    envelopes = []
    transcripts = []
    first_audio = asyncio.Event()
    response_verified = asyncio.Event()
    followup_started = False
    initial_response_id = None
    abort_at = float("inf")
    started = time.monotonic()
    counts = {"input_opus_frames": 0, "output_opus_frames": 0}
    summary = {"scope": "fake_xiaozhi_device_real_opus_no_robot", "passed": False}
    proxy = None
    bridge = None

    async def sink(message):
        if isinstance(message, bytes):
            output.extend(speaker.decode(message, 1440))
            counts["output_opus_frames"] += 1
            first_audio.set()
        else:
            envelopes.append({"seconds": round(time.monotonic() - started, 3),
                              "message": json.loads(message)})

    async def transcript(event):
        entry = {"type": event.get("type"), "text": event.get("transcript", ""),
                 "response_id": event.get("response_id")}
        transcripts.append(entry)
        if (followup_started and entry["response_id"] != initial_response_id
                and bridge.response_started_at.get(entry["response_id"], 0) > abort_at
                and entry["type"].startswith("response.")
                and "interruption successful" in entry["text"].lower()):
            response_verified.set()

    def factory(**kwargs):
        nonlocal bridge
        bridge = GbotVoiceBridge(**kwargs, local_tools=False, on_transcript=transcript)
        return bridge

    async def feed(pcm):
        for index in range(0, len(pcm), 1920):
            chunk = pcm[index:index + 1920].ljust(1920, b"\x00")
            packet = microphone.encode(chunk, 960)
            if not await proxy.send_device_binary(packet):
                raise RuntimeError("Device input rejected")
            counts["input_opus_frames"] += 1
            await asyncio.sleep(0.06)
        for _ in range(20):
            if not await proxy.send_device_binary(microphone.encode(bytes(1920), 960)):
                raise RuntimeError("Device silence rejected")
            counts["input_opus_frames"] += 1
            await asyncio.sleep(0.06)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        proxy = GbotVoiceProxy(bridge_factory=factory)
        if not await proxy.start(SimpleNamespace(device_id="opus-loopback"), {}, sink):
            raise RuntimeError("Voice proxy not ready")
        summary["provider"] = proxy.server_hello
        await feed(initial)
        await asyncio.wait_for(first_audio.wait(), 40)
        await asyncio.sleep(0.3)
        prior_id = bridge._active_response
        initial_response_id = prior_id
        prior_frame_count = counts["output_opus_frames"]
        summary["before_abort"] = {"tts_active": proxy._tts_active,
                                    "queue_frames": proxy._frame_queue.qsize(),
                                    "pcm_bytes": len(proxy._pcm_prebuffer),
                                    "codec_tail_bytes": len(proxy._codec._encode_buffer)}
        stop_before = sum(e["message"].get("state") == "stop" for e in envelopes)
        abort_at = time.monotonic()
        await proxy.send_device_json({"type": "abort", "reason": "loopback_verification"})
        snapshot = {"tts_active": proxy._tts_active, "queue_frames": proxy._frame_queue.qsize(),
                    "pcm_bytes": len(proxy._pcm_prebuffer),
                    "codec_tail_bytes": len(proxy._codec._encode_buffer)}
        summary["after_abort"] = snapshot
        summary["abort_sent_tts_stop"] = (
            sum(e["message"].get("state") == "stop" for e in envelopes) > stop_before)
        discarded_before = bridge.metrics["discarded_audio_deltas"]
        # Controlled late packet proves cancellation boundary after actual output.
        await bridge._handle_event({"type": "response.audio.delta", "response_id": prior_id,
                                    "delta": base64.b64encode(bytes(480)).decode()})
        summary["injected_stale_delta_discarded"] = (
            bridge.metrics["discarded_audio_deltas"] == discarded_before + 1)
        followup_started = True
        await feed(followup)
        await asyncio.wait_for(response_verified.wait(), 40)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if counts["output_opus_frames"] > prior_frame_count and not proxy._tts_active:
                break
            await asyncio.sleep(0.1)
        summary["new_opus_audio_after_abort"] = (
            counts["output_opus_frames"] > prior_frame_count
            and any(response_id != prior_id and count > 0
                    and bridge.response_started_at.get(response_id, 0) > abort_at
                    for response_id, count in bridge.audio_bytes_by_response.items()))
        summary["passed"] = (not any(snapshot.values()) and summary["abort_sent_tts_stop"]
                             and summary["injected_stale_delta_discarded"]
                             and summary["new_opus_audio_after_abort"]
                             and response_verified.is_set())
    except Exception as error:
        summary["error_type"] = type(error).__name__
    finally:
        if proxy:
            await proxy.stop()
        with wave.open(str(args.output_dir / "device-received.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        summary.update({"counts": counts, "fixture_transcripts": transcripts,
                        "envelopes": envelopes, "bridge_metrics": bridge.metrics if bridge else {},
                        "decoded_output_seconds": round(len(output) / 48000, 3)})
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-wav", required=True, type=Path)
    parser.add_argument("--interrupt-wav", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
