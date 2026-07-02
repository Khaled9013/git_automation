"""Single source of truth for the web app's environment configuration.

Every boot path -- ``make web``, ``make web-git``/``web-github``, the
``git-automation`` console script, or a raw ``uvicorn git_automation.web.app:app``
-- reads the same variables through these helpers, so no path can drift:

* ``GITAUTO_TOOL``   -- comma-separated tool names; unset/empty mounts all.
* ``GITAUTO_HOST``   -- bind host (default ``127.0.0.1``; policy enforced by
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


def reload_from_env() -> bool:
    """Return whether ``GITAUTO_RELOAD`` requests uvicorn auto-reload."""
    return os.environ.get(RELOAD_ENV, "").strip().lower() in _TRUTHY


def port_from_env() -> int:
    """Return the bind port from ``GITAUTO_PORT`` (default ``8000``)."""
    return int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))
