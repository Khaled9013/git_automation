"""Async wrapper around the local ``git`` CLI.

All commands run non-blocking via :mod:`git_automation.core.process`. Functions
that operate on a repository validate the path first.
"""

from __future__ import annotations

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.models import (
    Branch,
    BranchList,
    BranchRef,
    Changes,
    CommandResult,
    DiffResult,
    FileChange,
    GraphCommit,
    Remote,
    RepoStatus,
    Upstream,
)
from git_automation.core.process import validate_repo_path

# Unit-separator delimiter for plumbing output; cannot appear in ref names.
_SEP = "\x1f"


async def run_git(args: list[str], cwd: str | None = None) -> CommandResult:
    """Run ``git`` with ``args`` and return a :class:`CommandResult`.

    Args:
        args: Arguments passed after ``git`` (e.g. ``["status", "--porcelain"]``).
        cwd: Directory to run git in, if any.

    Returns:
        A :class:`CommandResult` with ``ok`` and combined output.
    """
    result = await process.run_process(["git", *args], cwd=cwd)
    return CommandResult(ok=result.ok, output=result.combined)


async def _config_get_global(key: str) -> str | None:
    """Return a global git config value, or ``None`` if it is unset."""
    result = await process.run_process(["git", "config", "--global", "--get", key])
    value = result.stdout.strip()
    return value if result.ok and value else None


async def get_global_identity() -> tuple[str | None, str | None]:
    """Return the global git ``(user.name, user.email)``; ``None`` where unset."""
    name = await _config_get_global("user.name")
    email = await _config_get_global("user.email")
    return name, email


async def set_global_identity(name: str, email: str) -> None:
    """Set the global git ``user.name`` and ``user.email``."""
    await run_git(["config", "--global", "user.name", name])
    await run_git(["config", "--global", "user.email", email])


async def list_remotes(path: str) -> list[Remote]:
    """List the configured remotes of the repository at ``path``.

    Args:
        path: Path to a local repository (validated as an existing directory).

    Returns:
        One :class:`Remote` per configured remote, in configuration order.
    """
    cwd = validate_repo_path(path)
    result = await run_git(["remote", "-v"], cwd=cwd)
    remotes: dict[str, str] = {}
    for line in result.output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])
    return [Remote(name=name, url=url) for name, url in remotes.items()]


async def _current_branch(cwd: str) -> str | None:
    """Return the current branch name, or ``None`` if detached/unborn-empty."""
    result = await run_git(["branch", "--show-current"], cwd=cwd)
    branch = result.output.strip()
    return branch or None


async def _upstream(cwd: str) -> Upstream | None:
    """Return the current branch's upstream tracking ref, or ``None``."""
    result = await run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=cwd,
    )
    full = result.output.strip()
    if not result.ok or "/" not in full:
        return None
    remote, branch = full.split("/", 1)
    return Upstream(remote=remote, branch=branch)


async def _ahead_behind(cwd: str) -> tuple[int, int]:
    """Return ``(ahead, behind)`` relative to the current branch's upstream."""
    result = await run_git(
        ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        cwd=cwd,
    )
    parts = result.output.split()
    if not result.ok or len(parts) != 2:
        return 0, 0
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0
    return ahead, behind


async def get_status(path: str) -> RepoStatus:
    """Return a :class:`RepoStatus` snapshot for the repository at ``path``.

    Args:
        path: Path to a local repository (validated as an existing directory).
    """
    cwd = validate_repo_path(path)
    current_branch = await _current_branch(cwd)
    upstream = await _upstream(cwd)
    ahead, behind = await _ahead_behind(cwd) if upstream else (0, 0)
    remotes = await list_remotes(cwd)
    dirty_result = await run_git(["status", "--porcelain"], cwd=cwd)
    dirty = bool(dirty_result.output.strip())
    return RepoStatus(
        path=path,
        current_branch=current_branch,
        upstream=upstream,
        remotes=remotes,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
    )


async def fetch(path: str, remote: str) -> CommandResult:
    """Fetch ``remote`` for the repository at ``path``."""
    cwd = validate_repo_path(path)
    return await run_git(["fetch", remote], cwd=cwd)


async def pull(path: str, remote: str, branch: str | None = None) -> CommandResult:
    """Pull ``remote`` (optionally a specific ``branch``) into ``path``."""
    cwd = validate_repo_path(path)
    args = ["pull", remote]
    if branch:
        args.append(branch)
    return await run_git(args, cwd=cwd)


async def push(
    path: str,
    remote: str,
    branch: str | None = None,
    set_upstream: bool = False,
) -> CommandResult:
    """Push to ``remote`` from ``path``.

    Args:
        path: Path to a local repository.
        remote: The remote to push to.
        branch: The branch to push; defaults to git's current-branch behavior.
        set_upstream: When True, pass ``--set-upstream`` (for a first push).
    """
    cwd = validate_repo_path(path)
    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    args.append(remote)
    if branch:
        args.append(branch)
    return await run_git(args, cwd=cwd)


# --- Working tree: changes / stage / commit / diff ------------------------


async def get_changes(path: str) -> Changes:
    """Return the working-tree changes split into staged/unstaged/untracked.

    Args:
        path: Path to a local repository (validated as an existing directory).

    Returns:
        A :class:`Changes` snapshot parsed from ``git status --porcelain``.
        ``status`` is the git short code (M/A/D/R/C/U).
    """
    cwd = validate_repo_path(path)
    result = await run_git(["status", "--porcelain"], cwd=cwd)
    staged: list[FileChange] = []
    unstaged: list[FileChange] = []
    untracked: list[str] = []
    for line in result.output.splitlines():
        if len(line) < 3:
            continue
        index_status, worktree_status, entry = line[0], line[1], line[3:]
        if index_status == "?" and worktree_status == "?":
            untracked.append(entry)
            continue
        # Renames/copies are reported as "ORIG -> NEW"; show the new path.
        display = entry.split(" -> ", 1)[-1] if " -> " in entry else entry
        if index_status not in (" ", "?"):
            staged.append(FileChange(path=display, status=index_status))
        if worktree_status not in (" ", "?"):
            unstaged.append(FileChange(path=display, status=worktree_status))
    return Changes(staged=staged, unstaged=unstaged, untracked=untracked)


async def stage(path: str, files: list[str]) -> CommandResult:
    """Stage ``files`` in the repository at ``path`` (``git add``)."""
    cwd = validate_repo_path(path)
    return await run_git(["add", "--", *files], cwd=cwd)


async def unstage(path: str, files: list[str]) -> CommandResult:
    """Unstage ``files`` in the repository at ``path`` (``git restore --staged``)."""
    cwd = validate_repo_path(path)
    return await run_git(["restore", "--staged", "--", *files], cwd=cwd)


async def discard(path: str, files: list[str]) -> CommandResult:
    """Discard working-tree changes to ``files`` (``git restore``).

    Destructive: overwrites the working-tree copy with the index version. The
    UI confirms before calling this.
    """
    cwd = validate_repo_path(path)
    return await run_git(["restore", "--", *files], cwd=cwd)


async def commit(path: str, message: str) -> CommandResult:
    """Commit the staged changes in ``path`` with ``message``.

    Args:
        path: Path to a local repository.
        message: The commit message; must be non-empty.

    Returns:
        A :class:`CommandResult`. A failed result (with git's output) is
        returned when nothing is staged.

    Raises:
        GitAutomationError: ``empty_commit_message`` if ``message`` is blank.
    """
    cwd = validate_repo_path(path)
    if not message.strip():
        raise GitAutomationError(
            "empty_commit_message",
            "A commit message is required.",
            400,
        )
    return await run_git(["commit", "-m", message], cwd=cwd)


def _is_binary_diff(diff: str) -> bool:
    """Return True when a unified diff describes a binary change."""
    return "Binary files" in diff or "GIT binary patch" in diff


async def get_diff(path: str, file: str, staged: bool = False) -> DiffResult:
    """Return the unified diff of ``file`` in the repository at ``path``.

    Args:
        path: Path to a local repository.
        file: Repository-relative path of the file to diff.
        staged: When True, diff the staged version (``--cached``).

    Returns:
        A :class:`DiffResult`. For untracked files (no tracked diff), the new
        file's content is shown via ``git diff --no-index``.
    """
    cwd = validate_repo_path(path)
    args = ["diff"]
    if staged:
        args.append("--cached")
    args += ["--", file]
    result = await run_git(args, cwd=cwd)
    diff = result.output
    if not diff and not staged:
        # Untracked file: render its full content as an added-file diff.
        no_index = await run_git(["diff", "--no-index", "--", "/dev/null", file], cwd=cwd)
        diff = no_index.output
    return DiffResult(file=file, diff=diff, binary=_is_binary_diff(diff))


# --- Branches -------------------------------------------------------------


def _parse_track(track: str) -> tuple[int, int]:
    """Parse ``ahead N, behind M`` (from ``upstream:track``) into counts."""
    ahead = behind = 0
    for part in track.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part[len("ahead ") :])
        elif part.startswith("behind "):
            behind = int(part[len("behind ") :])
    return ahead, behind


async def list_branches(path: str) -> BranchList:
    """Return the local and remote branches of the repository at ``path``.

    Args:
        path: Path to a local repository.

    Returns:
        A :class:`BranchList` with the current branch, local branches (with
        upstream + ahead/behind), and remote-tracking refs.
    """
    cwd = validate_repo_path(path)
    current = await _current_branch(cwd)

    local_fmt = _SEP.join(
        ["%(refname:short)", "%(HEAD)", "%(upstream:short)", "%(upstream:track,nobracket)"]
    )
    local_result = await run_git(["for-each-ref", f"--format={local_fmt}", "refs/heads"], cwd=cwd)
    local: list[Branch] = []
    for line in local_result.output.splitlines():
        if not line:
            continue
        name, head, upstream, track = (line.split(_SEP) + ["", "", "", ""])[:4]
        ahead, behind = _parse_track(track)
        local.append(
            Branch(
                name=name,
                is_current=head == "*",
                upstream=upstream or None,
                ahead=ahead,
                behind=behind,
            )
        )

    remote_result = await run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes"], cwd=cwd
    )
    remote = [
        BranchRef(name=name)
        for name in remote_result.output.splitlines()
        if name and not name.endswith("/HEAD")
    ]
    return BranchList(current=current, local=local, remote=remote)


async def create_branch(path: str, name: str, checkout: bool = False) -> CommandResult:
    """Create branch ``name``; when ``checkout`` is True, switch to it too."""
    cwd = validate_repo_path(path)
    args = ["checkout", "-b", name] if checkout else ["branch", name]
    return await run_git(args, cwd=cwd)


async def checkout_branch(path: str, name: str) -> CommandResult:
    """Switch the repository at ``path`` to branch ``name``."""
    cwd = validate_repo_path(path)
    return await run_git(["checkout", name], cwd=cwd)


async def delete_branch(path: str, name: str, force: bool = False) -> CommandResult:
    """Delete branch ``name``. Destructive; the UI confirms first.

    Args:
        path: Path to a local repository.
        name: The branch to delete.
        force: When True, force-delete unmerged branches (``-D``).
    """
    cwd = validate_repo_path(path)
    flag = "-D" if force else "-d"
    return await run_git(["branch", flag, name], cwd=cwd)


async def merge_branch(path: str, name: str) -> CommandResult:
    """Merge branch ``name`` into the current branch. The UI confirms first."""
    cwd = validate_repo_path(path)
    return await run_git(["merge", name], cwd=cwd)


# --- Commit graph ---------------------------------------------------------


def _parse_graph_line(line: str) -> GraphCommit | None:
    """Parse one delimited ``git log`` line into a :class:`GraphCommit`.

    The trailing (refs) field is empty for unreferenced commits; because
    ``CommandResult.output`` is whitespace-stripped and ``_SEP`` counts as
    whitespace, that empty field can vanish, so missing fields are padded.
    """
    fields = (line.split(_SEP) + [""] * 7)[:7]
    sha, short, parents, author, date, subject, refs = fields
    if not sha:
        return None
    ref_list = [ref.strip() for ref in refs.split(",") if ref.strip()]
    is_head = any(ref == "HEAD" or ref.startswith("HEAD -> ") for ref in ref_list)
    return GraphCommit(
        sha=sha,
        short=short,
        parents=parents.split() if parents else [],
        author=author,
        date=date,
        subject=subject,
        refs=ref_list,
        is_head=is_head,
    )


async def get_graph(path: str, limit: int = 200) -> list[GraphCommit]:
    """Return up to ``limit`` commits across all refs for graph drawing.

    Uses ``git log --all --date-order`` so the order suits client-side lane
    assignment. Each commit carries its parents and ref decorations.

    Args:
        path: Path to a local repository.
        limit: Maximum number of commits to return.

    Returns:
        A list of :class:`GraphCommit`, newest-first; empty for an unborn repo.
    """
    cwd = validate_repo_path(path)
    fmt = _SEP.join(["%H", "%h", "%P", "%an", "%aI", "%s", "%D"])
    result = await run_git(
        [
            "log",
            "--all",
            "--date-order",
            f"--max-count={limit}",
            f"--pretty=format:{fmt}",
        ],
        cwd=cwd,
    )
    if not result.ok:
        return []
    commits: list[GraphCommit] = []
    for line in result.output.splitlines():
        commit = _parse_graph_line(line)
        if commit is not None:
            commits.append(commit)
    return commits
