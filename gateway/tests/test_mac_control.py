"""Unit tests for the Mac-control executors.

No real subprocess runs — every test injects a stub run_cmd and asserts the
argv contract plus the structured result envelope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from stackchan_mcp.mac_control import MAC_TOOL_NAMES, MacController, resolve_claude_bin


class StubRunner:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr

    async def __call__(self, *argv: str, **kwargs) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self.rc, self.stdout, self.stderr


class SequenceRunner:
    def __init__(self, responses) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responses = list(responses)

    async def __call__(self, *argv: str, **kwargs) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self.responses.pop(0)


def _dispatch(controller: MacController, name: str, args: dict):
    return asyncio.run(controller.dispatch(name, args))


def test_every_mac_tool_name_has_a_handler():
    controller = MacController(run_cmd=StubRunner())
    for name in MAC_TOOL_NAMES:
        assert hasattr(controller, f"_tool_{name}"), name


def test_unknown_tool_rejected():
    result = _dispatch(MacController(run_cmd=StubRunner()), "rm_rf", {})
    assert result["ok"] is False


def test_open_app_builds_open_dash_a():
    runner = StubRunner()
    result = _dispatch(MacController(run_cmd=runner), "open_app", {"name": "Xcode"})
    assert result == {"ok": True, "opened": "Xcode"}
    assert runner.calls == [("open", "-a", "Xcode")]


def test_open_app_requires_name():
    runner = StubRunner()
    result = _dispatch(MacController(run_cmd=runner), "open_app", {"name": "  "})
    assert result["ok"] is False
    assert runner.calls == []


def test_open_url_rejects_non_http_schemes():
    runner = StubRunner()
    controller = MacController(run_cmd=runner)
    for url in ("file:///etc/passwd", "ftp://x", "javascript:alert(1)", "notaurl"):
        result = _dispatch(controller, "open_url", {"url": url})
        assert result["ok"] is False, url
    assert runner.calls == []


def test_open_url_accepts_https():
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "open_url", {"url": "https://example.com"}
    )
    assert result["ok"] is True
    assert runner.calls == [("open", "https://example.com")]


def test_media_control_validates_action():
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "media_control", {"action": "louder"}
    )
    assert result["ok"] is False
    assert runner.calls == []


def test_media_control_reports_music_auto_open_failure():
    runner = SequenceRunner(
        [
            (0, "no player running", ""),
            (1, "", "Unable to find application"),
        ]
    )

    result = _dispatch(
        MacController(run_cmd=runner),
        "media_control",
        {"action": "play"},
    )

    assert result == {
        "ok": False,
        "error": "Unable to find application",
        "auto_opened": "Music",
    }


async def _no_sleep(_seconds: float) -> None:
    return None


def _stuck(state: str) -> list[tuple[int, str, str]]:
    """The player stays in one state: the first read and every poll return it."""
    from stackchan_mcp.mac_control import _MEDIA_STATE_POLL_TRIES

    return [(0, state, "")] * (_MEDIA_STATE_POLL_TRIES + 1)


def _run_media_control_scenario(responses, action: str):
    runner = SequenceRunner(responses)
    controller = MacController(run_cmd=runner, sleep=_no_sleep)
    result = _dispatch(controller, "media_control", {"action": action})
    return result, runner


def test_media_control_play_uses_idempotent_play_and_returns_player_state():
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            (0, "playing", ""),
        ],
        action="play",
    )

    assert result == {
        "ok": True,
        "action": "play",
        "player": "Music",
        "player_state": "playing",
    }
    assert 'tell application "Music" to play' in runner.calls[1][2]
    assert "player state" in runner.calls[2][2]


def test_media_control_pause_uses_idempotent_pause_and_returns_player_state():
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            (0, "paused", ""),
        ],
        action="pause",
    )

    assert result == {
        "ok": True,
        "action": "pause",
        "player": "Music",
        "player_state": "paused",
    }
    assert 'tell application "Music" to pause' in runner.calls[1][2]
    assert "playpause" not in runner.calls[1][2]
    assert "player state" in runner.calls[2][2]


def test_media_control_pause_accepts_stopped_as_non_playing_state():
    result, _runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            (0, "stopped", ""),
        ],
        action="pause",
    )

    assert result == {
        "ok": True,
        "action": "pause",
        "player": "Music",
        "player_state": "stopped",
    }


def test_media_control_pause_reports_false_when_music_keeps_playing():
    result, _runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            *_stuck("playing"),
        ],
        action="pause",
    )

    assert result == {
        "ok": False,
        "error": "playback did not stop",
        "action": "pause",
        "player": "Music",
        "player_state": "playing",
    }


def test_media_control_play_falls_back_to_library_shuffle_on_empty_queue():
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            *_stuck("stopped"),
            (0, "", ""),
            (0, "playing", ""),
        ],
        action="play",
    )

    assert result == {
        "ok": True,
        "action": "play",
        "player": "Music",
        "player_state": "playing",
    }
    shuffle_calls = [c for c in runner.calls if "play playlist 1" in c[-1]]
    assert len(shuffle_calls) == 1
    assert "shuffle enabled" in shuffle_calls[0][-1]


def test_media_control_play_reports_false_when_music_does_not_start():
    result, _runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            *_stuck("paused"),
            (0, "", ""),
            *_stuck("paused"),
        ],
        action="play",
    )

    assert result == {
        "ok": False,
        "error": "playback did not start",
        "action": "play",
        "player": "Music",
        "player_state": "paused",
    }


def test_media_control_play_pause_uses_pre_state_to_verify_toggle():
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "playing", ""),
            (0, "", ""),
            (0, "paused", ""),
        ],
        action="play_pause",
    )

    assert result == {
        "ok": True,
        "action": "play_pause",
        "player": "Music",
        "player_state": "paused",
    }
    assert "player state" in runner.calls[1][2]
    assert "playpause" in runner.calls[2][2]
    assert "player state" in runner.calls[3][2]


def test_media_control_play_pause_reports_false_when_toggle_does_not_pause():
    result, _runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "playing", ""),
            (0, "", ""),
            *_stuck("playing"),
        ],
        action="play_pause",
    )

    assert result == {
        "ok": False,
        "error": "playback did not stop",
        "action": "play_pause",
        "player": "Music",
        "player_state": "playing",
    }


def test_media_control_next_returns_actual_player_state_without_guessing_success():
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            (0, "paused", ""),
        ],
        action="next",
    )

    assert result == {
        "ok": True,
        "action": "next",
        "player": "Music",
        "player_state": "paused",
    }
    assert "next track" in runner.calls[1][2]
    assert "player state" in runner.calls[2][2]


def test_media_control_previous_returns_actual_player_state():
    result, runner = _run_media_control_scenario(
        [
            (0, "Spotify", ""),
            (0, "", ""),
            (0, "playing", ""),
        ],
        action="previous",
    )

    assert result == {
        "ok": True,
        "action": "previous",
        "player": "Spotify",
        "player_state": "playing",
    }
    assert "previous track" in runner.calls[1][2]


def test_media_control_auto_opens_music_and_verifies_return_value():
    result, runner = _run_media_control_scenario(
        [
            (0, "no player running", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "playing", ""),
        ],
        action="play",
    )

    assert result == {
        "ok": True,
        "action": "play",
        "player": "Music",
        "player_state": "playing",
        "auto_opened": "Music",
    }
    assert runner.calls[0][0] == "osascript"
    assert runner.calls[1] == ("open", "-a", "Music")
    assert 'tell application "Music" to play' in runner.calls[2][2]
    assert "player state" in runner.calls[3][2]


def test_media_control_osascript_failure_returns_error_before_state_read():
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (1, "", "execution failed"),
        ],
        action="pause",
    )

    assert result == {"ok": False, "error": "execution failed"}
    assert len(runner.calls) == 2


def test_set_volume_clamps_level():
    runner = StubRunner()
    result = _dispatch(MacController(run_cmd=runner), "set_volume", {"level": 250})
    assert result == {"ok": True, "level": 100}
    assert runner.calls[0] == ("osascript", "-e", "set volume output volume 100")


def test_set_volume_mute_takes_precedence():
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "set_volume", {"mute": True, "level": 30}
    )
    assert result == {"ok": True, "muted": True}
    assert runner.calls[0] == ("osascript", "-e", "set volume output muted true")


def test_set_volume_requires_level_or_mute():
    result = _dispatch(MacController(run_cmd=StubRunner()), "set_volume", {})
    assert result["ok"] is False


def test_lock_screen_uses_pmset():
    runner = StubRunner()
    result = _dispatch(MacController(run_cmd=runner), "lock_screen", {})
    assert result["ok"] is True
    assert runner.calls == [("pmset", "displaysleepnow")]


def test_take_screenshot_saves_into_dir(tmp_path: Path):
    runner = StubRunner()
    controller = MacController(run_cmd=runner, screenshot_dir=tmp_path)
    result = _dispatch(controller, "take_screenshot", {})
    assert result["ok"] is True
    assert result["path"].startswith(str(tmp_path))
    assert runner.calls[0][:2] == ("screencapture", "-x")


def test_run_shortcut_requires_name():
    result = _dispatch(MacController(run_cmd=StubRunner()), "run_shortcut", {})
    assert result["ok"] is False


def test_run_shortcut_runs_by_name():
    runner = StubRunner(stdout="done")
    result = _dispatch(
        MacController(run_cmd=runner), "run_shortcut", {"name": "晚安模式"}
    )
    assert result["ok"] is True
    assert runner.calls == [("shortcuts", "run", "晚安模式")]


def test_list_shortcuts_returns_names():
    runner = StubRunner(stdout="甲\n乙\n\n丙")
    result = _dispatch(MacController(run_cmd=runner), "list_shortcuts", {})
    assert result == {"ok": True, "shortcuts": ["甲", "乙", "丙"]}


def test_failed_command_surfaces_stderr():
    runner = StubRunner(rc=1, stderr="Unable to find application")
    result = _dispatch(MacController(run_cmd=runner), "open_app", {"name": "Nope"})
    assert result["ok"] is False
    assert "Unable to find" in result["error"]


# ---- task lane --------------------------------------------------------------


def test_run_mac_task_requires_task():
    result = _dispatch(MacController(run_cmd=StubRunner()), "run_mac_task", {})
    assert result["ok"] is False


def test_run_mac_task_starts_and_announces_completion(monkeypatch):
    monkeypatch.delenv("STACKCHAN_CLAUDE_MODEL", raising=False)
    runner = StubRunner(stdout="整理完成，移动了 3 个文件")
    announced: list[dict] = []
    started: list[bool] = []

    async def on_done(record):
        announced.append(record)

    async def on_start():
        started.append(True)

    async def scenario():
        controller = MacController(
            run_cmd=runner,
            on_task_done=on_done,
            on_task_start=on_start,
            claude_bin="claude-test",
        )
        result = await controller.dispatch("run_mac_task", {"task": "整理桌面截图"})
        assert result["ok"] is True
        assert result["status"] == "started"
        # Let the background task run to completion.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status = await controller.dispatch("check_mac_task", {})
        return result, status

    result, status = asyncio.run(scenario())
    argv = runner.calls[0]
    assert argv[:3] == ("claude-test", "-p", "整理桌面截图")
    assert "--permission-mode" in argv
    # Voice favours speed: sonnet5 with low effort.
    assert "claude-sonnet-5" in argv
    assert "--effort" in argv and "low" in argv
    assert started == [True]
    assert announced and announced[0]["status"] == "done"
    assert "整理完成" in announced[0]["result"]
    assert status["tasks"][0]["id"] == result["task_id"]
    assert status["tasks"][0]["status"] == "done"


def test_run_mac_task_missing_command_records_failure_and_announces():
    announced: list[dict] = []

    async def on_done(record):
        announced.append(record)

    async def missing_cmd(*_argv, **_kwargs):
        raise FileNotFoundError("No such file or directory: 'claude'")

    async def scenario():
        controller = MacController(run_cmd=missing_cmd, on_task_done=on_done)
        result = await controller.dispatch("run_mac_task", {"task": "整理桌面"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status = await controller.dispatch("check_mac_task", {})
        return result, status

    result, status = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["status"] == "started"
    assert announced and announced[0]["status"] == "failed"
    assert "claude" in announced[0]["result"].lower()
    assert status["tasks"][0]["status"] == "failed"
    assert status["tasks"][0]["state"] == "failed"


def test_run_mac_task_uses_configured_claude_model(monkeypatch):
    monkeypatch.setenv("STACKCHAN_CLAUDE_MODEL", "claude-opus-5")
    calls = []

    async def runner(*argv, timeout=10.0, stdin_text=None):
        calls.append(argv)
        return 0, "done", ""

    async def scenario():
        controller = MacController(run_cmd=runner, claude_bin="claude-test")
        await controller.dispatch("run_mac_task", {"task": "整理桌面截图"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "claude-opus-5"


def test_resolve_claude_bin_uses_env_override(tmp_path, monkeypatch):
    binary = tmp_path / "my-claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("STACKCHAN_CLAUDE_BIN", str(binary))
    assert resolve_claude_bin() == str(binary)


def test_resolve_claude_bin_keeps_missing_name_and_is_callable():
    resolved = resolve_claude_bin("definitely-not-a-claude-binary")
    assert resolved == "definitely-not-a-claude-binary"


def test_run_mac_task_failure_is_reported():
    runner = StubRunner(rc=1, stderr="boom")
    announced: list[dict] = []

    async def on_done(record):
        announced.append(record)

    async def scenario():
        controller = MacController(run_cmd=runner, on_task_done=on_done)
        await controller.dispatch("run_mac_task", {"task": "会失败的任务"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert announced and announced[0]["status"] == "failed"
    assert "boom" in announced[0]["result"]


def test_cancel_non_cancelable_mac_task_is_rejected():
    async def hanging_cmd(*_argv, **_kwargs):
        await asyncio.Event().wait()

    async def scenario():
        controller = MacController(run_cmd=hanging_cmd)
        result = await controller.dispatch("run_mac_task", {"task": "整理文件"})
        cancel = await controller.cancel_task(result["task_id"])
        for task in list(controller._bg_tasks):
            task.cancel()
        await asyncio.gather(*list(controller._bg_tasks), return_exceptions=True)
        return result, cancel

    result, cancel = asyncio.run(scenario())
    assert result["status"] == "started"
    assert cancel["ok"] is False
    assert cancel["error"] == "task is not cancelable"


def test_check_mac_task_empty():
    result = _dispatch(MacController(run_cmd=StubRunner()), "check_mac_task", {})
    assert result["ok"] is True
    assert result["tasks"] == []


def test_web_search_requires_query():
    runner = StubRunner()
    result = _dispatch(MacController(run_cmd=runner), "web_search", {})
    assert result["ok"] is False
    assert "query" in result.get("error", "").lower()
    assert runner.calls == []


def test_web_search_defaults_to_grok_and_uses_chrome(monkeypatch):
    monkeypatch.setattr("stackchan_mcp.mac_control._chrome_installed", lambda: True)
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "web_search", {"query": "东京天气"}
    )
    assert result["ok"] is True
    assert result["query"] == "东京天气"
    assert result["engine"] == "grok"
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    # grok always uses Chrome
    assert argv[:3] == ("open", "-a", "Google Chrome")
    url = argv[3]
    assert url.startswith("https://grok.com/?q=")
    # The Chinese query is encoded by quote_plus (no raw Chinese or spaces left)
    assert "东京" not in url and " " not in url
    assert "%E4%B8%9C%E4%BA%AC%E5%A4%A9%E6%B0%94" in url or "%E6%9D%B1%E4%BA%AC" in url  # 东京 or variant


def test_web_search_grok_engine_url_splicing(monkeypatch):
    monkeypatch.setattr("stackchan_mcp.mac_control._chrome_installed", lambda: True)
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "web_search", {"query": "上海明天天气", "engine": "grok"}
    )
    assert result["ok"] is True
    assert result["engine"] == "grok"
    argv = runner.calls[-1]
    assert argv[:3] == ("open", "-a", "Google Chrome")
    url = argv[3]
    assert url.startswith("https://grok.com/?q=")
    assert "上海" not in url and " " not in url
    assert "%E4%B8%8A%E6%B5%B7" in url  # 上海 encoded


def test_web_search_engine_param_bing_baidu_and_explicit_override():
    runner = StubRunner()
    # bing
    _dispatch(MacController(run_cmd=runner), "web_search", {"query": "test", "engine": "bing"})
    url_bing = runner.calls[-1][1]
    assert url_bing.startswith("https://www.bing.com/search?q=")
    # baidu
    _dispatch(MacController(run_cmd=runner), "web_search", {"query": "测试", "engine": "baidu"})
    url_baidu = runner.calls[-1][1]
    assert url_baidu.startswith("https://www.baidu.com/s?wd=")
    assert "测试" not in url_baidu  # encoded

    # an explicit engine overrides the default grok
    _dispatch(MacController(run_cmd=runner), "web_search", {"query": "天气", "engine": "google"})
    argv_google = runner.calls[-1]
    assert argv_google[0] == "open"
    assert argv_google[1].startswith("https://www.google.com/search?q=")
    assert "grok.com" not in argv_google[1]


def test_web_search_rejects_invalid_engine():
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "web_search", {"query": "x", "engine": "yahoo"}
    )
    assert result["ok"] is False
    assert "engine" in result.get("error", "").lower()
    assert runner.calls == []


def test_web_search_grok_falls_back_to_default_browser_without_chrome(monkeypatch):
    """Without Chrome, grok search opens in the default browser instead of failing on `open -a`."""
    monkeypatch.setattr("stackchan_mcp.mac_control._chrome_installed", lambda: False)
    runner = StubRunner()
    result = _dispatch(
        MacController(run_cmd=runner), "web_search", {"query": "智能硬件"}
    )
    assert result["ok"] is True
    argv = runner.calls[0]
    assert argv[0] == "open" and "-a" not in argv
    assert argv[1].startswith("https://grok.com/?q=")


def test_media_control_play_waits_for_lagging_state_instead_of_reporting_failure():
    """Music was already playing, but the read right after play returned the old
    state and the robot said playback had failed."""
    result, runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            (0, "stopped", ""),
            (0, "stopped", ""),
            (0, "playing", ""),
        ],
        action="play",
    )

    assert result == {
        "ok": True,
        "action": "play",
        "player": "Music",
        "player_state": "playing",
    }
    # The state only lagged: the library shuffle fallback must not run (it would
    # replace the song that is already playing).
    assert not any("play playlist 1" in c[-1] for c in runner.calls)


def test_media_control_pause_waits_for_lagging_state_instead_of_reporting_failure():
    """On a real Mac the first read after pause still says playing; about 0.1 s
    later it says paused."""
    result, _runner = _run_media_control_scenario(
        [
            (0, "Music", ""),
            (0, "", ""),
            (0, "playing", ""),
            (0, "paused", ""),
        ],
        action="pause",
    )

    assert result == {
        "ok": True,
        "action": "pause",
        "player": "Music",
        "player_state": "paused",
    }
