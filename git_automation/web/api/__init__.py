"""FastAPI API layer: routers and the domain-error handler."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from git_automation.core.errors import GitAutomationError

from . import (
    fs,
    github,
    github_events,
    gitops,
    identity,
    merge,
    pr,
    repo,
    stash,
    terminal,
    watch,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(identity.router)
api_router.include_router(repo.router)
api_router.include_router(gitops.router)
api_router.include_router(fs.router)
api_router.include_router(merge.router)
api_router.include_router(stash.router)
api_router.include_router(pr.router)
api_router.include_router(terminal.router)
api_router.include_router(watch.router)
api_router.include_router(github.router)
api_router.include_router(github_events.router)


def register_error_handlers(app: FastAPI) -> None:
    """Register the handler that renders :class:`GitAutomationError` as JSON."""

    @app.exception_handler(GitAutomationError)
    async def _handle_domain_error(_request: Request, exc: GitAutomationError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


__all__ = ["api_router", "register_error_handlers"]
