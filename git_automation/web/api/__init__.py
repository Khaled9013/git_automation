"""FastAPI API layer: routers and the domain-error handler.

The web host (:func:`git_automation.web.app.create_app`) now composes routes
from the :mod:`git_automation.integration` tool registry. This module keeps the
individual router submodules importable and continues to expose ``api_router``
(all tools + the shared ``identity`` router) and ``register_error_handlers``
for back-compat with external code and tests.
"""

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


def _build_api_router() -> APIRouter:
    """All tools + the shared identity router, under ``/api``.

    Kept as back-compat for external importers of ``api_router``. It mounts
    every router this package exposes (the union of the ``git`` and ``github``
    tools plus the shared ``identity`` router), matching what
    ``create_app(tools=None)`` serves. Built directly from the submodules here
    -- rather than via :func:`git_automation.integration.available_tools` -- to
    avoid a module-load circular import (``integration.tools`` imports these
    same submodules from this package).
    """
    router = APIRouter(prefix="/api")
    for sub in (
        identity,
        repo,
        gitops,
        fs,
        merge,
        stash,
        terminal,
        watch,
        pr,
        github,
        github_events,
    ):
        router.include_router(sub.router)
    return router


api_router = _build_api_router()


def register_error_handlers(app: FastAPI) -> None:
    """Register the handler that renders :class:`GitAutomationError` as JSON."""

    @app.exception_handler(GitAutomationError)
    async def _handle_domain_error(_request: Request, exc: GitAutomationError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


__all__ = ["api_router", "register_error_handlers"]
