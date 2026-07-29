"""Single source of truth for the web app's environment configuration.

Every boot path -- ``make web``, ``make web-git``/``web-github``, the
``git-automation`` console script, or a raw ``uvicorn git_automation.web.app:app``
-- reads the same variables through these helpers, so no path can drift:

* ``GITAUTO_TOOL``   -- comma-separated tool names; unset/empty mounts all.
* ``GITAUTO_HOST``   -- comma-separated bind hosts; the token ``tailscale``
  expands to the machine's Tailscale IPs. Unset means loopback plus the
  Tailscale IPs when a tailnet is detected (policy enforced by
  :mod:`git_automation.web.__main__` / :mod:`git_automation.web.security`).
* ``GITAUTO_PORT``   -- bind port (default ``8000``).
* ``GITAUTO_RELOAD`` -- truthy enables uvicorn auto-reload (dev convenience).
"""

from __future__ import annotations

import os

TOOL_ENV = "GITAUTO_TOOL"
HOST_ENV = "GITAUTO_HOST"
PORT_ENV = "GITAUTO_PORT"
RELOAD_ENV = "GITAUTO_RELOAD"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_TRUTHY = {"1", "true", "yes", "on"}


def parse_tool_selection(raw: str | None) -> list[str] | None:
    """Parse a ``GITAUTO_TOOL`` value into a tool-selection list.

    Comma-separated names, whitespace-trimmed, with empty segments dropped
    (e.g. ``"git, github"`` -> ``["git", "github"]``). An unset, empty, or
    all-blank value yields ``None``, which tells ``create_app`` to mount every
    tool (the default). Pure so it is unit-testable without a server.
    """
    if raw is None:
        return None
    names = [segment.strip() for segment in raw.split(",")]
    names = [name for name in names if name]
    return names or None


def tools_from_env() -> list[str] | None:
    """Return the tool selection from ``GITAUTO_TOOL`` (``None`` = all tools)."""
    return parse_tool_selection(os.environ.get(TOOL_ENV))


def parse_host_selection(raw: str | None) -> list[str] | None:
    """Parse a ``GITAUTO_HOST`` value into an ordered bind-host list.

    Comma-separated hosts, whitespace-trimmed, with empty segments and
    duplicates dropped (e.g. ``"127.0.0.1, tailscale"`` ->
    ``["127.0.0.1", "tailscale"]``). An unset, empty, or all-blank value
    yields ``None``, which tells the entry point to apply the default policy
    (loopback, plus the machine's Tailscale IPs when a tailnet is detected).
    Pure so it is unit-testable without a server.
    """
    if raw is None:
        return None
    hosts: list[str] = []
    for segment in raw.split(","):
        host = segment.strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts or None


def hosts_from_env() -> list[str] | None:
    """Return the bind hosts from ``GITAUTO_HOST`` (``None`` = default policy)."""
    return parse_host_selection(os.environ.get(HOST_ENV))


def reload_from_env() -> bool:
    """Return whether ``GITAUTO_RELOAD`` requests uvicorn auto-reload."""
    return os.environ.get(RELOAD_ENV, "").strip().lower() in _TRUTHY


def port_from_env() -> int:
    """Return the bind port from ``GITAUTO_PORT`` (default ``8000``)."""
    return int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))
