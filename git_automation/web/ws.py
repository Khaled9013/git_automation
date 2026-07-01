"""Shared WebSocket security helpers for the loopback-only web surface.

Every WebSocket route in this app (``/terminal``, ``/watch``, ``/github/events``)
hands a same-origin browser tab a privileged or activity-revealing channel. This
is only safe because the app binds ``127.0.0.1`` (see :mod:`git_automation.web.__main__`).

Unlike the JSON ``/api`` endpoints -- which a cross-origin page cannot reach
because the ``application/json`` content type forces a CORS preflight -- a browser
may open a *cross-site* WebSocket to ``127.0.0.1`` with **no** preflight
(Cross-Site WebSocket Hijacking). The only trustworthy signal available at
handshake time is the ``Origin`` header, which browsers always send and page
JavaScript cannot forge.

These helpers are the single source of truth for that guard so every WS router
enforces it identically:

* :func:`origin_allowed` -- gate a handshake on a loopback-local ``Origin``.
* :func:`safe_close` -- close a socket, ignoring an already-closed error.
* :data:`WS_POLICY_VIOLATION` -- the RFC 6455 close code used to reject a
  foreign-origin handshake *before* :meth:`~starlette.websockets.WebSocket.accept`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import WebSocket

# Loopback hosts that a *same-origin* browser tab serving this app would present
# in its ``Origin`` header. Anything else is a foreign site.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Policy-violation close code (RFC 6455) used to reject foreign-origin sockets.
WS_POLICY_VIOLATION = 1008


def origin_allowed(origin: str | None) -> bool:
    """Return True when an ``Origin`` header is absent or loopback-local.

    A privileged/activity-revealing WebSocket must not be reachable from
    arbitrary websites. A browser may open a *cross-site* WebSocket to
    ``127.0.0.1`` with **no** CORS preflight (Cross-Site WebSocket Hijacking);
    the ``Origin`` header is the only handshake-time signal, and page JavaScript
    cannot forge it. We therefore require it to be loopback-local. A missing
    ``Origin`` (non-browser clients such as the CLI or tests) is allowed.

    Args:
        origin: The value of the request's ``Origin`` header, or ``None`` when
            it is absent (non-browser clients).

    Returns:
        True when the handshake should proceed; False to reject it.
    """
    if origin is None:
        return True
    try:
        host = urlparse(origin).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


async def safe_close(websocket: WebSocket) -> None:
    """Close ``websocket`` ignoring the error if it is already closed."""
    try:
        await websocket.close()
    except RuntimeError:
        pass
