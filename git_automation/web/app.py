"""FastAPI application factory and app instance."""

from __future__ import annotations

from fastapi import FastAPI

from git_automation import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="git-automation", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
