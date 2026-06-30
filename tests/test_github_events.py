"""Tests for the live GitHub-event push: hub fan-out, poll publishing, WS, endpoint.

These cover the pub/sub :class:`~git_automation.core.github.hub.Hub` (fan-out,
unsubscribe, drop-oldest under back-pressure), that ``poll_once`` broadcasts the
right event shape while still OS-notifying new mention/assign threads,
``fire_test_notification``'s delivered True/False contract (with the notify sink
mocked, never a real subprocess), and the WebSocket/endpoint surface
(Origin-reject at the handshake; the test-notification POST).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect as StarletteWSDisconnect

from git_automation.core.github import client, notifier
from git_automation.core.github.hub import Hub
from git_automation.core.github.models import Notification
from git_automation.web.api import github_events


def _note(id_: str, reason: str, *, unread: bool = True) -> Notification:
    return Notification(
        id=id_,
        reason=reason,
        unread=unread,
        title="Hi",
        subject_type="Issue",
        repo="octocat/hello",
        number=1,
        url="https://api.github.com/repos/octocat/hello/issues/1",
        updated_at="2026-06-30T10:00:00Z",
    )


# --- Hub: fan-out / unsubscribe / drop-oldest -----------------------------


async def test_hub_fans_out_to_every_subscriber() -> None:
    hub = Hub()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    assert hub.subscriber_count == 2

    await hub.publish({"type": "x"})

    assert q1.get_nowait() == {"type": "x"}
    assert q2.get_nowait() == {"type": "x"}


async def test_hub_unsubscribe_stops_delivery() -> None:
    hub = Hub()
    q = hub.subscribe()
    hub.unsubscribe(q)
    assert hub.subscriber_count == 0

    await hub.publish({"type": "x"})

    assert q.empty()
    # Idempotent: unsubscribing twice is harmless.
    hub.unsubscribe(q)


async def test_hub_drops_oldest_when_subscriber_queue_full() -> None:
    hub = Hub(maxsize=2)
    q = hub.subscribe()

    await hub.publish({"n": 1})
    await hub.publish({"n": 2})
    await hub.publish({"n": 3})  # queue full -> oldest ({"n":1}) dropped

    # Publisher never blocked; queue holds the two newest, oldest gone.
    assert q.get_nowait() == {"n": 2}
    assert q.get_nowait() == {"n": 3}
    assert q.empty()


# --- poll_once: publishes the live event AND still OS-notifies -------------


def _stub_notifications(monkeypatch: pytest.MonkeyPatch, notes: list[Notification]) -> None:
    async def fake_list(**_kwargs):
        return notes

    monkeypatch.setattr(client, "list_notifications", fake_list)


async def test_poll_once_publishes_event_shape_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes = [_note("1", "mention"), _note("2", "comment", unread=False)]
    _stub_notifications(monkeypatch, notes)

    published: list[dict] = []
    fired: list[Notification] = []

    async def publish(event: dict) -> None:
        published.append(event)

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: set[str] = set()
    await notifier.poll_once(seen, notify, prime=False, publish=publish)

    assert len(published) == 1
    event = published[0]
    assert event["type"] == "notifications"
    assert event["count"] == 1  # one unread of the two
    assert [i["id"] for i in event["items"]] == ["1", "2"]
    assert [i["id"] for i in event["new"]] == ["1", "2"]
    # Only the mention warrants an OS-level interrupt.
    assert [n.id for n in fired] == ["1"]


async def test_poll_once_prime_publishes_with_no_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes = [_note("1", "mention")]
    _stub_notifications(monkeypatch, notes)

    published: list[dict] = []
    fired: list[Notification] = []

    async def publish(event: dict) -> None:
        published.append(event)

    async def notify(n: Notification) -> None:  # pragma: no cover - must not run
        fired.append(n)

    seen: set[str] = set()
    await notifier.poll_once(seen, notify, prime=True, publish=publish)

    event = published[0]
    assert [i["id"] for i in event["items"]] == ["1"]  # full state still pushed
    assert event["new"] == []  # priming round never flags existing as new
    assert fired == []  # and never OS-notifies


# --- fire_test_notification: delivered True / False -----------------------


async def test_fire_test_notification_delivered_when_notify_send_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifier, "notify_send_available", lambda: True)
    calls: list[tuple[str, str]] = []

    async def fake_run(title: str, body: str) -> bool:
        calls.append((title, body))
        return True

    monkeypatch.setattr(notifier, "_run_notify_send", fake_run)
    published: list[dict] = []

    async def publish(event: dict) -> None:
        published.append(event)

    delivered = await notifier.fire_test_notification(publish=publish)

    assert delivered is True
    assert calls and calls[0][0] == "git-automation"
    assert published == [{"type": "test"}]


async def test_fire_test_notification_not_delivered_when_notify_send_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifier, "notify_send_available", lambda: False)
    ran: list[int] = []

    async def fake_run(title: str, body: str) -> bool:  # pragma: no cover - must not run
        ran.append(1)
        return True

    monkeypatch.setattr(notifier, "_run_notify_send", fake_run)
    published: list[dict] = []

    async def publish(event: dict) -> None:
        published.append(event)

    delivered = await notifier.fire_test_notification(publish=publish)

    assert delivered is False
    assert ran == []  # notify-send absent -> not invoked
    assert published == [{"type": "test"}]  # in-app push still fires


async def test_fire_test_notification_not_delivered_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifier, "notify_send_available", lambda: True)

    async def fake_run(title: str, body: str) -> bool:
        return False  # notify-send present but failed

    monkeypatch.setattr(notifier, "_run_notify_send", fake_run)

    async def publish(_event: dict) -> None:
        return None

    assert await notifier.fire_test_notification(publish=publish) is False


# --- WebSocket forwarder + endpoint surface -------------------------------


class _FakeWS:
    """Minimal WebSocket capturing sends; disconnects after ``limit`` frames."""

    def __init__(self, limit: int) -> None:
        self.sent: list[dict] = []
        self._limit = limit

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        if len(self.sent) >= self._limit:
            raise WebSocketDisconnect()


async def test_forward_events_drains_queue_until_disconnect() -> None:
    queue: asyncio.Queue[dict] = asyncio.Queue()
    queue.put_nowait({"type": "a"})
    queue.put_nowait({"type": "b"})
    ws = _FakeWS(limit=2)

    await github_events._forward_events(queue, ws)  # type: ignore[arg-type]

    assert ws.sent == [{"type": "a"}, {"type": "b"}]


def _events_app() -> FastAPI:
    app = FastAPI()
    app.include_router(github_events.router, prefix="/api")
    return app


def test_events_ws_rejects_foreign_origin() -> None:
    """A cross-site Origin is rejected at the handshake — no subscription opens."""
    test_client = TestClient(_events_app())

    with pytest.raises(StarletteWSDisconnect) as exc:
        with test_client.websocket_connect(
            "/api/github/events",
            headers={"origin": "https://evil.example.com"},
        ):
            pass
    assert exc.value.code == 1008


def test_test_notification_endpoint_reports_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fire(publish=None) -> bool:
        return True

    monkeypatch.setattr(notifier, "fire_test_notification", fake_fire)

    resp = TestClient(_events_app()).post("/api/github/test-notification")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "delivered": True}


def test_test_notification_endpoint_ok_when_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fire(publish=None) -> bool:
        return False

    monkeypatch.setattr(notifier, "fire_test_notification", fake_fire)

    resp = TestClient(_events_app()).post("/api/github/test-notification")
    assert resp.status_code == 200  # still 200 so the UI can explain
    assert resp.json() == {"ok": True, "delivered": False}
