"""Working-tree, branch, and commit-graph API endpoints.

Destructive operations (discard, branch delete, merge) just run the git
command; the UI confirms before sending the request.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core.git.client import (
    checkout_branch,
    checkout_ref,
    cherry_pick,
    commit,
    create_branch,
    delete_branch,
    delete_files,
    discard,
    get_changes,
    get_commit_detail,
    get_diff,
    get_graph,
    get_reflog,
    get_refs,
    list_branches,
    merge_branch,
    redo,
    reset,
    stage,
    undo,
    unstage,
)
from git_automation.core.models import (
    BranchList,
    Changes,
    CommandResult,
    CommitDetail,
    DiffResult,
    GraphCommit,
    MergeResult,
    ReflogEntry,
    RefsBundle,
    UndoResult,
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


class CheckoutRequest(BaseModel):
    """Body for checking out a branch name or a commit SHA (detached)."""

    path: str
    ref: str


class ResetRequest(BaseModel):
    """Body for moving HEAD to ``sha`` with a reset ``mode``."""

    path: str
    sha: str
    mode: str


class ShaRequest(BaseModel):
    """Body for an operation targeting a single commit ``sha``."""

    path: str
    sha: str


class PathRequest(BaseModel):
    """Body for an operation that only needs a repository ``path``."""

    path: str


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


@router.post("/git/delete")
async def git_delete(body: FilesRequest) -> CommandResult:
    """Delete the given working-tree files (tracked via ``git rm``; destructive)."""
    return await delete_files(body.path, body.files)


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
async def git_branch_merge(body: BranchNameRequest) -> MergeResult:
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


# --- Refs sidebar ---------------------------------------------------------


@router.get("/repo/refs")
async def read_refs(path: str = Query(...)) -> RefsBundle:
    """Return branches, tags, worktrees, and stashes for the sidebar."""
    return await get_refs(path)


# --- Commit detail --------------------------------------------------------


@router.get("/repo/commit")
async def read_commit(
    path: str = Query(...),
    sha: str = Query(...),
) -> CommitDetail:
    """Return full metadata and changed files for commit ``sha``."""
    return await get_commit_detail(path, sha)


# --- Navigation / history rewrite -----------------------------------------


@router.post("/git/checkout")
async def git_checkout(body: CheckoutRequest) -> CommandResult:
    """Check out a branch name or commit SHA (detached HEAD)."""
    return await checkout_ref(body.path, body.ref)


@router.post("/git/reset")
async def git_reset(body: ResetRequest) -> CommandResult:
    """Move HEAD to ``sha`` with the given reset mode (destructive)."""
    return await reset(body.path, body.sha, body.mode)


@router.post("/git/cherry-pick")
async def git_cherry_pick(body: ShaRequest) -> CommandResult:
    """Apply commit ``sha`` onto the current branch."""
    return await cherry_pick(body.path, body.sha)


# --- Undo / Redo (reflog) -------------------------------------------------


@router.get("/repo/reflog")
async def read_reflog(
    path: str = Query(...),
    limit: int = Query(50),
) -> list[ReflogEntry]:
    """Return the HEAD reflog entries powering undo/redo."""
    return await get_reflog(path, limit)


@router.post("/git/undo")
async def git_undo(body: PathRequest) -> UndoResult:
    """Move HEAD back one reflog step (best-effort)."""
    return await undo(body.path)


@router.post("/git/redo")
async def git_redo(body: PathRequest) -> UndoResult:
    """Move HEAD forward one reflog step (best-effort)."""
    return await redo(body.path)
