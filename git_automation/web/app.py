"""FastAPI application factory and app instance."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from git_automation import __version__
from git_automation.web.api import api_router, register_error_handlers

_STATIC_DIR = Path(__file__).parent / "static"


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
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app


app = create_app()
