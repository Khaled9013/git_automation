"""FastAPI application factory and app instance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from git_automation import __version__
from git_automation.web.api import api_router, register_error_handlers

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


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="git-automation", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    register_error_handlers(app)
    app.include_router(api_router)

    # Serve the single-page UI from web/static/ when it exists. Mounted last so
    # /health and /api/* take precedence. It may not exist yet (owned by the
    # frontend agents) -- handled gracefully.
    if _STATIC_DIR.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app


app = create_app()
