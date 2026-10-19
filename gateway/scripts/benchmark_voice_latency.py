#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Fair no-hardware voice latency benchmark through real device Opus proxies.

Only this process is configured: server VAD, no local wake gate, no tools, the
same neutral instructions and fixtures. Credentials remain in process memory.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import plistlib
import statistics
import struct
import time
from types import SimpleNamespace
from unittest.mock import patch
import wave

from probe_gbot_voice import read_pcm

PROMPT = ("You are a voice latency test assistant. Speak English. Follow the user's "
          "spoken instructions exactly and concisely. Never call tools. If asked to count, "
          "count slowly. If interrupted, stop counting and follow the new instruction.")


def waveform_bounds(pcm, threshold=180):
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    voiced = [i for i, sample in enumerate(samples) if abs(sample) >= threshold]
    if not voiced:
        raise ValueError("Fixture has no audible waveform")
    return voiced[0], voiced[-1] + 1


def safe_error(error):
    code = getattr(error, "status_code", None) or getattr(error, "code", None)
    if code is None:
        code = getattr(getattr(error, "response", None), "status_code", None)
    if not isinstance(code, int):
        code = None
    result = {"type": type(error).__name__}
    if code is not None:
        result["status"] = code
    # Never serialize exception strings: SDK errors may embed authenticated URLs.
    return result


def load_credentials(env_file, launchd_plist):
    from dotenv import load_dotenv
    if launchd_plist.exists():
        data = plistlib.loads(launchd_plist.read_bytes())
        for name, value in data.get("EnvironmentVariables", {}).items():
            if name in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "STACKCHAN_GEMINI_MODEL",
                        "STACKCHAN_GEMINI_VOICE", "STACKCHAN_GBOT_VOICE_BOT_ID",
                        "STACKCHAN_TOOL_BOT_ID"}:
                os.environ.setdefault(name, str(value))
    load_dotenv(env_file, override=False)


class Timeline:
    def __init__(self):
        self.origin = time.monotonic()
        self.events = []
        self.round = -1
        self.phase = "connect"

    def emit(self, name, **fields):
        event = {"name": name, "seconds": round(time.monotonic() - self.origin, 6),
                 "round": self.round, "phase": self.phase, **fields}
        self.events.append(event)
        return event

    def now(self):
        return time.monotonic() - self.origin


def duration(events, start_name, end_name, *, phase=None):
    starts = [e["seconds"] for e in events if e["name"] == start_name
              and (phase is None or e["phase"] == phase)]
    if not starts:
        return None
    ends = [e["seconds"] for e in events if e["name"] == end_name
            and (phase is None or e["phase"] == phase)]
    return round((ends[0] - starts[0]) * 1000, 3) if ends else None


def summarize_round(events):
    return {
        "initial_postutterance_first_pcm_ms": duration(events, "speech_packet_last_sent", "output_pcm_first", phase="initial"),
        "initial_postutterance_first_opus_ms": duration(events, "speech_packet_last_sent", "output_opus_first", phase="initial"),
        "interrupt_speech_start_to_server_detection_ms": duration(events, "wave_speech_start", "server_speech_started", phase="interrupt"),
        "interrupt_speech_end_to_server_detection_ms": duration(events, "speech_packet_last_sent", "server_speech_started", phase="interrupt"),
        "interrupt_speech_start_to_local_stop_ms": duration(events, "wave_speech_start", "tts_stop", phase="interrupt"),
        "interrupt_speech_start_to_queue_zero_ms": duration(events, "wave_speech_start", "queue_zero", phase="interrupt"),
        "server_detection_to_local_stop_ms": duration(events, "server_speech_started", "tts_stop", phase="interrupt"),
        "server_detection_to_queue_zero_ms": duration(events, "server_speech_started", "queue_zero", phase="interrupt"),
        "interrupt_postutterance_first_pcm_ms": duration(events, "speech_packet_last_sent", "output_pcm_first", phase="interrupt"),
        "interrupt_postutterance_first_opus_ms": duration(events, "speech_packet_last_sent", "output_opus_first", phase="interrupt"),
        "interrupt_fixture_speech_ms": duration(events, "wave_speech_start", "wave_speech_end", phase="interrupt"),
    }


def reasoning_update(event, effort):
    """Apply only the requested single experiment; preserve model and all other events."""
    if event.get("type") != "session.update" or effort == "default":
        return event
    session = event.get("session", {})
    return {**event, "session": {**session, "reasoning": {**session.get("reasoning", {}), "effort": effort}}}


def reasoning_echo(event, requested):
    session = event.get("session", {})
    reasoning = session.get("reasoning")
    value = reasoning.get("effort") if isinstance(reasoning, dict) else None
    allowed = {"none", "high", "low", "medium", "minimal"}
    safe = value if isinstance(value, str) and value in allowed else (None if value is None else "other")
    return {"requested": requested, "echo": safe,
            "supported": safe == requested if requested != "default" else None}


def interruption_semantics(transcripts, round_index):
    outputs = [entry.get("text", "") for entry in transcripts
               if entry.get("round") == round_index and entry.get("phase") == "interrupt"
               and (entry.get("kind", "").startswith("response.")
                    or entry.get("kind") == "server_output_transcript")]
    normalized = "".join("".join(outputs).lower().split())
    return "interruptionsuccessful" in normalized


def assess_result(result, args, errors):
    failures = []
    if not result.get("connected"):
        failures.append("not_connected")
    if errors:
        failures.append("runtime_errors")
    requested = getattr(args, "gbot_reasoning_effort", "default")
    if result.get("backend") == "gbot" and requested != "default":
        if result.get("reasoning_experiment", {}).get("supported") is not True:
            failures.append("reasoning_effort_not_confirmed_by_session_echo")
    rounds = result.get("rounds", [])
    if len(rounds) != args.warm_rounds + 1:
        failures.append("incomplete_rounds")
    limits = [
        ("initial_postutterance_first_pcm_ms", args.max_initial_ttfa_ms),
        ("interrupt_speech_start_to_server_detection_ms", args.max_interrupt_detection_ms),
        ("interrupt_postutterance_first_pcm_ms", args.max_interrupt_ttfa_ms),
        ("server_detection_to_local_stop_ms", args.max_local_stop_ms),
    ]
    for row in rounds:
        timings = row["timings"]
        if row.get("interrupt_semantic_match") is not True:
            failures.append(f"round_{row['round']}_interrupt_semantics_missing_or_mismatch")
        for required in ["initial_postutterance_first_pcm_ms", "initial_postutterance_first_opus_ms",
                         "interrupt_postutterance_first_pcm_ms", "interrupt_postutterance_first_opus_ms"]:
            if timings.get(required) is None:
                failures.append(f"round_{row['round']}_missing_{required}")
        if args.require_interruption:
            for required in ["interrupt_speech_start_to_server_detection_ms", "server_detection_to_local_stop_ms",
                             "server_detection_to_queue_zero_ms"]:
                if timings.get(required) is None:
                    failures.append(f"round_{row['round']}_missing_{required}")
        for name, maximum in limits:
            if maximum is not None and (timings.get(name) is None or timings[name] > maximum):
                failures.append(f"round_{row['round']}_limit_{name}")
    return not failures, failures


def summarize_pipeline(events):
    def stats(name, field):
        values = [e[field] for e in events if e["name"] in name and isinstance(e.get(field), (int, float))]
        if not values:
            return None
        ordered = sorted(values)
        return {"samples": len(values), "median": round(statistics.median(values), 3),
                "max": max(values), "sum": round(sum(values), 3), "last": values[-1],
                "p95": ordered[max(0, math.ceil(len(ordered) * .95) - 1)]}
    return {
        "proxy_or_direct_input_send": stats({"input_frame_sent"}, "send_ms"),
        "upstream_input_send": stats({"upstream_audio_send_done"}, "send_ms"),
        "input_schedule_lag": stats({"input_frame_sent"}, "schedule_lag_ms"),
        "server_handler": stats({"handler_exit", "gemini_handler_exit"}, "handler_ms"),
        "on_audio_callback": stats({"on_audio_done"}, "callback_ms"),
    }


def write_checkpoint(directory, result, events):
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = {"backend": result["backend"], "model": result.get("model"),
                  "prompt_mode": result["configuration"]["prompt_mode"],
                  "cold_connect_ms": duration(events, "proxy_start", "proxy_ready"),
                  "completed_rounds": len(result["rounds"]), "rounds": result["rounds"],
                  "pipeline_timing_ms": summarize_pipeline(events),
                  "reasoning_experiment": result.get("reasoning_experiment")}
    temporary = directory / ".summary.partial.json.tmp"
    temporary.write_text(json.dumps(checkpoint, indent=2))
    temporary.replace(directory / "summary.partial.json")


async def benchmark(args, backend):
    # Import only after env loading, because Gemini's model default is import-time.
    import aiohttp
    from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge
    from stackchan_mcp.gbot_voice_proxy import GbotVoiceProxy
    from stackchan_mcp.gemini_live_bridge import GeminiLiveBridge
    from stackchan_mcp.gemini_voice_proxy import GeminiVoiceProxy
    from stackchan_mcp.opus_codec import _patch_find_library_for_brew
    import stackchan_mcp.gemini_live_bridge as gemini_module

    timeline = Timeline()
    output = bytearray()
    first_pcm = asyncio.Event()
    first_opus = asyncio.Event()
    turn_done = asyncio.Event()
    transcripts = []
    errors = []
    bridge = None
    proxy = None
    pcm_seen = set()
    opus_seen = set()
    complete_count = 0
    initial_response_id = None
    interrupt_recognized = False
    _patch_find_library_for_brew()
    from opuslib import APPLICATION_AUDIO, Decoder, Encoder
    microphone = Encoder(16000, 1, APPLICATION_AUDIO)
    speaker = Decoder(24000, 1)

    class ObservedGbot(GbotVoiceBridge):
        async def _send(self, event):
            await super()._send(reasoning_update(event, args.gbot_reasoning_effort))

        async def _local(self, path, payload=None):
            timeline.emit("credential_request" if path == "/credential" else "session_plan_request")
            result = await super()._local(path, payload)
            timeline.emit("credential_received" if path == "/credential" else "session_plan_received")
            if path == "/credential":
                benchmark_result["model"] = result.get("credential", {}).get("model")
            if path == "/session":
                instruction = PROMPT if args.prompt_mode == "neutral" else result.get("instructions", "")
                benchmark_result["configuration"].update(prompt_chars=len(instruction),
                    prompt_sha256=hashlib.sha256(instruction.encode()).hexdigest())
                result = {**result, "instructions": instruction, "tools": []}
            return result

        async def _handle_event(self, event):
            began = timeline.now()
            name = event.get("type", "")
            if name == "session.updated":
                benchmark_result["reasoning_experiment"] = reasoning_echo(event, args.gbot_reasoning_effort)
                timeline.emit("reasoning_echo", **benchmark_result["reasoning_experiment"])
            safe_names = {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                          "response.created", "response.done", "response.audio.delta",
                          "response.output_audio.delta", "session.updated",
                          "conversation.item.input_audio_transcription.completed",
                          "response.output_audio_transcript.done", "response.audio_transcript.done", "error"}
            if name in safe_names:
                timeline.emit("handler_entry", event_type=name,
                              audio_start_ms=event.get("audio_start_ms"),
                              audio_end_ms=event.get("audio_end_ms"),
                              response_id=event.get("response_id") or event.get("response", {}).get("id"))
            names = {"input_audio_buffer.speech_started": "server_speech_started",
                     "input_audio_buffer.speech_stopped": "server_speech_stopped",
                     "response.created": "server_response_created", "response.done": "server_response_done",
                     "session.updated": "server_session_updated"}
            if name in names:
                timeline.emit(names[name], response_id=event.get("response_id") or event.get("response", {}).get("id"))
            if "transcription" in name or "transcript" in name:
                text = event.get("transcript", "")
                timeline.emit("server_input_transcript" if name.startswith("conversation.") else "server_output_transcript")
                transcripts.append({"round": timeline.round, "phase": timeline.phase, "kind": name, "text": text})
            await super()._handle_event(event)
            if name in safe_names:
                timeline.emit("handler_exit", event_type=name,
                              handler_ms=round((timeline.now() - began) * 1000, 3))

    class ObservedGemini(GeminiLiveBridge):
        async def start(self):
            await super().start()
            live = self._client.aio.live
            connect = live.connect

            @asynccontextmanager
            async def observed_connect(**kwargs):
                timeline.emit("websocket_connect_start")
                async with connect(**kwargs) as session:
                    timeline.emit("websocket_connect_ready")
                    yield session
            live.connect = observed_connect

        def _note_session_end(self, error):
            if error and safe_error(error).get("status") == 1000 and self._stop_event.is_set():
                timeline.emit("intentional_shutdown_normal_close", status=1000)
                return
            if error:
                errors.append(safe_error(error))
                timeline.emit("server_connection_error", **safe_error(error))
            # Benchmark suppresses raw SDK error in global status/log output.

        async def _handle_message(self, response):
            nonlocal interrupt_recognized
            began = timeline.now()
            timeline.emit("gemini_handler_entry")
            content = getattr(response, "server_content", None)
            if content:
                if getattr(content, "interrupted", False):
                    timeline.emit("server_speech_started", source="gemini_interrupted_no_raw_vad_event",
                                  queue=proxy._frame_queue.qsize(),
                                  pcm_bytes=len(proxy._pcm_prebuffer),
                                  codec_tail_bytes=len(proxy._codec._encode_buffer),
                                  tts_active=proxy._tts_active)
                    interrupt_recognized = True
                if getattr(content, "model_turn", None):
                    timeline.emit("server_model_turn")
                for attr, event in [("input_transcription", "server_input_transcript"),
                                    ("output_transcription", "server_output_transcript")]:
                    transcription = getattr(content, attr, None)
                    if getattr(transcription, "text", None):
                        timeline.emit(event)
                        transcripts.append({"round": timeline.round, "phase": timeline.phase,
                                            "kind": event, "text": transcription.text})
                if getattr(content, "turn_complete", False):
                    timeline.emit("server_response_done")
            await super()._handle_message(response)
            timeline.emit("gemini_handler_exit", handler_ms=round((timeline.now() - began) * 1000, 3))

        async def _dispatch_tool_calls(self, calls):
            timeline.emit("unexpected_tool_call_blocked")
            # No production tools are advertised; never execute even unsolicited ones.

    def factory(*a, **kwargs):
        nonlocal bridge
        original_audio = kwargs["on_audio"]
        original_complete = kwargs["on_turn_complete"]

        async def audio(pcm):
            began = timeline.now()
            eligible = (timeline.phase != "interrupt"
                        or (backend == "gbot" and bridge._active_response != initial_response_id)
                        or (backend == "gemini" and interrupt_recognized))
            key = (timeline.round, timeline.phase)
            if eligible and key not in pcm_seen:
                timeline.emit("output_pcm_first", bytes=len(pcm), queue=proxy._frame_queue.qsize(),
                              pcm_bytes=len(proxy._pcm_prebuffer), tts_active=proxy._tts_active)
                pcm_seen.add(key)
                first_pcm.set()
            await original_audio(pcm)
            timeline.emit("on_audio_done", callback_ms=round((timeline.now() - began) * 1000, 3),
                          eligible_new_reply=eligible)

        async def complete():
            nonlocal complete_count
            timeline.emit("local_turn_complete_start")
            await original_complete()
            complete_count += 1
            timeline.emit("local_turn_complete_done")
            turn_done.set()

        kwargs.update(on_audio=audio, on_turn_complete=complete)
        if backend == "gemini":
            kwargs.update(system_instruction=PROMPT if args.prompt_mode == "neutral" else None,
                          reconnect_on_close=False,
                          conversation_idle_timeout_s=600,
                          model=os.getenv("STACKCHAN_GEMINI_MODEL", gemini_module.DEFAULT_MODEL))
            bridge = ObservedGemini(*a, **kwargs)
            instruction = bridge._system_instruction
            benchmark_result["configuration"].update(prompt_chars=len(instruction),
                prompt_sha256=hashlib.sha256(instruction.encode()).hexdigest())
        else:
            bridge = ObservedGbot(*a, local_tools=False, **kwargs)
        original_send_audio = bridge.send_audio

        async def observed_send_audio(pcm):
            began = timeline.now()
            await original_send_audio(pcm)
            timeline.emit("upstream_audio_send_done",
                          send_ms=round((timeline.now() - began) * 1000, 3), input_pcm_bytes=len(pcm))
        bridge.send_audio = observed_send_audio
        return bridge

    async def sink(message):
        if isinstance(message, bytes):
            key = (timeline.round, timeline.phase)
            if key in pcm_seen and key not in opus_seen:
                timeline.emit("output_opus_first", bytes=len(message))
                opus_seen.add(key)
                first_opus.set()
            output.extend(speaker.decode(message, 1440))
        else:
            envelope = json.loads(message)
            if envelope.get("type") == "tts" and envelope.get("state") == "stop":
                timeline.emit("tts_stop")

    async def ws_connect(session, *a, **kwargs):
        timeline.emit("websocket_connect_start")
        result = await original_ws_connect(session, *a, **kwargs)
        timeline.emit("websocket_connect_ready")
        return result

    original_ws_connect = aiohttp.ClientSession.ws_connect
    original_config = gemini_module.build_live_config

    def no_tools_config(**kwargs):
        from google.genai import types
        config = original_config(**kwargs)
        config.tools = []
        config.input_audio_transcription = types.AudioTranscriptionConfig()
        return config

    async def send_input(chunk):
        if args.input_transport == "pcm":
            await bridge.send_audio(chunk)
            return True
        return await proxy.send_device_binary(microphone.encode(chunk, 960))

    async def feed(pcm):
        first_sample, end_sample = waveform_bounds(pcm)
        first_packet, last_packet = first_sample // 960, (end_sample - 1) // 960
        timeline.emit("fixture_start", fixture_samples=len(pcm) // 2,
                      speech_start_sample=first_sample, speech_end_sample=end_sample,
                      speech_duration_ms=round((end_sample - first_sample) / 16, 3))
        feed_origin = time.monotonic()
        for packet_index, index in enumerate(range(0, len(pcm), 1920)):
            chunk = pcm[index:index + 1920].ljust(1920, b"\x00")
            began = timeline.now()
            if packet_index == first_packet:
                timeline.emit("wave_speech_start", seconds=round(began + (first_sample % 960) / 16000, 6),
                              boundary="PCM_sample_offset_on_realtime_packet_clock")
            if not await send_input(chunk):
                raise RuntimeError("Device audio frame rejected")
            ended = timeline.now()
            timeline.emit("input_frame_sent", frame=packet_index, send_ms=round((ended - began) * 1000, 3),
                          schedule_lag_ms=round((time.monotonic() - feed_origin - packet_index * .06) * 1000, 3))
            if packet_index == last_packet:
                timeline.emit("speech_packet_last_sent")
                timeline.emit("wave_speech_end", seconds=round(began + ((end_sample - 1) % 960 + 1) / 16000, 6),
                              boundary="PCM_sample_offset_on_realtime_packet_clock")
            # Preserve the initial prototype's feed pacing to expose accumulated send overhead.
            await asyncio.sleep(.06)
        timeline.emit("fixture_send_done")
        for _ in range(30):
            await send_input(bytes(1920))
            await asyncio.sleep(.06)
        timeline.emit("trailing_silence_sent")

    result = {"backend": backend, "configuration": {"vad": "server", "wake_gate": False,
              "tools": False, "prompt_mode": args.prompt_mode,
              "gbot_reasoning_effort": args.gbot_reasoning_effort if backend == "gbot" else None,
              "input_transport": args.input_transport,
              "input": "real_16k_opus" if args.input_transport == "opus" else "direct_PCM16_to_bridge_not_device_input",
              "output": "real_24k_opus_fake_device", "fixture_feed": "await_send_then_sleep_60ms"},
              "scope": "no_robot_no_gateway_restart",
              "event_timestamp_boundary": "handler_entry_after_json_decode_not_socket_raw_recv",
              "opus_attribution": "first_frame_after_new_PCM_may_contain_stale_FIFO_audio_if_cancel_fails",
              "gemini_vad_event": "interrupted_proxy_only_raw_speech_started_unavailable", "rounds": []}
    if backend == "gbot":
        result["reasoning_experiment"] = {"requested": args.gbot_reasoning_effort,
                                          "echo": None, "supported": None}
    benchmark_result = result
    initial = read_pcm(args.input_wav)
    interruption = read_pcm(args.interrupt_wav)
    result["fixtures"] = {"input_pcm_sha256": hashlib.sha256(initial).hexdigest(),
                          "interrupt_pcm_sha256": hashlib.sha256(interruption).hexdigest()}
    patches = [patch.object(aiohttp.ClientSession, "ws_connect", ws_connect),
               patch.object(gemini_module, "build_live_config", no_tools_config)]
    for item in patches:
        item.start()
    try:
        fake_esp = SimpleNamespace(device_connected=False)
        proxy_cls = GeminiVoiceProxy if backend == "gemini" else GbotVoiceProxy
        proxy = proxy_cls(esp32_ref=lambda: fake_esp, bridge_factory=factory,
                          wake_gate_factory=lambda: None)
        original_abort = proxy._abort_in_flight_tts

        async def observed_abort():
            timeline.emit("local_abort_start")
            await original_abort()
            queue = proxy._frame_queue.qsize()
            pcm = len(proxy._pcm_prebuffer)
            tail = len(proxy._codec._encode_buffer)
            timeline.emit("queue_zero" if queue == pcm == tail == 0 else "queue_not_zero",
                          queue=queue, pcm_bytes=pcm, codec_tail_bytes=tail)
        proxy._abort_in_flight_tts = observed_abort
        timeline.emit("proxy_start")
        ready = await proxy.start(SimpleNamespace(device_id="latency-benchmark"), {}, sink)
        timeline.emit("proxy_ready", ready=ready)
        result["connected"] = ready
        if not ready:
            if backend == "gemini" and not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
                result["unavailable_reason"] = "missing_GEMINI_API_KEY_or_GOOGLE_API_KEY"
            raise RuntimeError("Voice proxy connection failed")
        if backend == "gemini":
            result["model"] = bridge._model
        elif (args.gbot_reasoning_effort != "default"
              and result.get("reasoning_experiment", {}).get("supported") is not True):
            raise RuntimeError("Reasoning experiment has no matching server echo")
        for index in range(args.warm_rounds + 1):
            timeline.round = index
            timeline.phase = "initial"
            first_pcm.clear()
            first_opus.clear()
            turn_done.clear()
            start = len(timeline.events)
            await feed(initial)
            await asyncio.wait_for(first_opus.wait(), args.timeout)
            await asyncio.sleep(.3)
            initial_response_id = getattr(bridge, "_active_response", None)
            interrupt_recognized = False
            timeline.phase = "interrupt"
            first_pcm.clear()
            first_opus.clear()
            turn_done.clear()
            await feed(interruption)
            await asyncio.wait_for(first_opus.wait(), args.timeout)
            # Let the actual proxy drain. No artificial abort substitutes for audio barge-in.
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                if not proxy._tts_active and proxy._frame_queue.empty() and turn_done.is_set():
                    break
                await asyncio.sleep(.05)
            if time.monotonic() >= deadline:
                timeline.emit("round_drain_timeout")
                raise TimeoutError("Round drain timeout")
            rows = timeline.events[start:]
            result["rounds"].append({"round": index, "session_state": "cold" if index == 0 else "warm",
                                      "timings": summarize_round(rows),
                                      "pipeline_timing_ms": summarize_pipeline(rows),
                                      "interrupt_semantic_match": interruption_semantics(transcripts, index)})
            write_checkpoint(args.output_dir / backend, result, timeline.events)
            await asyncio.sleep(.5)
    except Exception as error:
        errors.append(safe_error(error))
        timeline.emit("benchmark_error", **safe_error(error))
    finally:
        if proxy:
            await proxy.stop()
        for item in reversed(patches):
            item.stop()
        result["events"] = timeline.events
        result["pipeline_timing_ms"] = summarize_pipeline(timeline.events)
        warm = [r["timings"] for r in result["rounds"] if r["session_state"] == "warm"]
        result["warm_aggregate_ms"] = {}
        for metric in summarize_round([]):
            values = [r[metric] for r in warm if r.get(metric) is not None]
            if values:
                result["warm_aggregate_ms"][metric] = {"samples": len(values),
                    "median": round(statistics.median(values), 3), "min": min(values), "max": max(values)}
        result["passed"], result["failures"] = assess_result(result, args, errors)
        result["errors"] = errors
        result["fixture_transcripts"] = transcripts
        result["cold_connect_ms"] = duration(timeline.events, "proxy_start", "proxy_ready")
        result["decoded_audio_seconds"] = round(len(output) / 48000, 3)
        if backend == "gbot" and bridge:
            result["bridge_metrics"] = bridge.metrics
        send_ms = [e["send_ms"] for e in timeline.events if e["name"] == "input_frame_sent"]
        result["send_frame_ms"] = {"median": statistics.median(send_ms), "max": max(send_ms)} if send_ms else None
        directory = args.output_dir / backend
        directory.mkdir(parents=True, exist_ok=True)
        with wave.open(str(directory / "received.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(output)
        (directory / "summary.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k not in {"events", "fixture_transcripts"}}, indent=2))
    return result


async def main_async(args):
    load_credentials(args.env_file, args.launchd_plist)
    os.environ["STACKCHAN_MANUAL_VAD"] = "0"
    os.environ["STACKCHAN_GBOT_VOICE_TOOLS"] = "0"
    os.environ["STACKCHAN_GEMINI_TTS"] = "native"
    os.environ["STACKCHAN_GBOT_VOICE_LANGUAGE"] = args.language
    # Runtime log suppression prevents SDK/legacy debug strings leaking secrets.
    logging.disable(logging.CRITICAL)
    results = []
    for backend in args.backends.split(","):
        results.append(await benchmark(args, backend))
    return 0 if all(r.get("passed") for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backends", choices=["gemini", "gbot", "gemini,gbot", "gbot,gemini"], default="gemini,gbot")
    parser.add_argument("--input-wav", type=Path, required=True)
    parser.add_argument("--interrupt-wav", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--warm-rounds", type=int, default=3)
    parser.add_argument("--prompt-mode", choices=["neutral", "native"], default="neutral")
    parser.add_argument("--input-transport", choices=["opus", "pcm"], default="opus")
    parser.add_argument("--gbot-reasoning-effort", choices=["default", "none", "high"], default="default")
    parser.add_argument("--require-interruption", action="store_true")
    parser.add_argument("--max-interrupt-detection-ms", type=float)
    parser.add_argument("--max-initial-ttfa-ms", type=float)
    parser.add_argument("--max-interrupt-ttfa-ms", type=float)
    parser.add_argument("--max-local-stop-ms", type=float)
    parser.add_argument("--timeout", type=float, default=40)
    parser.add_argument("--language", default="en")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--launchd-plist", type=Path,
                        default=Path.home() / "Library/LaunchAgents/com.stackchan.gateway.plist")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
