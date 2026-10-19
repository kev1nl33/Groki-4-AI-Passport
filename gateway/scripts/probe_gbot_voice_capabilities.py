#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Probe signed-in Grok Bot session capabilities without sending speech or text.

Dry-run by default. --live opens exactly one temporary account-authenticated
WebSocket, sends session.update, records whitelisted echoes, then closes it.
No API key, response.create, audio append, tool call, or model override is used.

Official parameter references (synapse support still needs live confirmation):
https://docs.x.ai/developers/rest-api-reference/inference/voice
https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from stackchan_mcp.gbot_voice_bridge import gather_with_cancel, voice_url_with_model

BRIDGE_URL = "http://127.0.0.1:18771"
CANDIDATES = (
    "baseline",
    "query-reasoning-none",
    "session-reasoning-none",
    "vad-threshold-0.3",
    "vad-silence-250",
)
# These VAD fields are NOT documented for synapse or current xAI Realtime.
# They are unsupported candidates until exact server echo and audio A/B evidence.
UNSUPPORTED_CANDIDATES = {"vad-threshold-0.3", "vad-silence-250"}
ERROR_CODES = {
    "invalid_request_error",
    "invalid_value",
    "unknown_parameter",
    "authentication_error",
    "session_expired",
    "rate_limit_exceeded",
    "server_error",
}


def trial_config(credential: dict, plan: dict, candidate: str) -> tuple[str, dict]:
    """Each fixed candidate changes only one parameter; none changes the model."""
    if candidate not in CANDIDATES:
        raise ValueError("unknown_candidate")
    url = voice_url_with_model(credential["websocketUrl"], credential["model"])
    config = {
        "instructions": plan["instructions"],
        "resumption": {"enabled": True},
        "turn_detection": {"type": "server_vad"},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "grok-transcribe", "language_hint": "zh"},
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        },
        "tools": [],
    }
    if candidate == "query-reasoning-none":
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query))
        query["reasoning.effort"] = "none"
        url = urlunsplit(parsed._replace(query=urlencode(query)))
    elif candidate == "session-reasoning-none":
        config["reasoning"] = {"effort": "none"}
    elif candidate == "vad-threshold-0.3":
        config["turn_detection"]["threshold"] = 0.3
    elif candidate == "vad-silence-250":
        config["turn_detection"]["silence_duration_ms"] = 250
    return url, config


def safe_echo(event: dict) -> dict:
    """Never copy session dictionaries, upstream text, or arbitrary error values."""
    result = {}
    session = event.get("session")
    if not isinstance(session, dict):
        return result
    reasoning = session.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") in {"high", "none"}:
        result["reasoning"] = {"effort": reasoning["effort"]}
    turn = session.get("turn_detection")
    if isinstance(turn, dict):
        safe_turn = {}
        if turn.get("type") == "server_vad" or (
            "type" in turn and turn["type"] is None
        ):
            safe_turn["type"] = turn["type"]
        idle = turn.get("idle_timeout_ms")
        if type(idle) is int and 0 <= idle <= 3_600_000:
            safe_turn["idle_timeout_ms"] = idle
        threshold = turn.get("threshold")
        if type(threshold) in {int, float} and 0 <= threshold <= 1:
            safe_turn["threshold"] = threshold
        for field in ("silence_duration_ms", "prefix_padding_ms"):
            value = turn.get(field)
            if type(value) in {int, float} and 0 <= value <= 10_000:
                safe_turn[field] = value
        if safe_turn:
            result["turn_detection"] = safe_turn
    elif "turn_detection" in session and turn is None:
        result["turn_detection"] = None
    return result


def support_for_echo(candidate: str, echo: dict) -> str:
    """An ACK or an unrelated field echo never confirms the requested knob."""
    if candidate == "baseline":
        return "baseline_only"
    if candidate in {"query-reasoning-none", "session-reasoning-none"}:
        matched = echo.get("reasoning", {}).get("effort") == "none"
    else:
        turn = echo.get("turn_detection")
        turn = turn if isinstance(turn, dict) else {}
        matched = (
            candidate == "vad-threshold-0.3"
            and turn.get("threshold") == 0.3
            or candidate == "vad-silence-250"
            and turn.get("silence_duration_ms") == 250
        )
    return "confirmed_echo_only" if matched else "not_confirmed"


def candidate_status(candidate: str) -> str:
    if candidate == "baseline":
        return "baseline"
    return (
        "unsupported_candidate"
        if candidate in UNSUPPORTED_CANDIDATES
        else "documented_parameter_synapse_unverified"
    )


def safe_error_code(event: dict) -> str:
    error = event.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in ERROR_CODES else "other"


async def run_trial(candidate: str, timeout: float = 10.0) -> dict:
    report = {
        "candidate": candidate,
        "candidate_status": candidate_status(candidate),
        "live": True,
        "session_updated": False,
        "support": "not_confirmed",
        "errors": [],
    }
    trace = aiohttp.TraceConfig()

    async def reject_redirect(*_args):
        raise RuntimeError("redirect_forbidden")

    trace.on_request_redirect.append(reject_redirect)
    try:
        async with aiohttp.ClientSession(
            trust_env=False,
            trace_configs=[trace],
            timeout=aiohttp.ClientTimeout(total=30),
        ) as http:

            async def local(path, body=None):
                async with http.request(
                    "GET" if body is None else "POST",
                    BRIDGE_URL + path,
                    json=body,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise RuntimeError("local_request_failed")
                    value = await response.json()
                    if value.get("ok") is False:
                        raise RuntimeError("local_request_rejected")
                    return value

            health = await local("/health")
            agent_id = health.get("agentId")
            if not isinstance(agent_id, str) or not agent_id:
                raise RuntimeError("bridge_agent_not_configured")
            # Each candidate mints a fresh ephemeral secret. Login state and the
            # account's chosen model remain owned by Grok Bot and its local bridge.
            minted, session = await gather_with_cancel(
                local("/credential"),
                local("/session", {
                    "agentId": agent_id,
                    "agentName": "Groki",
                    "spokenLanguage": "zh",
                }),
            )
            credential = minted["credential"]
            if float(credential["expiresAtUnixSeconds"]) < time.time() + 5:
                raise RuntimeError("ephemeral_expired")
            url, config = trial_config(credential, session, candidate)
            async with http.ws_connect(
                url,
                protocols=["xai-client-secret." + credential["clientSecret"]],
                max_msg_size=2 * 1024 * 1024,
                heartbeat=20,
            ) as ws:
                await ws.send_json({"type": "session.update", "session": config})
                async def receive_echo():
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        event = json.loads(message.data)
                        if event.get("type") == "session.created":
                            report["created_echo"] = safe_echo(event)
                        elif event.get("type") == "session.updated":
                            report["session_updated"] = True
                            echo = safe_echo(event)
                            report["updated_echo"] = echo
                            report["support"] = support_for_echo(candidate, echo)
                            # An ACK without the exact requested value is NOT
                            # proof that this parameter is honored for synapse.
                            break
                        elif event.get("type") == "error":
                            report["errors"].append(safe_error_code(event))
                            break
                await asyncio.wait_for(receive_echo(), timeout)
    except Exception as error:
        # No exception strings: authenticated request URLs and response bodies
        # can appear inside aiohttp errors. Restrict metadata to numeric status.
        status = getattr(error, "status", None)
        if type(status) is int:
            report["http_status"] = status
        report["failure"] = (
            "timeout" if isinstance(error, (asyncio.TimeoutError, TimeoutError)) else "request_failed"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=CANDIDATES, default="baseline")
    parser.add_argument(
        "--live", action="store_true", help="Open one no-audio account voice handshake"
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not args.live:
        report = {
            "candidate": args.candidate,
            "candidate_status": candidate_status(args.candidate),
            "live": False,
            "support": "not_tested",
        }
    else:
        report = asyncio.run(
            run_trial(args.candidate, max(1.0, min(args.timeout, 30.0)))
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
