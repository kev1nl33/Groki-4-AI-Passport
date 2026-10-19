# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Offline shape/privacy checks; these tests never request a voice credential."""

import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts/probe_gbot_voice_capabilities.py"
)
spec = importlib.util.spec_from_file_location("gbot_capability_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_reasoning_trials_change_only_one_parameter_and_keep_account_model():
    credential = {
        "websocketUrl": "wss://api.x.ai/v1/realtime",
        "model": "grok-voice-synapse",
    }
    plan = {"instructions": "PRIVATE_FAKE_INSTRUCTIONS"}
    baseline_url, baseline = probe.trial_config(credential, plan, "baseline")
    query_url, query_config = probe.trial_config(
        credential, plan, "query-reasoning-none"
    )
    session_url, session_config = probe.trial_config(
        credential, plan, "session-reasoning-none"
    )
    assert baseline == query_config
    assert session_url == baseline_url
    assert parse_qs(urlsplit(query_url).query) == {
        "model": ["grok-voice-synapse"],
        "reasoning.effort": ["none"],
    }
    assert session_config.pop("reasoning") == {"effort": "none"}
    assert session_config == baseline
    assert "response.create" not in str(baseline)
    assert baseline["tools"] == []


def test_echo_and_error_redaction_never_include_unknown_fields_or_error_messages():
    assert probe.safe_echo(
        {
            "session": {
                "instructions": "PRIVATE",
                "client_secret": "PRIVATE",
                "reasoning": {"effort": "none", "secret": "PRIVATE"},
                "turn_detection": {
                    "type": "server_vad",
                    "idle_timeout_ms": 3000,
                    "unknown": "PRIVATE",
                },
            }
        }
    ) == {
        "reasoning": {"effort": "none"},
        "turn_detection": {"type": "server_vad", "idle_timeout_ms": 3000},
    }
    assert probe.safe_echo({"session": {"reasoning": {"effort": "PRIVATE"}}}) == {}
    assert (
        probe.safe_error_code({"error": {"code": "PRIVATE", "message": "PRIVATE"}})
        == "other"
    )
    assert (
        probe.safe_error_code({"error": {"code": "invalid_request_error"}})
        == "invalid_request_error"
    )


def test_vad_trials_change_only_requested_field_and_cannot_confirm_from_reasoning():
    credential = {
        "websocketUrl": "wss://api.x.ai/v1/realtime",
        "model": "grok-voice-synapse",
    }
    plan = {"instructions": "PRIVATE_FAKE_INSTRUCTIONS"}
    baseline_url, baseline = probe.trial_config(credential, plan, "baseline")
    for candidate, field, value in [
        ("vad-threshold-0.3", "threshold", 0.3),
        ("vad-silence-250", "silence_duration_ms", 250),
    ]:
        url, config = probe.trial_config(credential, plan, candidate)
        assert probe.candidate_status(candidate) == "unsupported_candidate"
        assert url == baseline_url
        assert config["turn_detection"].pop(field) == value
        assert config == baseline
        assert (
            probe.support_for_echo(candidate, {"reasoning": {"effort": "none"}})
            == "not_confirmed"
        )
        assert (
            probe.support_for_echo(candidate, {"turn_detection": {field: value}})
            == "confirmed_echo_only"
        )
        assert (
            probe.support_for_echo(candidate, {"turn_detection": {field: value + 0.01}})
            == "not_confirmed"
        )


def test_vad_echo_numbers_are_bounded_and_booleans_or_strings_do_not_count():
    assert probe.safe_echo(
        {
            "session": {
                "turn_detection": {
                    "threshold": 0.3,
                    "silence_duration_ms": 250,
                    "prefix_padding_ms": 100,
                }
            }
        }
    ) == {
        "turn_detection": {
            "threshold": 0.3,
            "silence_duration_ms": 250,
            "prefix_padding_ms": 100,
        }
    }
    for value in [-1, 1.1, float("nan"), float("inf"), True, "0.3"]:
        assert (
            probe.safe_echo({"session": {"turn_detection": {"threshold": value}}}) == {}
        )
    for field in ["silence_duration_ms", "prefix_padding_ms"]:
        for value in [-1, 10001, True, "250", float("inf")]:
            assert (
                probe.safe_echo({"session": {"turn_detection": {field: value}}}) == {}
            )
