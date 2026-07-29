"""Shared WebSocket security helpers for the loopback-only web surface.

Every WebSocket route in this app (``/terminal``, ``/watch``, ``/github/events``)
hands a same-origin browser tab a privileged or activity-revealing channel. This
is only safe because the app binds ``127.0.0.1`` -- optionally extended to the
machine's own Tailscale tailnet, whose peers are the operator's own devices
(see :mod:`git_automation.web.__main__` / :mod:`git_automation.web.security`).

Unlike the JSON ``/api`` endpoints -- which a cross-origin page cannot reach
because the ``application/json`` content type forces a CORS preflight -- a browser
may open a *cross-site* WebSocket to ``127.0.0.1`` with **no** preflight
(Cross-Site WebSocket Hijacking). The only trustworthy signal available at
handshake time is the ``Origin`` header, which browsers always send and page
JavaScript cannot forge.

These helpers are the single source of truth for that guard so every WS router
enforces it identically:

* :func:`origin_allowed` -- gate a handshake on a trusted (loopback or
  own-tailnet) ``Origin``.
* :func:`safe_close` -- close a socket, ignoring an already-closed error.
* :data:`WS_POLICY_VIOLATION` -- the RFC 6455 close code used to reject a
  foreign-origin handshake *before* :meth:`~starlette.websockets.WebSocket.accept`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import WebSocket

from git_automation.web.security import is_trusted_host

# Policy-violation close code (RFC 6455) used to reject foreign-origin sockets.
WS_POLICY_VIOLATION = 1008


def origin_allowed(origin: str | None, *, expected_port: int | None = None) -> bool:
    """Return True when an ``Origin`` header is absent or from a trusted host.

    A privileged/activity-revealing WebSocket must not be reachable from
    arbitrary websites. A browser may open a *cross-site* WebSocket to
    ``127.0.0.1`` with **no** CORS preflight (Cross-Site WebSocket Hijacking);
    the ``Origin`` header is the only handshake-time signal, and page JavaScript
    cannot forge it. We therefore require its host to be trusted -- loopback or
    the machine's own tailnet identity (:func:`~git_automation.web.security.is_trusted_host`),
    mirroring the Host guard. A missing ``Origin`` (non-browser clients such as
    the CLI or tests) is allowed.

    Loopback host alone is **not** a full same-origin check: another local dev
    server (e.g. a page served on ``http://localhost:3000``) shares the loopback
    host but is a *different origin*, and would otherwise be able to open our
    socket. When the server knows its own port, pass it as ``expected_port`` to
    additionally pin the Origin's port -- so only a genuinely same-origin page
    (same host **and** same port) is allowed.

    Port-pin rules (only applied when ``expected_port is not None``):

    * If the Origin carries an explicit port that differs from ``expected_port``,
      reject it -- it is a different loopback origin.
    * If the Origin carries **no** port, the browser normalizes it to the scheme
      default (80/443), which will not equal our custom port, so it is treated as
      a different origin and rejected. This is safe: a real same-origin page
      served by this app on its port always includes that port in ``Origin``.

    When ``expected_port is None`` (the current call sites, which do not pass it)
    no port check is performed, preserving the loopback-host-only behavior.

    Args:
        origin: The value of the request's ``Origin`` header, or ``None`` when
            it is absent (non-browser clients).
        expected_port: The server's own port. When provided, the Origin's port
            must match it; when ``None``, no port check is done.

    Returns:
        True when the handshake should proceed; False to reject it.
    """
    if origin is None:
        return True
    try:
        parsed = urlparse(origin)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if host is None or not is_trusted_host(host):
        return False
    if expected_port is not None and port != expected_port:
        return False
    return True


async def safe_close(websocket: WebSocket) -> None:
    """Close ``websocket`` ignoring the error if it is already closed."""
    try:
        await websocket.close()
    except RuntimeError:
        pass
