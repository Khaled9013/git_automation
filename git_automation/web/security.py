"""Loopback trust helpers and the Host-header guard middleware.

Why a Host guard exists
-----------------------
The app's whole security model is "only the local user can reach us"
(see :mod:`git_automation.web.__main__`). Two browser-side defenses back that
up for pages from *other* origins:

* JSON ``/api`` endpoints force a CORS preflight (the ``application/json``
  content type), which a cross-origin page cannot satisfy.
* WebSocket routes check the ``Origin`` header (:mod:`git_automation.web.ws`).

Neither helps against **DNS rebinding**: an attacker's page at
``http://evil.com:8000`` whose DNS record is re-pointed at ``127.0.0.1``
becomes *same-origin* with this server in the victim's browser -- no CORS, no
preflight, and the ``Origin`` header matches the page itself. The one signal
that survives is the ``Host`` header, which the browser always sets to the
attacker's hostname (``evil.com:8000``). :class:`HostGuardMiddleware` therefore
rejects any request whose ``Host`` is not a loopback name/address, closing the
rebinding vector for the HTTP API and (as defense-in-depth alongside the Origin
check) for the WebSocket routes.

Operators who deliberately front the app with their own authenticating proxy
(the documented ``GITAUTO_ALLOW_NONLOOPBACK`` opt-out) get arbitrary ``Host``
values by design, so the same opt-out disables this guard.

This module is also the single source of truth for "what counts as loopback",
shared by the WS Origin guard and the entry point's bind-host policy.
"""

from __future__ import annotations

import os
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Names/addresses a same-origin loopback client would present (in ``Host`` or
# ``Origin``). Literal-IP loopback ranges (127.0.0.0/8, ::1) are additionally
# accepted by :func:`is_loopback_host`.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

ALLOW_NONLOOPBACK_ENV = "GITAUTO_ALLOW_NONLOOPBACK"

# Policy-violation close code (RFC 6455), mirrored from the WS guard so the
# middleware rejects WebSocket handshakes identically.
_WS_POLICY_VIOLATION = 1008


def nonloopback_allowed() -> bool:
    """Return whether the operator opted out of loopback-only enforcement.

    Truthy values of ``GITAUTO_ALLOW_NONLOOPBACK`` (``1``/``true``/``yes``/
    ``on``, case-insensitive) mean the app is deliberately fronted by an
    authenticating proxy; both the bind-host policy and the Host guard honor it.
    """
    value = os.environ.get(ALLOW_NONLOOPBACK_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def is_loopback_host(host: str) -> bool:
    """Return True when ``host`` names the loopback interface.

    Accepts the well-known loopback names/addresses directly, and treats any
    literal IP inside a loopback range (e.g. the whole ``127.0.0.0/8`` block or
    an IPv6-mapped ``::ffff:127.0.0.1``) as loopback too. A hostname that is
    not a literal IP (and not in the allow-list) is considered non-loopback:
    it could resolve anywhere, so we fail closed.
    """
    normalized = host.strip().strip("[]").lower()
    if normalized in LOOPBACK_HOSTS:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        # Not a literal IP address (an arbitrary hostname) -> not trusted here.
        return False


def host_header_hostname(host_header: str | None) -> str | None:
    """Extract the lowercase hostname from a ``Host`` header value.

    Handles ``name``, ``name:port``, and bracketed IPv6 (``[::1]:8000`` ->
    ``::1``). Returns ``None`` for a missing, empty, or malformed value --
    callers must treat ``None`` as "cannot be trusted".
    """
    if not host_header:
        return None
    try:
        parsed = urlsplit(f"//{host_header.strip()}")
        parsed.port  # noqa: B018 - property access validates the port digits
        return parsed.hostname
    except ValueError:
        return None


class HostGuardMiddleware:
    """Reject HTTP requests / WS handshakes whose ``Host`` is not loopback.

    Pure ASGI middleware so it covers both ``http`` and ``websocket`` scopes
    (Starlette's ``TrustedHostMiddleware`` also mishandles bracketed IPv6
    hosts, which this app must accept for ``[::1]``). A missing ``Host`` header
    is allowed: HTTP/1.1 clients and every browser always send one, so its
    absence implies a non-browser client -- the same trust call the WS Origin
    guard makes for a missing ``Origin``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        raw_host = next(
            (value for name, value in scope.get("headers", []) if name == b"host"),
            None,
        )
        if raw_host is None:
            await self.app(scope, receive, send)
            return

        hostname = host_header_hostname(raw_host.decode("latin-1"))
        if hostname is not None and is_loopback_host(hostname):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Close before accept, exactly like the routes' Origin rejection.
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})
            return

        response = JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_host",
                    "message": (
                        "Rejected non-loopback Host header. This app only serves "
                        "localhost; see GITAUTO_ALLOW_NONLOOPBACK for proxy setups."
                    ),
                }
            },
        )
        await response(scope, receive, send)
