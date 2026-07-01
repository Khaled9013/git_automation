"""Typed domain models for the Phase 1 slice (identity + remote-aware git ops)."""

from __future__ import annotations

from pydantic import BaseModel

# ``PullRequest`` is a GitHub concept and now lives beside the other GitHub
# models in :mod:`git_automation.core.github.models`. It is re-exported here for
# backward compatibility so any lingering ``from core.models import PullRequest``
# keeps working; new code should import it from ``core.github.models``.
from git_automation.core.github.models import PullRequest as PullRequest


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


# --- Slice 3: refs sidebar, commit detail, merge/conflicts, stash, reflog ---


class Tag(BaseModel):
    """A git tag reference (name only)."""

    name: str


class Worktree(BaseModel):
    """A linked or main working tree of a repository."""

    path: str
    branch: str | None
    is_current: bool


class Stash(BaseModel):
    """A single stash entry (``git stash list``)."""

    index: int
    message: str


class RefsBundle(BaseModel):
    """The full set of refs powering the sidebar (branches/tags/worktrees/stashes)."""

    local: list[Branch]
    remote: list[BranchRef]
    tags: list[Tag]
    worktrees: list[Worktree]
    stashes: list[Stash]


class CommitFile(BaseModel):
    """A single file changed by a commit, with line-count deltas."""

    path: str
    status: str
    additions: int
    deletions: int


class CommitDetail(BaseModel):
    """Full metadata and changed-file list for a single commit."""

    sha: str
    short: str
    parents: list[str]
    author: str
    email: str
    date: str
    subject: str
    body: str
    refs: list[str]
    files: list[CommitFile]


class MergeResult(BaseModel):
    """The outcome of a merge/pull, including any conflicting paths."""

    ok: bool
    output: str
    conflicted: bool
    conflicts: list[str]


class MergeStatus(BaseModel):
    """The in-progress merge state of a repository."""

    merging: bool
    conflicts: list[str]
    message: str


class Conflict(BaseModel):
    """The three merge stages plus the working copy of a conflicted file.

    Any stage that is absent (e.g. a file added on only one side, or deleted on
    one side) is ``None`` rather than an empty string.
    """

    file: str
    base: str | None
    ours: str | None
    theirs: str | None
    merged: str | None
    binary: bool


class ReflogEntry(BaseModel):
    """A single ``git reflog`` entry used by undo/redo."""

    selector: str
    subject: str


class UndoResult(BaseModel):
    """The outcome of a reflog-based undo/redo step."""

    ok: bool
    output: str
    undone: str


# --- Slice 4: hunk staging --------------------------------------------------


class HunkLine(BaseModel):
    """A single line within a diff hunk.

    ``type`` is the leading git diff marker: ``" "`` (context), ``"+"``
    (added), or ``"-"`` (removed). ``text`` is the line content with that
    marker stripped.
    """

    type: str
    text: str


class Hunk(BaseModel):
    """A single ``@@`` hunk of a file's unified diff.

    ``patch`` is a self-contained, applyable patch: the file header lines
    (``diff --git``/``---``/``+++``) plus this one hunk, suitable for piping to
    ``git apply`` to stage or unstage exactly this hunk.
    """

    header: str
    patch: str
    lines: list[HunkLine]


class FileHunks(BaseModel):
    """The per-hunk breakdown of a single file's diff (staged or unstaged)."""

    file: str
    binary: bool
    hunks: list[Hunk]


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
    "Tag",
    "Worktree",
    "Stash",
    "RefsBundle",
    "CommitFile",
    "CommitDetail",
    "MergeResult",
    "MergeStatus",
    "Conflict",
    "ReflogEntry",
    "UndoResult",
    "HunkLine",
    "Hunk",
    "FileHunks",
    "PullRequest",
]
