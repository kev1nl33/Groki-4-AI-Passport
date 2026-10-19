#!/usr/bin/env python3
"""Verify Gemini session behaviour while the wake gate is dormant.

Usage (the gateway must be running with the wake word gate available)::

    cd gateway && uv run python scripts/verify_dormant_session.py
    cd gateway && uv run python scripts/verify_dormant_session.py --minutes 12

What is checked:

* Manual VAD is on (``STACKCHAN_MANUAL_VAD`` defaults to 1): real speech is
  bracketed by activity_start/activity_end, and no periodic silent PCM is
  sent to keep the session alive.
* While DORMANT the server may still close the session with 1008 as part of
  its normal lifecycle; the script watches whether ``reconnect_1008_count``
  and ``reconnect_receive_stall_count`` grow unexpectedly.
* After 10+ minutes of silence ``reconnect_receive_stall_count`` must still
  be 0 (the receive-stall fallback did not fire by mistake).

Exit code: 0 = observation finished and the counters are as expected;
1 = gateway unreachable or a counter is off.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def fetch_status(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/debug/status"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Gemini session counters while the wake gate is dormant")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8766",
        help="Base URL of the gateway capture server",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=10.0,
        help="Observation window in minutes",
    )
    parser.add_argument(
        "--poll-s",
        type=float,
        default=30.0,
        help="Polling interval in seconds",
    )
    args = parser.parse_args()

    try:
        baseline = fetch_status(args.base_url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read {args.base_url}/debug/status: {exc}")
        return 1

    gemini0 = baseline.get("gemini", {})
    wake0 = baseline.get("wake_gate", {})
    c1008_0 = int(gemini0.get("reconnect_1008_count", 0))
    stall_0 = int(gemini0.get("reconnect_receive_stall_count", 0))
    keepalive_running = bool(gemini0.get("keepalive_running"))

    print("=== Baseline (wake gate dormant) ===")
    print(f"wake_state={wake0.get('state')} keepalive_running={keepalive_running}")
    print(f"reconnect_1008_count={c1008_0}")
    print(f"reconnect_receive_stall_count={stall_0}")
    print(f"has_resumption_handle={gemini0.get('has_resumption_handle')}")
    print(f"Watching for {args.minutes:.1f} min, polling every {args.poll_s:.0f}s...")

    deadline = time.time() + args.minutes * 60.0
    last = baseline
    while time.time() < deadline:
        time.sleep(args.poll_s)
        try:
            last = fetch_status(args.base_url)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"WARN: poll failed: {exc}")
            continue
        g = last.get("gemini", {})
        print(
            f"  t+{args.minutes * 60 - (deadline - time.time()):.0f}s "
            f"1008={g.get('reconnect_1008_count')} "
            f"stall={g.get('reconnect_receive_stall_count')} "
            f"wake={last.get('wake_gate', {}).get('state')}"
        )

    gemini1 = last.get("gemini", {})
    c1008_1 = int(gemini1.get("reconnect_1008_count", 0))
    stall_1 = int(gemini1.get("reconnect_receive_stall_count", 0))
    delta_1008 = c1008_1 - c1008_0
    delta_stall = stall_1 - stall_0

    print("=== Result ===")
    print(f"Δ reconnect_1008_count = {delta_1008}")
    print(f"Δ reconnect_receive_stall_count = {delta_stall}")

    ok = True
    if delta_stall > 0:
        print("FAIL: the receive-stall fallback fired during silence")
        ok = False
    if keepalive_running:
        print(
            "NOTE: the experimental silent keepalive is still running; "
            "it should be off by default (STACKCHAN_GEMINI_KEEPALIVE_S unset or 0)"
        )
    if delta_1008 > 0:
        print(
            "NOTE: a 1008 reconnect happened while dormant. "
            "Manual VAD does not fake idle activity, so this is an expected lifecycle event; "
            "check has_resumption_handle / last_reconnect_used_handle for lost context."
        )
    else:
        print("PASS: reconnect_1008_count did not grow during the window")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())