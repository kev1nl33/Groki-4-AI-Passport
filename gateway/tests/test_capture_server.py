"""Tests for HTTP capture upload helpers."""

import pytest

from stackchan_mcp.capture_server import (
    CAPTURE_TOKEN_KEY,
    INJECT_TEXT_HANDLER_KEY,
    _is_authorized,
    create_capture_app,
    handle_inject_text,
)


def test_capture_app_stores_capture_token():
    """Capture app keeps the expected bearer token in app state."""
    app = create_capture_app(capture_token="capture-token")

    assert app[CAPTURE_TOKEN_KEY] == "capture-token"
    assert app[INJECT_TEXT_HANDLER_KEY] is None


def test_is_authorized_accepts_matching_bearer():
    """Bearer auth must match exactly."""
    assert _is_authorized("Bearer capture-token", "capture-token") is True


def test_is_authorized_rejects_missing_or_wrong_bearer():
    """Missing or mismatched bearer auth is rejected."""
    assert _is_authorized("", "capture-token") is False
    assert _is_authorized("Bearer wrong-token", "capture-token") is False


class FakeJsonRequest:
    def __init__(self, app, body, *, token: str = "") -> None:
        self.app = app
        self._body = body
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_inject_text_calls_handler_and_reports_active_session():
    """Debug text injection becomes a plain user turn through the wired handler."""
    seen: list[str] = []

    async def handler(text: str) -> bool:
        seen.append(text)
        return True

    app = create_capture_app(inject_text_handler=handler)
    response = await handle_inject_text(FakeJsonRequest(app, {"text": "放一首歌"}))

    assert response.status == 200
    assert '"active_session": true' in response.text
    assert seen == ["放一首歌"]


@pytest.mark.asyncio
async def test_inject_text_reports_no_active_session_without_handler():
    app = create_capture_app()
    response = await handle_inject_text(FakeJsonRequest(app, {"text": "测试"}))

    assert response.status == 200
    assert '"active_session": false' in response.text


@pytest.mark.asyncio
async def test_inject_text_reuses_capture_bearer_token():
    app = create_capture_app(capture_token="secret")

    response = await handle_inject_text(FakeJsonRequest(app, {"text": "测试"}))

    assert response.status == 401
