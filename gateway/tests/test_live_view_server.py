# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Smoke tests for the face tracking live-view server (tools/vision-tracker/live-view)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = ROOT / "tools" / "vision-tracker" / "live-view" / "server.py"

if not SERVER_PATH.is_file():
    # The live view ships with the upstream Groki Bot repository's
    # tools/vision-tracker; this standalone gateway copy does not include it.
    pytest.skip(
        "vision-tracker live view is not part of this standalone gateway copy",
        allow_module_level=True,
    )


def load_server():
    spec = importlib.util.spec_from_file_location("groki_live_view_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_live_view_app_exposes_expected_routes():
    server = load_server()
    app = server.create_app()

    paths = {resource.canonical for resource in app.router.resources()}

    assert "/" in paths
    assert "/config" in paths
    assert "/track" in paths
    assert "/ws" in paths


def test_live_view_html_contains_camera_and_controls():
    html = (SERVER_PATH.with_name("index.html")).read_text(encoding="utf-8")

    assert "getUserMedia" in html
    assert "gain_x" in html
    assert "WebSocket" in html


def test_live_view_forward_to_gateway_uses_configured_url():
    """LiveViewState.forward_to_gateway POSTs to STACKCHAN_GATEWAY_TRACK_URL.

    Face-tracker fan-out: the live view shares face detections with the
    gateway's TrackingBridge by HTTP POST so a single Vision Tracker pass
    drives both the local visualiser and the physical head.
    """
    import asyncio

    server = load_server()
    state = server.LiveViewState()

    captured: list[tuple[str, dict]] = []

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class _FakeSession:
        closed = False

        def post(self, url, json):
            captured.append((url, json))
            return _FakeResp()

        async def close(self):
            self.closed = True

    state._http_session = _FakeSession()

    payload = {"x": 0.1, "y": 0.6, "confidence": 0.9}
    asyncio.new_event_loop().run_until_complete(state.forward_to_gateway(payload))

    assert len(captured) == 1
    url, sent = captured[0]
    assert url == server.GATEWAY_TRACK_URL
    assert sent == payload
