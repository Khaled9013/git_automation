"""Stash API endpoints (save / pop / apply / drop).

Drop is destructive; the UI confirms before calling it.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from git_automation.core.git.client import (
    stash,
    stash_apply,
    stash_drop,
    stash_pop,
)
from git_automation.core.models import CommandResult

router = APIRouter()


class StashRequest(BaseModel):
    """Body for saving the working tree onto the stash stack."""

    path: str
    message: str | None = None
    include_untracked: bool = False


class StashPopRequest(BaseModel):
    """Body for popping a stash entry (default: the most recent)."""

    path: str
    index: int | None = None


class StashIndexRequest(BaseModel):
    """Body for applying or dropping a specific stash ``index``."""

    path: str
    index: int


@router.post("/git/stash")
async def git_stash(body: StashRequest) -> CommandResult:
    """Save the working-tree changes onto the stash stack."""
    return await stash(body.path, body.message, body.include_untracked)


@router.post("/git/stash/pop")
async def git_stash_pop(body: StashPopRequest) -> CommandResult:
    """Apply and remove a stash entry."""
    return await stash_pop(body.path, body.index)


@router.post("/git/stash/apply")
async def git_stash_apply(body: StashIndexRequest) -> CommandResult:
    """Apply a stash entry without removing it."""
    return await stash_apply(body.path, body.index)


@router.post("/git/stash/drop")
async def git_stash_drop(body: StashIndexRequest) -> CommandResult:
    """Drop a stash entry (destructive)."""
    return await stash_drop(body.path, body.index)
