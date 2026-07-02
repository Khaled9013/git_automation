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
by setting ``GITAUTO_ALLOW_NONLOOPBACK=1``; this logs a loud warning and proceeds
(and also disables the in-app Host guard -- see
:mod:`git_automation.web.security`). The host itself is configurable via
``GITAUTO_HOST`` (default ``127.0.0.1``) and the port via ``GITAUTO_PORT``
(default ``8000``).

Unified boot
------------
This is the *one* guarded entry point: ``make web``, ``make web-git`` /
``web-github``, and the ``git-automation`` console script all come through
:func:`main`, which validates host and tool selection and then hands uvicorn the
``git_automation.web.app:app`` import string. The module-level ``app`` object
itself honors ``GITAUTO_TOOL`` (see :mod:`git_automation.web.config`), so the
selection is identical in reload mode (``GITAUTO_RELOAD=1``), where uvicorn's
reloader subprocess re-imports the app from scratch.
"""

from __future__ import annotations

import logging
import os

from git_automation.web import config
from git_automation.web.security import (
    ALLOW_NONLOOPBACK_ENV,
    is_loopback_host,
    nonloopback_allowed,
)

logger = logging.getLogger(__name__)

# Back-compat aliases: the canonical implementations moved to the shared
# ``web.config`` / ``web.security`` modules (single source of truth).
_selected_tools = config.parse_tool_selection
_is_loopback_host = is_loopback_host


def _resolve_host() -> str:
    """Return the host to bind, enforcing the loopback fail-closed policy.

    Refuses to return a non-loopback host unless the operator has explicitly set
    ``GITAUTO_ALLOW_NONLOOPBACK`` to a truthy value, in which case a loud warning
    is logged and the requested host is honored.

    Raises:
        SystemExit: when a non-loopback host is requested without the opt-out.
    """
    host = os.environ.get(config.HOST_ENV, config.DEFAULT_HOST)
    if is_loopback_host(host):
        return host

    if nonloopback_allowed():
        logger.warning(
            "SECURITY: binding non-loopback host %r because %s is set. The "
            "unauthenticated terminal/watch/GitHub WebSocket surface is now "
            "reachable off-host -- only do this behind an authenticating proxy.",
            host,
            ALLOW_NONLOOPBACK_ENV,
        )
        return host

    raise SystemExit(
        f"Refusing to start: {config.HOST_ENV}={host!r} is not a loopback address.\n"
        "This app exposes an unauthenticated shell/WebSocket surface and is only "
        "safe on 127.0.0.1/::1/localhost.\n"
        f"To override (advanced; front it with your own auth), set "
        f"{ALLOW_NONLOOPBACK_ENV}=1."
    )


def _validate_tools() -> None:
    """Pre-validate ``GITAUTO_TOOL`` so a typo exits cleanly, not as a traceback.

    The module-level app performs the same selection on import; validating here
    (before uvicorn spawns/imports anything) turns an unknown tool name into an
    actionable message on *every* boot path, including reload mode where the
    import happens in uvicorn's reloader subprocess.

    Raises:
        SystemExit: when ``GITAUTO_TOOL`` names an unknown tool.
    """
    tools = config.tools_from_env()
    if tools is None:
        return
    from git_automation.integration import available_tools

    registry = available_tools()
    unknown = [name for name in tools if name not in registry]
    if unknown:
        raise SystemExit(
            f"Refusing to start: unknown tool(s) {unknown!r}; "
            f"available: {sorted(registry)}.\n"
            f"Set {config.TOOL_ENV} to a comma-separated list of valid tool "
            f"names, or leave it unset to mount all tools."
        )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    host = _resolve_host()
    port = config.port_from_env()
    _validate_tools()

    # The import string (rather than an app object) lets uvicorn's reloader
    # re-import the app in its subprocess; the module-level app re-reads
    # GITAUTO_TOOL there, so reload mode serves the same selection.
    uvicorn.run(
        "git_automation.web.app:app",
        host=host,
        port=port,
        reload=config.reload_from_env(),
    )


if __name__ == "__main__":
    main()
