"""Tests for safe STT-to-cmux voice bridge."""

import json

import pytest

from stackchan_mcp.voice_input_bridge import VoiceInputBridge


def _tree(title="Claude Leader", ref="surface:263"):
    return json.dumps({"windows": [{"workspaces": [{"panes": [{"surfaces": [{"ref": ref, "title": title}]}]}]}]})


@pytest.mark.asyncio
async def test_voice_bridge_sends_prefixed_text_to_valid_claude_surface(monkeypatch):
    monkeypatch.setenv("STACKCHAN_VOICE_INPUT_DELAY_S", "0")
    calls = []

    async def runner(args):
        calls.append(args)
        if args[:4] == ["cmux", "tree", "--all", "--json"]:
            return 0, _tree(), ""
        if args[:3] == ["cmux", "send", "--surface"]:
            return 0, "", ""
        return 1, "", "unexpected"

    bridge = VoiceInputBridge(runner=runner)
    bridge.set_enabled(True, surface="surface:263")

    result = await bridge.handle_stt_message({"type": "stt", "text": "帮我看一下状态"})

    assert result.ok is True
    send = calls[-1]
    assert send[:4] == ["cmux", "send", "--surface", "surface:263"]
    assert send[4] == "[voice] The user said: 帮我看一下状态\\n"


@pytest.mark.asyncio
async def test_voice_bridge_rejects_command_prefix(monkeypatch):
    monkeypatch.setenv("STACKCHAN_VOICE_INPUT_DELAY_S", "0")
    bridge = VoiceInputBridge(runner=lambda args: None)  # not reached
    bridge.set_enabled(True, surface="surface:263")

    result = await bridge.handle_stt_message({"type": "stt", "text": "/danger"})

    assert result.ok is False
    assert result.reason == "command prefix rejected"


@pytest.mark.asyncio
async def test_voice_bridge_fails_closed_for_non_claude_surface(monkeypatch):
    monkeypatch.setenv("STACKCHAN_VOICE_INPUT_DELAY_S", "0")

    async def runner(args):
        if args[:4] == ["cmux", "tree", "--all", "--json"]:
            return 0, _tree(title="Codex", ref="surface:263"), ""
        return 0, "", ""

    bridge = VoiceInputBridge(runner=runner)
    bridge.set_enabled(True, surface="surface:263")

    with pytest.raises(RuntimeError, match="not Claude Code"):
        await bridge.handle_stt_message({"type": "stt", "text": "你好"})
