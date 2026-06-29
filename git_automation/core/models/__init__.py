"""Typed domain models for the Phase 1 slice (identity + remote-aware git ops)."""

from __future__ import annotations

from pydantic import BaseModel


class Remote(BaseModel):
    """A configured git remote."""

    name: str
    url: str


class Upstream(BaseModel):
    """The upstream tracking ref of a branch."""

    remote: str
    branch: str


class RepoStatus(BaseModel):
    """A snapshot of a repository's branch/remote/sync state."""

    path: str
    current_branch: str | None
    upstream: Upstream | None
    remotes: list[Remote]
    ahead: int
    behind: int
    dirty: bool


class IdentityState(BaseModel):
    """Combined git identity + ``gh`` auth state used for onboarding."""

    git_name: str | None
    git_email: str | None
    gh_authenticated: bool
    gh_user: str | None
    needs_onboarding: bool


class CommandResult(BaseModel):
    """The result of a git command surfaced to the API."""

    ok: bool
    output: str


class FsEntry(BaseModel):
    """A single directory entry returned by the filesystem browser."""

    name: str
    path: str
    is_dir: bool
    is_git_repo: bool


class FileChange(BaseModel):
    """A single changed file with its git short status code (M/A/D/R/C/U)."""

    path: str
    status: str


class Changes(BaseModel):
    """The working-tree change sets split by stage state."""

    staged: list[FileChange]
    unstaged: list[FileChange]
    untracked: list[str]


class Branch(BaseModel):
    """A local branch with its upstream tracking and ahead/behind counts."""

    name: str
    is_current: bool
    upstream: str | None
    ahead: int
    behind: int


class BranchRef(BaseModel):
    """A remote-tracking branch reference (name only)."""

    name: str


class BranchList(BaseModel):
    """The branches of a repository, split into local and remote refs."""

    current: str | None
    local: list[Branch]
    remote: list[BranchRef]


class DiffResult(BaseModel):
    """A unified diff for a single file."""

    file: str
    diff: str
    binary: bool


class GraphCommit(BaseModel):
    """A commit node for the interactive commit graph."""

    sha: str
    short: str
    parents: list[str]
    author: str
    date: str
    subject: str
    refs: list[str]
    is_head: bool


__all__ = [
    "Remote",
    "Upstream",
    "RepoStatus",
    "IdentityState",
    "CommandResult",
    "FsEntry",
    "FileChange",
    "Changes",
    "Branch",
    "BranchRef",
    "BranchList",
    "DiffResult",
    "GraphCommit",
]
