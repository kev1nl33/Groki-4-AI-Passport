"""Behavior tests for owner-granted, completion-acknowledged announcements."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from stackchan_mcp.notify_service import NotifyService
from stackchan_mcp.esp32_client import ESP32Manager


def harness():
    now = [1000.0]
    proxy = SimpleNamespace(connected=True, speak_text=AsyncMock(return_value={"ok": True}),
                            cancel_announcement=AsyncMock())
    connection = SimpleNamespace(cloud_proxy=proxy, notify_replies=None)
    manager = SimpleNamespace(connection=connection, device_connected=True, on_device_state=None)
    def reply(state, item_id=None):
        ESP32Manager._observe_device_state(manager, dict(type="notify_ack",
            id=item_id or connection.notify_replies[0], state=state))
    async def send(raw):
        msg = json.loads(raw)
        if msg["state"] == "start":
            reply("granted")
    connection._ws_send = AsyncMock(side_effect=send)
    service = NotifyService(SimpleNamespace(esp32=manager), clock=lambda: now[0])
    return service, proxy, connection, manager, now, reply


async def started(service, proxy):
    service.submit("private announcement")
    task = asyncio.create_task(service.tick())
    for _ in range(100):
        if proxy.speak_text.called:
            return task
        await asyncio.sleep(.001)
    raise AssertionError("announcement did not start")


@pytest.mark.asyncio
async def test_cloud_completion_does_not_complete_job_and_id_must_match():
    service, proxy, connection, manager, now, reply = harness()
    task = await started(service, proxy)
    await asyncio.sleep(.01)
    assert not task.done()
    reply("done", 999)
    await asyncio.sleep(.01)
    assert not task.done()
    now[0] += 10
    reply("done")
    await task
    assert service.job(1) == dict(id=1, status="done", ok=True)
    assert not service.ready()
    now[0] += 2
    assert service.ready()


@pytest.mark.asyncio
async def test_ok_during_preparation_cancels_real_response():
    service, proxy, connection, manager, now, reply = harness()
    async def pending(*args, **kwargs):
        await asyncio.sleep(60)
    proxy.speak_text.side_effect = pending
    task = await started(service, proxy)
    reply("cancelled")
    await asyncio.wait_for(task, 1)
    proxy.cancel_announcement.assert_awaited_once_with(1)
    assert service.job(1)["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_first", [False, True])
async def test_cancelled_cloud_result_and_device_ack_in_either_order(ack_first):
    service, proxy, connection, manager, now, reply = harness()
    release_cloud = asyncio.Event()
    async def cancelled(*args, **kwargs):
        assert kwargs["session_id"] == "notify-1"
        await release_cloud.wait()
        return {"ok": False, "error": "cancelled"}
    proxy.speak_text.side_effect = cancelled
    task = await started(service, proxy)
    reply("cancelled", 999)  # An unrelated lease cannot cancel this job.
    await asyncio.sleep(.01)
    assert not task.done()
    if ack_first:
        reply("cancelled", 1)
        await asyncio.wait_for(task, 1)
    else:
        release_cloud.set()
        await asyncio.wait_for(task, 1)  # Cloud cancellation precedes the ack.
        assert connection.notify_replies is None
    assert service.job(1) == dict(id=1, status="cancelled", ok=False)
    proxy.cancel_announcement.assert_awaited_once_with(1)
    assert service.failed_count == 0
    now[0] += 2
    proxy.speak_text.side_effect = None
    proxy.speak_text.return_value = {"ok": True}
    proxy.speak_text.reset_mock()
    task = await started(service, proxy)
    reply("cancelled", 1)  # Late ack from the cancelled job cannot own job 2.
    await asyncio.sleep(.01)
    assert not task.done()
    reply("done", 2)
    await asyncio.wait_for(task, 1)
    assert service.job(2) == dict(id=2, status="done", ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"ok": False, "error": "provider_failed"},
    {"ok": False, "error": None},
])
async def test_real_cloud_failure_is_not_reclassified_as_cancellation(result):
    service, proxy, connection, manager, now, reply = harness()
    proxy.speak_text.return_value = result
    service.submit("private announcement")
    await asyncio.wait_for(service.tick(), 1)
    assert service.job(1) == dict(id=1, status="failed", ok=False)
    assert service.failed_count == 1
    proxy.cancel_announcement.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_owner_busy_is_not_overridden_by_elapsed_time():
    service, proxy, connection, manager, now, reply = harness()
    async def busy(raw):
        reply("busy")
    connection._ws_send.side_effect = busy
    service.submit("wait")
    now[0] += 11
    await service.tick()
    proxy.speak_text.assert_not_called()
    assert service.job(1)["status"] == "queued"


@pytest.mark.asyncio
async def test_disconnect_fails_and_cancels():
    service, proxy, connection, manager, now, reply = harness()
    task = await started(service, proxy)
    manager.device_connected = False
    await asyncio.wait_for(task, 1)
    assert service.job(1)["status"] == "failed"
    proxy.cancel_announcement.assert_awaited_once()


def test_expiry_frees_capacity_on_submit_and_disconnected_proxy_rejects():
    service, proxy, connection, manager, now, reply = harness()
    for _ in range(3):
        assert service.submit("x").ok
    assert service.submit("full").error == "queue_full"
    now[0] += 61
    assert service.submit("fresh").ok
    assert service.job(1)["status"] == "expired"
    proxy.connected = False
    assert service.submit("no").error == "no_voice_session"


@pytest.mark.asyncio
async def test_duration_limit_cancels_cloud_and_does_not_succeed():
    service, proxy, connection, manager, now, reply = harness()
    service._max_duration_s = .02
    async def pending(*args, **kwargs):
        await asyncio.sleep(60)
    proxy.speak_text.side_effect = pending
    service.submit("bounded")
    await asyncio.wait_for(service.tick(), 1)
    assert service.job(1)["status"] == "failed"
    proxy.cancel_announcement.assert_awaited_once_with(1)
    assert json.loads(connection._ws_send.call_args.args[0])["state"] == "cancel"


@pytest.mark.asyncio
async def test_failure_logs_never_include_exception_contents(caplog):
    service, proxy, connection, manager, now, reply = harness()
    proxy.speak_text.side_effect = ValueError("PRIVATE TEXT")
    service.submit("PRIVATE TEXT")
    await service.tick()
    assert service.job(1)["status"] == "failed"
    assert "PRIVATE TEXT" not in caplog.text
