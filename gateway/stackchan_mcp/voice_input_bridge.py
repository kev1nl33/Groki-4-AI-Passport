"""Safe STT-to-cmux bridge for StackChan voice input."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

_TEXT_KEYS = ("text", "content", "asr_text", "transcript")
_SYMBOL_RE = re.compile(r"^[\W_]+$", re.UNICODE)


@dataclass(frozen=True)
class VoiceDispatchResult:
    ok: bool
    text: str = ""
    reason: str = ""
    queued: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "reason": self.reason, "queued": self.queued}


class VoiceInputBridge:
    """Convert trusted cloud STT events into explicit cmux input.

    Voice mode is fail-closed: when enabled, every STT message is either safely
    sent to the selected Claude Code surface or discarded with a reason.
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        max_queue: int = 5,
        cmux_bin: str = "cmux",
    ) -> None:
        self.enabled = False
        self.target_surface = os.getenv("STACKCHAN_VOICE_TARGET_SURFACE") or os.getenv("CMUX_SURFACE_ID") or ""
        self.max_queue = max_queue
        self.cmux_bin = cmux_bin
        self._runner = runner or self._run_command
        self._queue: deque[str] = deque()
        self._lock = asyncio.Lock()

    def set_enabled(self, enabled: bool, *, surface: str | None = None) -> dict[str, Any]:
        if surface:
            self.target_surface = surface
        self.enabled = bool(enabled)
        if not self.enabled:
            self._queue.clear()
        return {"enabled": self.enabled, "target_surface": self.target_surface, "queued": len(self._queue)}

    async def handle_stt_message(self, message: dict[str, Any]) -> VoiceDispatchResult:
        if not self.enabled:
            return VoiceDispatchResult(ok=False, reason="voice mode disabled")
        text = _extract_text(message).strip()
        reason = _reject_reason(text)
        if reason:
            return VoiceDispatchResult(ok=False, text=text, reason=reason)
        if len(self._queue) >= self.max_queue:
            return VoiceDispatchResult(ok=False, text=text, reason="queue full", queued=len(self._queue))
        self._queue.append(text)
        await self._drain_queue()
        return VoiceDispatchResult(ok=True, text=text, queued=len(self._queue))

    async def _drain_queue(self) -> None:
        async with self._lock:
            while self._queue:
                text = self._queue[0]
                ok, reason = await self._dispatch_text(text)
                if not ok:
                    # Fail closed: drop the unsafe/undeliverable item instead of
                    # forwarding it back to the device or cloud.
                    self._queue.popleft()
                    raise RuntimeError(reason)
                self._queue.popleft()

    async def _dispatch_text(self, text: str) -> tuple[bool, str]:
        surface = self.target_surface
        if not surface:
            return False, "target surface not set"
        ok, reason = await self._surface_is_claude(surface)
        if not ok:
            return False, reason
        # No reliable input-state API exists on all cmux versions; a short wait
        # avoids racing the terminal while keeping the path simple.
        await asyncio.sleep(float(os.getenv("STACKCHAN_VOICE_INPUT_DELAY_S", "0.5")))
        payload = f"[voice] The user said: {text}\\n"
        code, _out, err = await self._runner([self.cmux_bin, "send", "--surface", surface, payload])
        if code != 0:
            return False, err or "cmux send failed"
        return True, "sent"

    async def _surface_is_claude(self, surface: str) -> tuple[bool, str]:
        code, out, err = await self._runner([self.cmux_bin, "tree", "--all", "--json"])
        if code != 0:
            return False, err or "cmux tree failed"
        try:
            tree = json.loads(out)
        except json.JSONDecodeError:
            return False, "cmux tree returned invalid json"
        for item in _iter_surfaces(tree):
            if item.get("ref") != surface and item.get("id") != surface:
                continue
            haystack = " ".join(str(item.get(k) or "") for k in ("title", "process_name", "command"))
            if "claude" in haystack.lower():
                return True, "ok"
            return False, "target surface is not Claude Code"
        return False, "target surface not found"

    async def _run_command(self, args: list[str]) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _extract_text(message: dict[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    payload = message.get("payload")
    if isinstance(payload, dict):
        return _extract_text(payload)
    return ""


def _reject_reason(text: str) -> str:
    if not text:
        return "empty text"
    if len(text) > 200:
        return "text too long"
    if text[0] in {"/", "!"}:
        return "command prefix rejected"
    if _SYMBOL_RE.fullmatch(text):
        return "symbol-only text rejected"
    return ""


def _iter_surfaces(node: Any):
    if isinstance(node, dict):
        if "surface_type" in node or "tab_ref" in node or node.get("ref", "").startswith("surface:"):
            yield node
        for value in node.values():
            yield from _iter_surfaces(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_surfaces(value)
