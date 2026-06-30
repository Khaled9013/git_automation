"""WebSocket endpoint that streams repository change *notifications*.

``WS /api/watch?path=`` watches the repo working tree and pushes a JSON frame
``{"type": "change", "paths": [...]}`` whenever relevant files change, so the
UI can auto-refresh. The ``paths`` are repo-relative hints only -- this endpoint
**never streams file contents**; clients re-query the JSON ``/api`` endpoints to
get the actual state.

Security / trust model
----------------------
Like the terminal WebSocket, this endpoint is only safe because the app binds
``127.0.0.1``. A cross-site page can open a WebSocket to ``127.0.0.1`` with no
CORS preflight (Cross-Site WebSocket Hijacking), so we reject any non-loopback
``Origin`` with close code 1008 *before* accepting the handshake. A missing
``Origin`` (non-browser clients/tests) is allowed. The Origin check and the
safe-close helper are reused verbatim from :mod:`git_automation.web.api.terminal`
to keep a single source of truth.

Each socket owns exactly one watcher task. The watcher is always cancelled and
awaited -- so its background OS watcher is torn down -- when the socket closes
for any reason (client disconnect, watcher error, or server shutdown).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from git_automation.core.errors import GitAutomationError
from git_automation.core.watcher import watch_repo
from git_automation.web.api.terminal import (
    _WS_POLICY_VIOLATION,
    _origin_allowed,
    _safe_close,
)

router = APIRouter()


async def _stream_changes(path: str, websocket: WebSocket) -> None:
    """Forward watcher notifications to the client until it goes away."""
    try:
        async for paths in watch_repo(path):
            try:
                await websocket.send_json({"type": "change", "paths": paths})
            except (WebSocketDisconnect, RuntimeError):
                # Client disconnected or the socket was closed underneath us.
                break
    except GitAutomationError as exc:
        # The watched directory became invalid mid-stream; report and stop.
        with suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json(
                {"type": "error", "code": exc.code, "message": exc.message}
            )


@router.websocket("/watch")
async def watch_socket(websocket: WebSocket, path: str = Query(...)) -> None:
    """Stream ``{"type":"change","paths":[...]}`` frames for the repo at ``path``."""
    if not _origin_allowed(websocket.headers.get("origin")):
        # Reject the handshake (no accept) so a foreign-origin page can never
        # observe repository activity. Defends against Cross-Site WS Hijacking.
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    await websocket.accept()

    # Validate the path up front so an invalid repo gets a clean error frame
    # rather than a silently dead socket.
    try:
        from git_automation.core.process import validate_repo_path

        validate_repo_path(path)
    except GitAutomationError as exc:
        await websocket.send_json({"type": "error", "code": exc.code, "message": exc.message})
        await _safe_close(websocket)
        return

    watch_task = asyncio.create_task(_stream_changes(path, websocket))

    # When the watcher stops on its own (error), unblock the receive loop below.
    def _on_watch_done(_task: asyncio.Task[None]) -> None:
        asyncio.create_task(_safe_close(websocket))

    watch_task.add_done_callback(_on_watch_done)

    try:
        # We do not consume client input, but receiving lets us detect the
        # disconnect promptly so we can tear the watcher down.
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        # WebSocketDisconnect: client went away. RuntimeError: socket closed by
        # the watcher's done-callback.
        pass
    finally:
        watch_task.cancel()
        # Awaiting the cancelled task guarantees awatch's finally runs, so the
        # background OS watcher is torn down (no leaked watch task).
        with suppress(asyncio.CancelledError):
            await watch_task
        await _safe_close(websocket)
