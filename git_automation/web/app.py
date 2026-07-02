"""FastAPI application factory and app instance.

The app is a thin *host* that mounts a selection of composable tools (see
:mod:`git_automation.integration`) plus the always-present shared ``identity``
router. Each selected tool contributes its routes and, optionally, a lifespan
running its background work; the host composes those lifespans into the app's
single lifespan via an :class:`~contextlib.AsyncExitStack`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from git_automation import __version__
from git_automation.integration import ToolModule, available_tools
from git_automation.web.api import identity, register_error_handlers

_STATIC_DIR = Path(__file__).parent / "static"


class _NoCacheStaticFiles(StaticFiles):
    """Serve static assets with revalidation so browsers never run stale JS/CSS.

    ``no-cache`` lets the browser keep a copy but forces a conditional request
    (ETag/Last-Modified) on every load, so an updated asset is always fetched
    fresh while unchanged ones still return a cheap 304. Without this the SPA's
    JS/CSS get cached aggressively and a rebuild appears not to take effect.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _select_tools(tools: list[str] | None) -> list[ToolModule]:
    """Resolve requested tool names to :class:`ToolModule` objects.

    ``tools=None`` selects every available tool (preserving today's all-tools
    behavior). An unknown name raises :class:`ValueError`.
    """
    registry = available_tools()
    if tools is None:
        return list(registry.values())
    selected: list[ToolModule] = []
    for name in tools:
        try:
            selected.append(registry[name])
        except KeyError:
            raise ValueError(
                f"unknown tool {name!r}; available: {sorted(registry)}"
            ) from None
    return selected


def create_app(tools: list[str] | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    :param tools: names of the tools to mount. ``None`` (the default) mounts
        every tool, which is byte-identical to the historical single-router
        behavior (all routes present, GitHub notifier running). A list selects
        a subset; the shared ``identity`` router is always mounted regardless.
        An unknown tool name raises :class:`ValueError`.
    """
    selected = _select_tools(tools)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Compose the selected tools' lifespans into the app's lifespan."""
        async with contextlib.AsyncExitStack() as stack:
            for tool in selected:
                if tool.lifespan is not None:
                    await stack.enter_async_context(tool.lifespan(app))
            yield

    app = FastAPI(title="git-automation", version=__version__, lifespan=_lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    register_error_handlers(app)

    # The shared identity router (git-user onboarding + gh auth) is mounted for
    # every configuration; each selected tool contributes its own routes. All
    # live under the /api prefix, exactly as the historical single api_router.
    api = APIRouter(prefix="/api")
    api.include_router(identity.router)
    for tool in selected:
        api.include_router(tool.router)
    app.include_router(api)

    # Serve the single-page UI from web/static/ when it exists. Mounted last so
    # /health and /api/* take precedence. It may not exist yet (owned by the
    # frontend agents) -- handled gracefully.
    if _STATIC_DIR.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app


app = create_app()
