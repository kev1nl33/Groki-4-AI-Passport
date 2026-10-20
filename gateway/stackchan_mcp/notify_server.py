"""Loopback-only HTTP entry for proactive device announcements (Stage H).

The gateway binds this app to ``127.0.0.1`` only, on its own port, so
machines on the LAN cannot reach it. A bearer token is mandatory; if none
is configured the gateway does not start the endpoint at all.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

NOTIFY_TOKEN_KEY = web.AppKey("notify_token", str)
NOTIFY_SERVICE_KEY = web.AppKey("notify_service", object)

# Submission errors mapped to HTTP status codes. Anything unknown is a
# client error; offline-style failures must never look like success.
_ERROR_STATUS = {
    "too_long": 413,
    "queue_full": 429,
    "device_offline": 503,
    "no_voice_session": 503,
}


async def handle_notify(request: web.Request) -> web.Response:
    """Queue one announcement: POST /notify {"text": "...", "flash": false}."""
    expected_token = request.app[NOTIFY_TOKEN_KEY]
    auth = request.headers.get("Authorization", "")
    if not expected_token or not hmac.compare_digest(auth, f"Bearer {expected_token}"):
        logger.warning("Notify auth rejected")
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        return web.json_response(
            {"ok": False, "error": "payload too large"}, status=413
        )
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "invalid body"}, status=400)

    text = body.get("text")
    flash = body.get("flash", False)
    if not isinstance(text, str) or not text.strip():
        return web.json_response(
            {"ok": False, "error": "text is required"}, status=400
        )
    if not isinstance(flash, bool):
        return web.json_response(
            {"ok": False, "error": "flash must be a boolean"}, status=400
        )

    result = request.app[NOTIFY_SERVICE_KEY].submit(text, flash=flash)
    if not result.ok:
        return web.json_response(
            {"ok": False, "error": result.error},
            status=_ERROR_STATUS.get(result.error or "", 400),
        )
    return web.json_response(
        {
            "ok": True,
            "status": result.status,
            "id": result.item_id,
            "queue_depth": result.queue_depth,
        },
        status=202,
    )


async def handle_job(request: web.Request) -> web.Response:
    token = request.app[NOTIFY_TOKEN_KEY]
    if not token or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}"):
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        job = request.app[NOTIFY_SERVICE_KEY].job(int(request.match_info["id"]))
    except ValueError:
        job = None
    return web.json_response(job or {"ok": False, "error": "not_found"}, status=200 if job else 404)


def create_notify_app(*, service, token: str) -> web.Application:
    """Build the loopback notify app around a NotifyService."""
    app = web.Application()
    app[NOTIFY_TOKEN_KEY] = token
    app[NOTIFY_SERVICE_KEY] = service
    app.router.add_post("/notify", handle_notify)
    app.router.add_get("/notify/{id}", handle_job)
    return app
