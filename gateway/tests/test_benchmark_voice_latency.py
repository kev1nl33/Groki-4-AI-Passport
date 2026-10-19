# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Timing accounting tests, with no network calls or production configuration."""
import importlib.util
from pathlib import Path
import struct
import sys

import pytest

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("benchmark_voice_latency", scripts / "benchmark_voice_latency.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_fixture_speech_excludes_leading_and_trailing_silence():
    pcm = struct.pack("<8h", 0, 0, 200, -250, 180, 0, 0, 0)
    assert bench.waveform_bounds(pcm) == (2, 5)
    with pytest.raises(ValueError, match="no audible"):
        bench.waveform_bounds(bytes(1920))


def test_detection_during_speech_is_not_hidden_or_called_full_utterance_latency():
    rows = [{"name": name, "seconds": value, "phase": "interrupt"} for name, value in [
        ("wave_speech_start", 10), ("server_speech_started", 10.2),
        ("tts_stop", 10.21), ("queue_zero", 10.211),
        ("wave_speech_end", 15), ("speech_packet_last_sent", 15),
        ("output_pcm_first", 15.8), ("output_opus_first", 16.08)]]
    result = bench.summarize_round(rows)
    assert result["interrupt_speech_start_to_server_detection_ms"] == 200
    assert result["interrupt_speech_end_to_server_detection_ms"] == -4800
    assert result["server_detection_to_local_stop_ms"] == 10
    assert result["server_detection_to_queue_zero_ms"] == 11
    assert result["interrupt_postutterance_first_pcm_ms"] == 800
    assert result["interrupt_fixture_speech_ms"] == 5000


def test_missing_server_event_is_null_not_inferred_from_local_callback():
    assert bench.summarize_round([])["server_detection_to_local_stop_ms"] is None


def test_error_strings_never_reach_artifact():
    class FixtureError(Exception):
        status_code = 429
    assert bench.safe_error(FixtureError("secret-url?key=DO_NOT_PRINT")) == {
        "type": "FixtureError", "status": 429}


def test_assertions_fail_null_or_incomplete_timings():
    from types import SimpleNamespace
    args = SimpleNamespace(warm_rounds=0, require_interruption=True,
        max_initial_ttfa_ms=None, max_interrupt_detection_ms=1500,
        max_interrupt_ttfa_ms=None, max_local_stop_ms=None)
    result = {"connected": True, "rounds": [{"round": 0, "timings": {}}]}
    passed, failures = bench.assess_result(result, args, [])
    assert not passed
    assert "round_0_missing_server_detection_to_local_stop_ms" in failures
    assert "round_0_limit_interrupt_speech_start_to_server_detection_ms" in failures
    passed, failures = bench.assess_result({"connected": True, "rounds": []}, args, [])
    assert not passed
    assert "incomplete_rounds" in failures


def test_checkpoint_whitelist_excludes_credentials_and_context(tmp_path):
    import json
    result = {"backend": "gbot", "model": "fixture-model", "configuration": {"prompt_mode": "native"},
              "rounds": [{"round": 0, "timings": {"first_pcm_ms": 100}}],
              "credential": "SECRET_FIXTURE", "instructions": "PRIVATE_CONTEXT", "fixture_transcripts": ["TEXT"]}
    bench.write_checkpoint(tmp_path, result, [])
    text = (tmp_path / "summary.partial.json").read_text()
    assert "SECRET_FIXTURE" not in text and "PRIVATE_CONTEXT" not in text
    assert "fixture_transcripts" not in text
    assert json.loads(text)["completed_rounds"] == 1
    assert not (tmp_path / ".summary.partial.json.tmp").exists()


def test_pipeline_summary_exposes_send_drift_and_blocking_callbacks():
    rows = [{"name": "input_frame_sent", "send_ms": 2.0, "schedule_lag_ms": 3.0},
            {"name": "input_frame_sent", "send_ms": 4.0, "schedule_lag_ms": 12.0},
            {"name": "handler_exit", "handler_ms": 4000.0},
            {"name": "gemini_handler_exit", "handler_ms": 8000.0},
            {"name": "on_audio_done", "callback_ms": 80.0}]
    summary = bench.summarize_pipeline(rows)
    assert summary["proxy_or_direct_input_send"]["max"] == 4.0
    assert summary["input_schedule_lag"]["last"] == 12.0
    assert summary["input_schedule_lag"]["max"] == 12.0
    assert summary["server_handler"]["max"] == 8000.0
    assert summary["on_audio_callback"]["max"] == 80.0


def test_reasoning_experiment_only_updates_session_without_changing_model():
    event = {"type": "session.update", "session": {"model": "fixed-fixture", "voice": "fixture-voice"}}
    assert bench.reasoning_update(event, "default") is event
    result = bench.reasoning_update(event, "none")
    assert result["session"] == {"model": "fixed-fixture", "voice": "fixture-voice", "reasoning": {"effort": "none"}}
    assert "reasoning" not in event["session"]
    audio = {"type": "input_audio_buffer.append", "audio": "fixture"}
    assert bench.reasoning_update(audio, "none") is audio


def test_reasoning_requires_matching_actual_session_echo():
    assert bench.reasoning_echo({"session": {"reasoning": {"effort": "none"}}}, "none")["supported"] is True
    assert bench.reasoning_echo({"session": {}}, "none")["supported"] is False
    assert bench.reasoning_echo({"session": {"reasoning": {"effort": "high"}}}, "none")["supported"] is False
    assert bench.reasoning_echo({"session": {"reasoning": {"effort": "SECRET_FIXTURE"}}}, "none")["echo"] == "other"
    from types import SimpleNamespace
    args = SimpleNamespace(warm_rounds=-1, require_interruption=False, gbot_reasoning_effort="none",
        max_initial_ttfa_ms=None, max_interrupt_detection_ms=None,
        max_interrupt_ttfa_ms=None, max_local_stop_ms=None)
    passed, failures = bench.assess_result({"backend": "gbot", "connected": True, "rounds": []}, args, [])
    assert not passed
    assert "reasoning_effort_not_confirmed_by_session_echo" in failures


def test_semantic_gate_joins_gemini_fragments_but_excludes_input_and_other_rounds():
    rows = [{"round": 0, "phase": "interrupt", "kind": "server_output_transcript", "text": "Interruption "},
            {"round": 0, "phase": "interrupt", "kind": "server_output_transcript", "text": "successful."},
            {"round": 1, "phase": "interrupt", "kind": "server_input_transcript", "text": "Interruption successful"}]
    assert bench.interruption_semantics(rows, 0)
    assert not bench.interruption_semantics(rows, 1)
    assert not bench.interruption_semantics(rows, 2)
    assert bench.interruption_semantics([{"round": 2, "phase": "interrupt",
        "kind": "response.output_audio_transcript.done", "text": "Interruption successful."}], 2)


def test_semantic_gate_is_required_even_when_all_timings_pass():
    from types import SimpleNamespace
    args = SimpleNamespace(warm_rounds=0, require_interruption=True, gbot_reasoning_effort="default",
        max_initial_ttfa_ms=None, max_interrupt_detection_ms=None,
        max_interrupt_ttfa_ms=None, max_local_stop_ms=None)
    timings = {name: 1.0 for name in bench.summarize_round([])}
    row = {"round": 0, "timings": timings, "interrupt_semantic_match": False}
    result = {"backend": "gbot", "connected": True, "rounds": [row]}
    assert not bench.assess_result(result, args, [])[0]
    row["interrupt_semantic_match"] = True
    assert bench.assess_result(result, args, [])[0]
