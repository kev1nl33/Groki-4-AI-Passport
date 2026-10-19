#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Fake ``gbot`` CLI for tests: keeps thread entries in a state file.

FAKE_GBOT_EXTRA_REPLY adds a second bot message after each reply, to cover
agents that answer "on it" first and send the result as a later message.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _state_path() -> Path:
    raw = os.environ.get("FAKE_GBOT_STATE", "").strip()
    if not raw:
        print("FAKE_GBOT_STATE is required", file=sys.stderr)
        raise SystemExit(2)
    return Path(raw)


def _with_state(mutator):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text('{"entries": [], "seq": 0}', encoding="utf-8")
    with path.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read() or '{"entries": [], "seq": 0}'
            data = json.loads(raw)
            result = mutator(data)
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
            return result
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] == "--json":
        args = args[1:]
    if not args:
        print("missing command", file=sys.stderr)
        return 2

    if args[0] == "thread":
        if os.environ.get("FAKE_GBOT_THREAD_FAIL", "").strip() in {"1", "true", "yes"}:
            print("not authenticated", file=sys.stderr)
            return 1
        delay = float(os.environ.get("FAKE_GBOT_THREAD_DELAY_S", "0") or "0")
        if delay > 0:
            time.sleep(delay)

        def read_entries(data: dict[str, Any]) -> list[Any]:
            return list(data.get("entries") or [])

        entries = _with_state(read_entries)
        _dump({"transcript": {"entries": entries}})
        return 0

    if args[0] == "send":
        if len(args) < 3:
            print("usage: send BOT TEXT", file=sys.stderr)
            return 2
        text = args[2]
        configured_delay = float(os.environ.get("FAKE_GBOT_SEND_DELAY_S", "0") or "0")
        lock_path = _state_path().with_name(_state_path().name + ".sendlock")
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                def take_delay(data: dict[str, Any]) -> float:
                    leftover = float(data.get("send_delay_s", configured_delay) or 0)
                    data["send_delay_s"] = 0.0
                    return leftover

                leftover = _with_state(take_delay)
                if leftover > 0:
                    time.sleep(leftover)

                def append_reply(data: dict[str, Any]) -> None:
                    seq = int(data.get("seq") or 0) + 1
                    data["seq"] = seq
                    entries = list(data.get("entries") or [])
                    entries.append(
                        {
                            "kind": "send-message",
                            "id": f"e{seq}",
                            "message": {"content": f"reply-{text}。"},
                            "timestampMs": int(time.time() * 1000),
                        }
                    )
                    extra = os.environ.get("FAKE_GBOT_EXTRA_REPLY", "")
                    if extra:
                        seq += 1
                        data["seq"] = seq
                        entries.append(
                            {
                                "kind": "send-message",
                                "id": f"e{seq}",
                                "message": {"content": extra},
                                "timestampMs": int(time.time() * 1000),
                            }
                        )
                    data["entries"] = entries

                _with_state(append_reply)
                _dump({"result": {"accepted": True}})
                return 0
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    print(f"unknown command: {args[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
