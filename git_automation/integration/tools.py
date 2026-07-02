"""The composable tool registry.

A :class:`ToolModule` bundles everything the host needs to mount one tool: a
name, the :class:`~fastapi.APIRouter` carrying its HTTP/WS routes, and an
optional ``lifespan`` running any background work for the tool's lifetime.

Two tools are exposed via :func:`available_tools`:

* ``git`` -- local repository operations (``repo, gitops, fs, merge, stash,
  terminal, watch``). Purely request/response, so no lifespan.
* ``github`` -- GitHub integration (``pr, github, github_events``). Its
  lifespan runs the notification poller.

The ``identity`` router (onboarding for both the local git user and ``gh``
auth) is deliberately *not* a tool: it is shared and always mounted by the
host regardless of which tools are selected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI

from git_automation.core.github import gh_cli, notifier
from git_automation.core.github.hub import hub
from git_automation.web.api import (
    fs,
    github,
    github_events,
    gitops,
    merge,
    pr,
    repo,
    stash,
    terminal,
    watch,
)

logger = logging.getLogger(__name__)

_Lifespan = Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]


@dataclass
class ToolModule:
    """One mountable tool: its name, router, and optional background lifespan."""

    name: str
    router: APIRouter
    lifespan: _Lifespan | None = None


@contextlib.asynccontextmanager
async def _github_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the GitHub notification poller for the app's lifetime.

    On startup, if ``gh`` is authenticated, launch the poller as a background
    task; on shutdown, cancel it cleanly. A failure here must never crash app
    startup, so everything is guarded and logged.
    """
    task: asyncio.Task[None] | None = None
    try:
        authenticated, _ = await gh_cli.auth_status()
        if authenticated:
            # One poll loop drives both OS notifications and the live browser
            # push (via hub.publish -> WS /api/github/events).
            task = asyncio.create_task(notifier.run_poller(publish=hub.publish))
    except Exception:  # noqa: BLE001 - startup must never crash
        logger.warning("could not start GitHub notifier", exc_info=True)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            # The poller re-raises CancelledError on cancel (a BaseException,
            # not caught by suppress(Exception)); suppress it too for a clean,
            # crash-free shutdown.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _git_router() -> APIRouter:
    """Router for the local-git tool. Prefix ``/api`` is applied by the host."""
    router = APIRouter()
    router.include_router(repo.router)
    router.include_router(gitops.router)
    router.include_router(fs.router)
    router.include_router(merge.router)
    router.include_router(stash.router)
    router.include_router(terminal.router)
    router.include_router(watch.router)
    return router


def _github_router() -> APIRouter:
    """Router for the GitHub tool. Prefix ``/api`` is applied by the host."""
    router = APIRouter()
    router.include_router(pr.router)
    router.include_router(github.router)
    router.include_router(github_events.router)
    return router


def available_tools() -> dict[str, ToolModule]:
    """Return the registry of composable tools, keyed by name.

    A fresh mapping (and fresh routers) is built on each call so callers cannot
    mutate shared state.
    """
    return {
        "git": ToolModule(name="git", router=_git_router(), lifespan=None),
        "github": ToolModule(
            name="github", router=_github_router(), lifespan=_github_lifespan
        ),
    }


__all__ = ["ToolModule", "available_tools"]
