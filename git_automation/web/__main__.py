"""Entry point: run the web app with uvicorn.

Trust model
-----------
The entire security model of this app rests on binding a **loopback** interface.
The WebSocket surface (``/api/terminal`` hands out a real shell; ``/api/watch``
and ``/api/github/events`` stream repository/GitHub activity) is *unauthenticated*
-- it is only safe because a remote host cannot reach ``127.0.0.1``. Binding a
non-loopback host would expose that PTY/WS surface to the network, so we
**fail closed**: refuse to start unless the host is loopback.

Advanced users who front the app with their own authenticating proxy can opt out
by setting ``GITAUTO_ALLOW_NONLOOPBACK=1``; this logs a loud warning and proceeds.
The host itself is configurable via ``GITAUTO_HOST`` (default ``127.0.0.1``) and
the port via ``GITAUTO_PORT`` (default ``8000``).
"""

from __future__ import annotations

import logging
import os
from ipaddress import ip_address

logger = logging.getLogger(__name__)

# Hostnames/addresses that keep the server unreachable from other machines.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

_ALLOW_NONLOOPBACK_ENV = "GITAUTO_ALLOW_NONLOOPBACK"


def _is_loopback_host(host: str) -> bool:
    """Return True when binding ``host`` keeps the server loopback-only.

    Accepts the well-known loopback names/addresses directly, and treats any
    address inside a loopback IP range (e.g. the whole ``127.0.0.0/8`` block or
    an IPv6-mapped ``::ffff:127.0.0.1``) as loopback too. A hostname that is not
    a literal IP (and not in the allow-list) is considered non-loopback: it
    could resolve to a routable address, so we fail closed.
    """
    normalized = host.strip().strip("[]").lower()
    if normalized in _LOOPBACK_HOSTS:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        # Not a literal IP address (an arbitrary hostname) -> not trusted here.
        return False


def _resolve_host() -> str:
    """Return the host to bind, enforcing the loopback fail-closed policy.

    Refuses to return a non-loopback host unless the operator has explicitly set
    ``GITAUTO_ALLOW_NONLOOPBACK`` to a truthy value, in which case a loud warning
    is logged and the requested host is honored.

    Raises:
        SystemExit: when a non-loopback host is requested without the opt-out.
    """
    host = os.environ.get("GITAUTO_HOST", "127.0.0.1")
    if _is_loopback_host(host):
        return host

    allow = os.environ.get(_ALLOW_NONLOOPBACK_ENV, "").strip().lower()
    if allow in {"1", "true", "yes", "on"}:
        logger.warning(
            "SECURITY: binding non-loopback host %r because %s is set. The "
            "unauthenticated terminal/watch/GitHub WebSocket surface is now "
            "reachable off-host -- only do this behind an authenticating proxy.",
            host,
            _ALLOW_NONLOOPBACK_ENV,
        )
        return host

    raise SystemExit(
        f"Refusing to start: GITAUTO_HOST={host!r} is not a loopback address.\n"
        "This app exposes an unauthenticated shell/WebSocket surface and is only "
        "safe on 127.0.0.1/::1/localhost.\n"
        f"To override (advanced; front it with your own auth), set "
        f"{_ALLOW_NONLOOPBACK_ENV}=1."
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    host = _resolve_host()
    port = int(os.environ.get("GITAUTO_PORT", "8000"))
    uvicorn.run("git_automation.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
