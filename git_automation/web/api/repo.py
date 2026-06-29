"""Repository inspection and remote git-operation API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core.git.client import fetch, get_status, list_remotes, pull, push
from git_automation.core.models import CommandResult, Remote, RepoStatus

router = APIRouter()


class GitOpRequest(BaseModel):
    """Body shared by fetch/pull operations."""

    path: str
    remote: str
    branch: str | None = None


class PushRequest(GitOpRequest):
    """Body for push, allowing ``set_upstream`` for a first push."""

    set_upstream: bool = False


@router.get("/repo")
async def read_repo(path: str = Query(...)) -> RepoStatus:
    """Return the status snapshot of the repository at ``path``."""
    return await get_status(path)


@router.get("/repo/remotes")
async def read_repo_remotes(path: str = Query(...)) -> list[Remote]:
    """Return the configured remotes of the repository at ``path``."""
    return await list_remotes(path)


@router.post("/git/fetch")
async def git_fetch(body: GitOpRequest) -> CommandResult:
    """Fetch the chosen remote."""
    return await fetch(body.path, body.remote)


@router.post("/git/pull")
async def git_pull(body: GitOpRequest) -> CommandResult:
    """Pull from the chosen remote (optionally a specific branch)."""
    return await pull(body.path, body.remote, body.branch)


@router.post("/git/push")
async def git_push(body: PushRequest) -> CommandResult:
    """Push to the chosen remote (optionally setting upstream)."""
    return await push(body.path, body.remote, body.branch, body.set_upstream)
