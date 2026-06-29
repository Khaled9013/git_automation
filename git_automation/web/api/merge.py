"""Merge lifecycle and 3-way conflict-resolution API endpoints.

Backs the visual merge editor: start a merge, inspect its conflicts, fetch the
three stages of a conflicted file, write a resolution, then continue or abort.
Destructive actions are confirmed by the UI before they reach these routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core.git.client import (
    get_conflict,
    merge_abort,
    merge_branch,
    merge_continue,
    merge_status,
    resolve_conflict,
)
from git_automation.core.models import (
    CommandResult,
    Conflict,
    MergeResult,
    MergeStatus,
)

router = APIRouter()


class MergeRequest(BaseModel):
    """Body for merging branch ``name`` into the current branch."""

    path: str
    name: str


class ResolveRequest(BaseModel):
    """Body for writing the resolved ``content`` of a conflicted ``file``."""

    path: str
    file: str
    content: str


class MergeContinueRequest(BaseModel):
    """Body for finalizing an in-progress merge, with an optional message."""

    path: str
    message: str | None = None


class PathRequest(BaseModel):
    """Body for an operation that only needs a repository ``path``."""

    path: str


@router.post("/git/merge")
async def git_merge(body: MergeRequest) -> MergeResult:
    """Merge branch ``name`` into the current branch, reporting conflicts."""
    return await merge_branch(body.path, body.name)


@router.get("/repo/merge-status")
async def read_merge_status(path: str = Query(...)) -> MergeStatus:
    """Return whether a merge is in progress and its unmerged paths."""
    return await merge_status(path)


@router.get("/repo/conflict")
async def read_conflict(
    path: str = Query(...),
    file: str = Query(...),
) -> Conflict:
    """Return the three merge stages plus the working copy of ``file``."""
    return await get_conflict(path, file)


@router.post("/git/resolve")
async def git_resolve(body: ResolveRequest) -> CommandResult:
    """Write the resolved content of a conflicted file and stage it."""
    return await resolve_conflict(body.path, body.file, body.content)


@router.post("/git/merge/continue")
async def git_merge_continue(body: MergeContinueRequest) -> CommandResult:
    """Commit the in-progress merge (errors if conflicts remain)."""
    return await merge_continue(body.path, body.message)


@router.post("/git/merge/abort")
async def git_merge_abort(body: PathRequest) -> CommandResult:
    """Abort the in-progress merge and restore the pre-merge state."""
    return await merge_abort(body.path)
