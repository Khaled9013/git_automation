"""Self-contained GitHub cockpit module (notifications + issues via ``gh``).

This package is the exposable seam future tools (e.g. the agentic workflow)
consume directly. It has a clean, typed, UI-agnostic Python interface in
:mod:`client` and pydantic models in :mod:`models`; the HTTP surface in
``web/api/github.py`` is just one consumer.
"""

from __future__ import annotations
