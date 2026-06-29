"""Working-tree, branch, and commit-graph API endpoints.

Destructive operations (discard, branch delete, merge) just run the git
command; the UI confirms before sending the request.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core.git.client import (
    checkout_branch,
    commit,
    create_branch,
    delete_branch,
    discard,
    get_changes,
    get_diff,
    get_graph,
    list_branches,
    merge_branch,
    stage,
    unstage,
)
from git_automation.core.models import (
    BranchList,
    Changes,
    CommandResult,
    DiffResult,
    GraphCommit,
)

router = APIRouter()


class FilesRequest(BaseModel):
    """Body for stage/unstage/discard operations on a set of files."""

    path: str
    files: list[str]


class CommitRequest(BaseModel):
    """Body for committing the staged changes."""

    path: str
    message: str


class BranchCreateRequest(BaseModel):
    """Body for creating a branch, optionally checking it out."""

    path: str
    name: str
    checkout: bool = False


class BranchNameRequest(BaseModel):
    """Body for checkout/merge operations targeting a branch ``name``."""

    path: str
    name: str


class BranchDeleteRequest(BaseModel):
    """Body for deleting a branch, optionally forcing unmerged deletion."""

    path: str
    name: str
    force: bool = False


class GraphResponse(BaseModel):
    """The commit graph response wrapper."""

    commits: list[GraphCommit]


# --- Working tree ---------------------------------------------------------


@router.get("/repo/changes")
async def read_changes(path: str = Query(...)) -> Changes:
    """Return the working-tree changes of the repository at ``path``."""
    return await get_changes(path)


@router.get("/repo/diff")
async def read_diff(
    path: str = Query(...),
    file: str = Query(...),
    staged: bool = Query(False),
) -> DiffResult:
    """Return the unified diff for ``file`` in the repository at ``path``."""
    return await get_diff(path, file, staged)


@router.post("/git/stage")
async def git_stage(body: FilesRequest) -> CommandResult:
    """Stage the given files."""
    return await stage(body.path, body.files)


@router.post("/git/unstage")
async def git_unstage(body: FilesRequest) -> CommandResult:
    """Unstage the given files."""
    return await unstage(body.path, body.files)


@router.post("/git/discard")
async def git_discard(body: FilesRequest) -> CommandResult:
    """Discard working-tree changes to the given files (destructive)."""
    return await discard(body.path, body.files)


@router.post("/git/commit")
async def git_commit(body: CommitRequest) -> CommandResult:
    """Commit the staged changes with the given message."""
    return await commit(body.path, body.message)


# --- Branches -------------------------------------------------------------


@router.get("/repo/branches")
async def read_branches(path: str = Query(...)) -> BranchList:
    """Return the local and remote branches of the repository at ``path``."""
    return await list_branches(path)


@router.post("/git/branch/create")
async def git_branch_create(body: BranchCreateRequest) -> CommandResult:
    """Create a branch, optionally checking it out."""
    return await create_branch(body.path, body.name, body.checkout)


@router.post("/git/branch/checkout")
async def git_branch_checkout(body: BranchNameRequest) -> CommandResult:
    """Switch to the given branch."""
    return await checkout_branch(body.path, body.name)


@router.post("/git/branch/delete")
async def git_branch_delete(body: BranchDeleteRequest) -> CommandResult:
    """Delete the given branch (destructive)."""
    return await delete_branch(body.path, body.name, body.force)


@router.post("/git/branch/merge")
async def git_branch_merge(body: BranchNameRequest) -> CommandResult:
    """Merge the given branch into the current branch (destructive)."""
    return await merge_branch(body.path, body.name)


# --- Commit graph ---------------------------------------------------------


@router.get("/repo/graph")
async def read_graph(
    path: str = Query(...),
    limit: int = Query(200),
) -> GraphResponse:
    """Return the commit graph (all refs, date-order) for ``path``."""
    return GraphResponse(commits=await get_graph(path, limit))
