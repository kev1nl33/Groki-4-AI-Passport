"""Tests for the loopback-only notify HTTP endpoint (Stage H)."""

from __future__ import annotations

import pytest

from stackchan_mcp.notify_server import (
    NOTIFY_SERVICE_KEY,
    NOTIFY_TOKEN_KEY,
    create_notify_app,
    handle_notify,
)
from stackchan_mcp.notify_service import SubmitResult


class FakeService:
    def __init__(self, result: SubmitResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    def submit(self, text: str, flash: bool = False) -> SubmitResult:
        self.calls.append((text, flash))
        return self.result


class FakeJsonRequest:
    def __init__(self, app, body, *, token: str = "") -> None:
        self.app = app
        self._body = body
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._body


def make_app(service: FakeService, *, token: str = "secret") -> object:
    return create_notify_app(service=service, token=token)


def test_notify_app_stores_token_and_service():
    service = FakeService(SubmitResult(ok=True, status="queued"))
    app = make_app(service)
    assert app[NOTIFY_TOKEN_KEY] == "secret"
    assert app[NOTIFY_SERVICE_KEY] is service


@pytest.mark.asyncio
async def test_notify_requires_bearer_token():
    service = FakeService(SubmitResult(ok=True, status="queued"))
    app = make_app(service)
    response = await handle_notify(FakeJsonRequest(app, {"text": "hi"}, token="wrong"))
    assert response.status == 401

    response = await handle_notify(FakeJsonRequest(app, {"text": "hi"}))
    assert response.status == 401
    assert service.calls == []


@pytest.mark.asyncio
async def test_notify_rejects_invalid_payloads():
    service = FakeService(SubmitResult(ok=True, status="queued"))
    app = make_app(service)

    class BadJsonRequest(FakeJsonRequest):
        async def json(self):
            raise ValueError("not json")

    response = await handle_notify(BadJsonRequest(app, None, token="secret"))
    assert response.status == 400

    response = await handle_notify(
        FakeJsonRequest(app, {"text": "   "}, token="secret")
    )
    assert response.status == 400

    response = await handle_notify(FakeJsonRequest(app, {"text": 42}, token="secret"))
    assert response.status == 400
    assert service.calls == []


@pytest.mark.asyncio
async def test_notify_maps_service_errors_to_status_codes():
    cases = [
        ("too_long", 413),
        ("queue_full", 429),
        ("device_offline", 503),
        ("no_voice_session", 503),
    ]
    for error, status in cases:
        service = FakeService(SubmitResult(ok=False, status="", error=error))
        app = make_app(service)
        response = await handle_notify(
            FakeJsonRequest(app, {"text": "hi"}, token="secret")
        )
        assert response.status == status


@pytest.mark.asyncio
async def test_notify_accepts_queued_submission():
    service = FakeService(
        SubmitResult(ok=True, status="queued", item_id=7, queue_depth=1)
    )
    app = make_app(service)
    response = await handle_notify(
        FakeJsonRequest(app, {"text": "build ok", "flash": True}, token="secret")
    )
    assert response.status == 202
    assert service.calls == [("build ok", True)]
