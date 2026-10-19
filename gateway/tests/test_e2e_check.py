"""Tests for the end-to-end self-check core logic (all I/O injected, none real)."""

from typing import Any

from stackchan_mcp.e2e_check import (
    MUSIC_PAUSE_PROMPT,
    MUSIC_PLAY_PROMPT,
    TEXT_QA_PROMPT,
    CheckResult,
    E2EChecker,
    gateway_token_from_env,
    has_failure,
    main,
    render_report,
)


def healthy_status(**overrides: Any) -> dict[str, Any]:
    st = {
        "device": {"connected": True},
        "gemini": {"connected": True},
        "wake_gate": {"available": True, "wake_count": 0},
        "audio": {"last_device_audio_at": 1.0, "tts_active": False},
        "recent": {"tool_calls": [], "transcripts": []},
    }
    st.update(overrides)
    return st


class FakeGateway:
    """Scriptable fake gateway: fetch/post/run_cmd/sleep/clock all go through here."""

    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.status = status or healthy_status()
        self.injected: list[str] = []
        self.inject_response: dict[str, Any] = {"ok": True, "active_session": True}
        self.cmds: list[list[str]] = []
        self.cmd_results: dict[str, tuple[int, str]] = {}
        self.now = 0.0
        self.on_sleep = None

    def fetch_json(self, url: str, *, timeout: float) -> dict[str, Any]:
        assert url.endswith("/debug/status")
        return self.status

    def post_json(self, url, payload, headers, *, timeout):
        assert url.endswith("/debug/inject-text")
        self.injected.append(payload["text"])
        return self.inject_response

    def run_cmd(self, cmd: list[str]) -> tuple[int, str]:
        self.cmds.append(cmd)
        return self.cmd_results.get(cmd[0], (0, ""))

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()

    def clock(self) -> float:
        return self.now

    def make_checker(self, **kwargs: Any) -> E2EChecker:
        return E2EChecker(
            fetch_json=self.fetch_json,
            post_json=self.post_json,
            run_cmd=self.run_cmd,
            sleep=self.sleep,
            clock=self.clock,
            **kwargs,
        )


# --- status check ----------------------------------------------------------


def test_check_status_passes_when_all_healthy():
    result = FakeGateway().make_checker().check_status()
    assert result.passed is True


def test_check_status_lists_every_problem():
    gw = FakeGateway(
        healthy_status(
            device={"connected": False},
            gemini={"connected": False},
            wake_gate={"available": False, "wake_count": 0},
        )
    )
    result = gw.make_checker().check_status()
    assert result.passed is False
    assert "device not connected" in result.detail
    assert "gemini not connected" in result.detail
    assert "wake_gate unavailable" in result.detail


def test_check_status_fails_when_gateway_unreachable():
    def broken_fetch(url, *, timeout):
        raise OSError("connection refused")

    checker = E2EChecker(fetch_json=broken_fetch)
    result = checker.check_status()
    assert result.passed is False
    assert result.detail.startswith("cannot read")


# --- text Q&A ---------------------------------------------------------------


def test_text_qa_passes_when_new_transcript_appears_after_inject():
    gw = FakeGateway()

    def add_transcript():
        gw.status["recent"]["transcripts"] = [{"text": "现在是三点整", "at": 99.0}]

    gw.on_sleep = add_transcript
    result = gw.make_checker(timeout_s=15.0).check_text_qa()
    assert result.passed is True
    assert gw.injected == [TEXT_QA_PROMPT]
    assert "现在是三点整" in result.detail


def test_text_qa_ignores_stale_transcripts_and_times_out():
    gw = FakeGateway()
    gw.status["recent"]["transcripts"] = [{"text": "旧回应", "at": 50.0}]
    result = gw.make_checker(timeout_s=15.0).check_text_qa()
    assert result.passed is False
    assert "without a new transcript" in result.detail
    assert gw.now >= 15.0  # polled until the timeout


def test_text_qa_fails_fast_when_no_active_session():
    gw = FakeGateway()
    gw.inject_response = {"ok": True, "active_session": False}
    result = gw.make_checker().check_text_qa()
    assert result.passed is False
    assert "active session" in result.detail


# --- voice wake ---------------------------------------------------------------


def test_voice_wake_passes_when_wake_count_increases():
    gw = FakeGateway()

    def wake():
        gw.status["wake_gate"]["wake_count"] = 1

    gw.on_sleep = wake
    result = gw.make_checker().check_voice_wake()
    assert result.passed is True
    assert gw.cmds[0][:2] == ["say", "-v"]


def test_voice_wake_adds_say_audio_device_from_env(monkeypatch):
    monkeypatch.setenv("STACKCHAN_E2E_SAY_DEVICE", "机器人麦克风")
    gw = FakeGateway()

    def wake():
        gw.status["wake_gate"]["wake_count"] = 1

    gw.on_sleep = wake
    result = gw.make_checker().check_voice_wake()
    assert result.passed is True
    assert "-a" in gw.cmds[0]
    assert gw.cmds[0][gw.cmds[0].index("-a") + 1] == "机器人麦克风"


def test_voice_wake_fails_when_say_command_missing():
    gw = FakeGateway()
    gw.cmd_results["say"] = (127, "say not found")
    result = gw.make_checker().check_voice_wake()
    assert result.passed is False
    assert "say failed" in result.detail


def test_voice_wake_hints_device_listing_when_say_device_fails(monkeypatch):
    monkeypatch.setenv("STACKCHAN_E2E_SAY_DEVICE", "不存在的设备")
    gw = FakeGateway()
    gw.cmd_results["say"] = (1, "Could not find audio device")
    result = gw.make_checker().check_voice_wake()
    assert result.passed is False
    assert "say -a '?'" in result.detail


def test_voice_wake_times_out_without_new_wake():
    gw = FakeGateway()
    result = gw.make_checker(timeout_s=15.0).check_voice_wake()
    assert result.passed is False
    assert "without a new wake" in result.detail


# --- music control ---------------------------------------------------------------


class MusicGateway(FakeGateway):
    """Fake osascript: the player state follows the injected play/pause requests."""

    def __init__(self) -> None:
        super().__init__()
        self.player_state = "stopped"
        self.player = "Music"

    def post_json(self, url, payload, headers, *, timeout):
        result = super().post_json(url, payload, headers, timeout=timeout)
        if payload["text"] == MUSIC_PLAY_PROMPT:
            self.player_state = "playing"
        elif payload["text"] == MUSIC_PAUSE_PROMPT:
            self.player_state = "paused"
        return result

    def run_cmd(self, cmd: list[str]) -> tuple[int, str]:
        self.cmds.append(cmd)
        script = cmd[-1]
        if "is running" in script:
            return 0, self.player
        return 0, self.player_state


def test_music_check_verifies_play_then_pause():
    gw = MusicGateway()
    result = gw.make_checker().check_music()
    assert result.passed is True
    assert gw.injected == [MUSIC_PLAY_PROMPT, MUSIC_PAUSE_PROMPT]


def test_music_check_fails_when_state_never_reaches_playing():
    gw = MusicGateway()
    gw.player = "missing"  # the player stays stopped

    def run_cmd(cmd):
        gw.cmds.append(cmd)
        return 0, "stopped"

    gw.run_cmd = run_cmd
    result = gw.make_checker(timeout_s=15.0).check_music()
    assert result.passed is False
    assert "playback did not start" in result.detail


def test_music_check_reports_no_player():
    gw = FakeGateway()
    gw.cmd_results["osascript"] = (0, "no player running")  # mac_control sentinel value
    result = gw.make_checker(timeout_s=15.0).check_music()
    assert result.passed is False
    assert "no-player" in result.detail


# --- run all and summary -------------------------------------------------------------


def test_run_all_default_skips_voice_and_runs_music():
    gw = MusicGateway()
    gw.status["recent"]["transcripts"] = []

    def add_transcript():
        gw.status["recent"]["transcripts"] = [{"text": "好的", "at": 9.0}]

    gw.on_sleep = add_transcript
    results = gw.make_checker().run_all()
    names = [r.name for r in results]
    assert names == ["Status", "Text Q&A", "Voice wake", "Music control"]
    voice = results[2]
    assert voice.skipped is True
    assert has_failure(results) is False


def test_run_all_marks_rest_skipped_when_gateway_unreachable():
    def broken_fetch(url, *, timeout):
        raise OSError("connection refused")

    checker = E2EChecker(fetch_json=broken_fetch)
    results = checker.run_all(with_voice=True)
    assert results[0].passed is False
    assert all(r.skipped for r in results[1:])
    assert has_failure(results) is True


def test_render_report_contains_labels_and_summary():
    results = [
        CheckResult("Status", True, "all online"),
        CheckResult("Text Q&A", False, "timed out"),
        CheckResult("Voice wake", True, "not enabled", skipped=True),
    ]
    report = render_report(results)
    assert "[PASS] Status" in report
    assert "[FAIL] Text Q&A" in report
    assert "[SKIP] Voice wake" in report
    assert "1 passed, 1 failed, 1 skipped" in report


def test_main_returns_nonzero_on_failure(monkeypatch, capsys):
    def broken_fetch(url, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(
        "stackchan_mcp.e2e_check._default_fetch_json",
        broken_fetch,
    )
    rc = main(["--skip-music"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Groki Bot end-to-end self-check" in out
    assert "[FAIL]" in out


def test_gateway_token_from_env(monkeypatch):
    for var in ("STACKCHAN_TOKEN", "BEARER_TOKEN", "VISION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert gateway_token_from_env() == ""
    monkeypatch.setenv("BEARER_TOKEN", "b-token")
    assert gateway_token_from_env() == "b-token"
    monkeypatch.setenv("STACKCHAN_TOKEN", "s-token")
    assert gateway_token_from_env() == "s-token"


def test_voice_self_check_says_the_default_wake_word():
    from stackchan_mcp.e2e_check import VOICE_WAKE_UTTERANCE
    from stackchan_mcp.wake_gate import WAKE_PHRASE

    assert VOICE_WAKE_UTTERANCE.lower().startswith(WAKE_PHRASE)
