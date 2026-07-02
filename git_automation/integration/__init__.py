"""Integration/host layer: composable tools mounted by a thin FastAPI host.

The design (see ``docs/superpowers/specs``) treats each capability as an
independent, standalone *tool* that can boot on its own. This package defines
the :class:`ToolModule` contract and the registry of available tools; the web
host (:func:`git_automation.web.app.create_app`) composes a selected subset of
them plus the always-mounted shared ``identity`` router.
"""

from __future__ import annotations

from .tools import ToolModule, available_tools

__all__ = ["ToolModule", "available_tools"]
