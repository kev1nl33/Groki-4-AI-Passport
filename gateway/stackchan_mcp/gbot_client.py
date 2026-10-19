# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Grok Bot client built on the external ``gbot`` CLI (standard library only).

``gbot`` comes from the npm package ``grok-bot-cli`` and uses the signed-in
Grok Bot app session on this computer. This module only runs
``gbot --json send`` and ``gbot --json thread``; it never reads or stores a
token. Used by :mod:`stackchan_mcp.gbot_http_proxy`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

SEND_TIMEOUT_DEFAULT_S = 20.0
SEND_RETRY_ATTEMPTS = 2
SEND_RETRY_SLEEP_S = 0.4


def send_timeout_s() -> float:
    """Timeout of one gbot subprocess. A send is one direct send plus two client retries."""
    raw = os.getenv("STACKCHAN_GBOT_SEND_TIMEOUT_S", str(SEND_TIMEOUT_DEFAULT_S))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return SEND_TIMEOUT_DEFAULT_S
    return value if value > 0 else SEND_TIMEOUT_DEFAULT_S


class AskError(RuntimeError):
    """The exchange failed. Callers must not treat this error text as the bot's reply."""


def resolve_gbot_bin() -> str | None:
    """Locate the gbot executable, from the environment variable or PATH only."""
    override = (
        os.environ.get("STACKCHAN_GBOT_BIN") or os.environ.get("STACKCHAN_ASK_GBOT") or ""
    ).strip()
    if override:
        expanded = os.path.expanduser(override)
        if Path(expanded).is_file():
            return expanded
        found = shutil.which(override)
        return found
    return shutil.which("gbot")


def gbot_missing_error() -> str:
    override = (
        os.environ.get("STACKCHAN_GBOT_BIN") or os.environ.get("STACKCHAN_ASK_GBOT") or ""
    ).strip()
    if override:
        return f"gbot not found: {override} (check STACKCHAN_GBOT_BIN)"
    return "gbot not found; install grok-bot-cli and put it on PATH, or set STACKCHAN_GBOT_BIN"


def extract_bot_replies(thread: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract the kind=send-message replies from gbot --json thread."""
    payload = thread.get("transcript") or thread.get("thread") or thread
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "send-message":
            continue
        eid = str(entry.get("id") or "")
        message = entry.get("message")
        text = ""
        if isinstance(message, dict):
            text = str(message.get("content") or "")
        elif isinstance(entry.get("content"), str):
            text = entry["content"]
        out.append((eid, text.strip()))
    return out


class GrokBotClient:
    def __init__(self, gbot: str | None = None, timeout_s: float | None = None) -> None:
        self.gbot = gbot or resolve_gbot_bin()
        self.timeout_s = send_timeout_s() if timeout_s is None else timeout_s
        if not self.gbot:
            raise AskError(gbot_missing_error())

    def _run(self, args: list[str]) -> dict[str, Any]:
        last_error = "gbot failed"
        for attempt in range(SEND_RETRY_ATTEMPTS):
            try:
                proc = subprocess.run(
                    [self.gbot, "--json", *args],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = f"gbot could not run: {exc}"
                if attempt + 1 < SEND_RETRY_ATTEMPTS:
                    time.sleep(SEND_RETRY_SLEEP_S)
                    continue
                raise AskError(last_error) from exc
            if proc.returncode == 0:
                try:
                    payload = json.loads(proc.stdout)
                except json.JSONDecodeError as exc:
                    raise AskError("gbot returned JSON that cannot be parsed") from exc
                if not isinstance(payload, dict):
                    raise AskError("gbot returned JSON that is not an object")
                return payload
            last_error = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            if attempt + 1 < SEND_RETRY_ATTEMPTS:
                time.sleep(SEND_RETRY_SLEEP_S)
                continue
        raise AskError(f"gbot failed: {last_error}")

    def send(self, bot: str, text: str) -> None:
        payload = self._run(["send", bot, text])
        result = payload.get("result")
        accepted = True
        if isinstance(result, dict):
            accepted = bool(result.get("accepted", True))
        if not accepted:
            raise AskError("gbot did not accept the message")

    def thread(self, bot: str) -> dict[str, Any]:
        return self._run(["thread", bot, "--limit", "20"])
