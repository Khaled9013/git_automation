"""Entry point: run the web app with uvicorn.

Trust model
-----------
The entire security model of this app rests on binding interfaces only trusted
clients can reach. The WebSocket surface (``/api/terminal`` hands out a real
shell; ``/api/watch`` and ``/api/github/events`` stream repository/GitHub
activity) is *unauthenticated* -- it is only safe because the bound interfaces
are unreachable to strangers. Two interfaces qualify:

* **loopback** -- only the local user;
* the machine's own **Tailscale tailnet** IPs -- only the operator's own
  authenticated devices (see :mod:`git_automation.web.tailscale`).

Anything else (a LAN address, ``0.0.0.0``) would expose the PTY/WS surface to
untrusted hosts, so we **fail closed**: refuse to start unless every bind host
is loopback or a Tailscale-range IP.

Advanced users who front the app with their own authenticating proxy can opt out
by setting ``GITAUTO_ALLOW_NONLOOPBACK=1``; this logs a loud warning and proceeds
(and also disables the in-app Host guard -- see
:mod:`git_automation.web.security`). The hosts are configurable via
``GITAUTO_HOST`` (comma-separated; the token ``tailscale`` expands to the
machine's Tailscale IPs) and the port via ``GITAUTO_PORT`` (default ``8000``).
When ``GITAUTO_HOST`` is unset the app binds loopback **plus** the Tailscale
IPs when a running tailnet is detected, so it works on ``http://127.0.0.1:8000``
and from the operator's other tailnet devices out of the box; set
``GITAUTO_HOST=127.0.0.1`` to stay loopback-only.

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

from git_automation.web import config, tailscale
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

# GITAUTO_HOST entry that expands to the machine's detected Tailscale IPs.
TAILSCALE_HOST_TOKEN = "tailscale"


def _check_host_allowed(host: str) -> None:
    """Enforce the fail-closed bind policy for a single host.

    Loopback passes silently; a Tailscale-range IP passes with a note (the
    tailnet is the operator's own devices, but the exposure is worth logging);
    anything else requires the ``GITAUTO_ALLOW_NONLOOPBACK`` opt-out, which
    logs a loud warning and proceeds.

    Raises:
        SystemExit: when a non-loopback, non-Tailscale host is requested
            without the opt-out.
    """
    if is_loopback_host(host):
        return

    if tailscale.is_tailscale_ip(host):
        logger.info(
            "Binding Tailscale address %r: the app is reachable from devices "
            "on your tailnet. Set %s=127.0.0.1 for loopback only.",
            host,
            config.HOST_ENV,
        )
        return

    if nonloopback_allowed():
        logger.warning(
            "SECURITY: binding non-loopback host %r because %s is set. The "
            "unauthenticated terminal/watch/GitHub WebSocket surface is now "
            "reachable off-host -- only do this behind an authenticating proxy.",
            host,
            ALLOW_NONLOOPBACK_ENV,
        )
        return

    raise SystemExit(
        f"Refusing to start: {config.HOST_ENV}={host!r} is not a loopback or "
        "Tailscale address.\n"
        "This app exposes an unauthenticated shell/WebSocket surface and is only "
        "safe on 127.0.0.1/::1/localhost or the machine's own tailnet IPs.\n"
        f"To override (advanced; front it with your own auth), set "
        f"{ALLOW_NONLOOPBACK_ENV}=1."
    )


def _resolve_hosts() -> list[str]:
    """Return the hosts to bind, enforcing the fail-closed policy on each.

    ``GITAUTO_HOST`` unset applies the default policy: loopback, plus the
    machine's Tailscale IPs when a running tailnet is detected -- so the app
    serves localhost *and* the operator's other tailnet devices out of the
    box. An explicit value is honored as given: each comma-separated entry is
    validated by :func:`_check_host_allowed`, and the ``tailscale`` token
    expands to the detected Tailscale IPs (a clean error when there are none).

    Raises:
        SystemExit: when a host fails the bind policy, or the ``tailscale``
            token is used with no running tailnet.
    """
    requested = config.hosts_from_env()

    if requested is None:
        hosts = [config.DEFAULT_HOST]
        identity = tailscale.self_identity()
        if identity is not None:
            hosts.extend(identity.ips)
            names = ", ".join(identity.dns_names) or "no MagicDNS name"
            logger.info(
                "Tailscale detected: also binding %s (%s) -- reachable from "
                "your tailnet devices. Set %s=127.0.0.1 for loopback only.",
                ", ".join(identity.ips),
                names,
                config.HOST_ENV,
            )
        return hosts

    hosts = []
    for entry in requested:
        if entry.lower() == TAILSCALE_HOST_TOKEN:
            identity = tailscale.self_identity()
            if identity is None:
                raise SystemExit(
                    f"Refusing to start: {config.HOST_ENV} includes "
                    f"{TAILSCALE_HOST_TOKEN!r} but no running Tailscale was "
                    "detected (is `tailscale up` done and the CLI on PATH?)."
                )
            hosts.extend(ip for ip in identity.ips if ip not in hosts)
        elif entry not in hosts:
            hosts.append(entry)

    for host in hosts:
        _check_host_allowed(host)
    return hosts


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
    hosts = _resolve_hosts()
    port = config.port_from_env()
    _validate_tools()
    reload = config.reload_from_env()

    # The import string (rather than an app object) lets uvicorn's reloader
    # re-import the app in its subprocess; the module-level app re-reads
    # GITAUTO_TOOL there, so reload mode serves the same selection.
    if len(hosts) == 1:
        uvicorn.run("git_automation.web.app:app", host=hosts[0], port=port, reload=reload)
        return

    # uvicorn.run() binds a single host. For loopback + Tailscale we pre-bind
    # one socket per host and hand them all to a single server -- or, in
    # reload mode, to the reload supervisor, which passes them to the
    # re-spawned server process (the same mechanism uvicorn.run() uses for
    # its one socket).
    configs = [
        uvicorn.Config("git_automation.web.app:app", host=host, port=port, reload=reload)
        for host in hosts
    ]
    sockets = [cfg.bind_socket() for cfg in configs]
    server = uvicorn.Server(configs[0])
    if configs[0].should_reload:
        from uvicorn.supervisors import ChangeReload

        ChangeReload(configs[0], target=server.run, sockets=sockets).run()
    else:
        server.run(sockets=sockets)


if __name__ == "__main__":
    main()
