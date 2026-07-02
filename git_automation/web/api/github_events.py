"""Live GitHub-event push: a fan-out WebSocket plus a test-notification trigger.

One backend poll loop (:mod:`git_automation.core.github.notifier`) drives both the
OS notification *and* a live push to connected browsers. The push path is:

    notifier.poll_once  ->  hub.publish(event)  ->  every subscriber queue
                                                 ->  WS /github/events -> browser

``WS /github/events`` subscribes to :data:`git_automation.core.github.hub.hub`
and forwards each published event as JSON until the client disconnects. It carries
no privileges, but — like the terminal/watch sockets — a cross-site page could
open it against ``127.0.0.1`` with no CORS preflight (Cross-Site WebSocket
Hijacking) and observe the user's GitHub activity, so we reject any non-loopback
``Origin`` *before* accepting the handshake, reusing the shared guard in
:mod:`git_automation.web.ws`.

``POST /github/test-notification`` fires a one-off alert on demand so the user can
confirm OS notifications work; it returns ``200`` even when ``notify-send`` is
missing (``delivered=False``) so the UI can explain the situation.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from git_automation.core.github import notifier
from git_automation.core.github.hub import hub
from git_automation.web.ws import WS_POLICY_VIOLATION, origin_allowed, safe_close

router = APIRouter()


async def _forward_events(queue: asyncio.Queue[dict], websocket: WebSocket) -> None:
    """Forward each event drained from ``queue`` to the client until it goes away."""
    while True:
        event = await queue.get()
        try:
            await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            # Client disconnected or the socket was closed underneath us.
            break


@router.websocket("/github/events")
async def github_events_socket(websocket: WebSocket) -> None:
    """Forward published GitHub events to the client until it disconnects."""
    if not origin_allowed(websocket.headers.get("origin"), expected_port=websocket.url.port):
        # Reject the handshake (no accept) so a foreign-origin page can never
        # observe the user's GitHub activity. Defends against Cross-Site WS
        # Hijacking exactly as the terminal/watch sockets do. expected_port also
        # rejects a same-host page on a different loopback port.
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept()

    queue = hub.subscribe()
    forward_task = asyncio.create_task(_forward_events(queue, websocket))

    # When the forwarder stops on its own (send failed), unblock the receive loop.
    def _on_forward_done(_task: asyncio.Task[None]) -> None:
        asyncio.create_task(safe_close(websocket))

    forward_task.add_done_callback(_on_forward_done)

    try:
        # We do not consume client input, but receiving lets us detect the
        # disconnect promptly so we can tear the subscription down.
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        # WebSocketDisconnect: client went away. RuntimeError: socket closed by
        # the forwarder's done-callback.
        pass
    finally:
        forward_task.cancel()
        with suppress(asyncio.CancelledError):
            await forward_task
        # Always drop our subscriber queue so the hub never leaks it.
        hub.unsubscribe(queue)
        await safe_close(websocket)


@router.post("/github/test-notification")
async def test_notification() -> dict[str, bool]:
    """Fire a test OS + in-app notification; report whether the OS alert landed."""
    delivered = await notifier.fire_test_notification()
    return {"ok": True, "delivered": delivered}
