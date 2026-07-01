"""WebSocket endpoint streaming a real shell over a pseudo-terminal.

Security / trust model
----------------------
``WS /api/terminal?path=`` gives the connected client an interactive shell with
the privileges of the server process. This is intentional but dangerous: it is
only safe because the application binds ``127.0.0.1`` (see ``web/app.py``) and
must never be exposed on a non-loopback interface or fronted by a proxy that
forwards remote clients.

Each socket owns exactly one :class:`PtySession`. The session is always torn
down -- killing the child process and closing the PTY -- when the socket closes
for any reason (clean disconnect, client error, or shell exit). See
:mod:`git_automation.core.terminal` for the underlying guarantees.

Wire protocol:

* client -> server (JSON text frames):
    ``{"type": "input", "data": "<text>"}`` -- feed text to the shell.
    ``{"type": "resize", "cols": <int>, "rows": <int>}`` -- resize the tty.
* server -> client:
    raw binary frames carrying terminal output, and a JSON
    ``{"type": "error", ...}`` / ``{"type": "exit"}`` control frame.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from git_automation.core.errors import GitAutomationError
from git_automation.core.terminal import PtySession
from git_automation.web.ws import WS_POLICY_VIOLATION, origin_allowed, safe_close

router = APIRouter()


async def _pump_output(session: PtySession, websocket: WebSocket) -> None:
    """Forward PTY output to the client until the shell reaches EOF."""
    while True:
        data = await session.read()
        if data is None:
            break
        try:
            await websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError):
            break


@router.websocket("/terminal")
async def terminal_socket(websocket: WebSocket, path: str = Query(...)) -> None:
    """Relay a client WebSocket to a shell running on a PTY in ``path``."""
    if not origin_allowed(websocket.headers.get("origin")):
        # Reject the handshake outright (no accept) so a foreign-origin page
        # never gets a shell. Defends against Cross-Site WebSocket Hijacking.
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept()

    try:
        session = PtySession(path)
        await session.start()
    except GitAutomationError as exc:
        await websocket.send_json({"type": "error", "code": exc.code, "message": exc.message})
        await websocket.close()
        return

    output_task = asyncio.create_task(_pump_output(session, websocket))

    # When the shell exits, unblock the receive loop by closing the socket.
    def _on_output_done(_task: asyncio.Task[None]) -> None:
        asyncio.create_task(safe_close(websocket))

    output_task.add_done_callback(_on_output_done)

    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            if msg_type == "input":
                await session.write(str(message.get("data", "")))
            elif msg_type == "resize":
                try:
                    session.resize(int(message["cols"]), int(message["rows"]))
                except (KeyError, TypeError, ValueError):
                    # Ignore malformed resize frames rather than tearing down.
                    continue
            # Unknown frame types are ignored defensively.
    except (WebSocketDisconnect, RuntimeError, ValueError):
        # WebSocketDisconnect: client went away. RuntimeError: socket closed by
        # the output pump. ValueError: receive_json got a non-JSON frame.
        pass
    finally:
        output_task.cancel()
        await session.terminate()
        await safe_close(websocket)
