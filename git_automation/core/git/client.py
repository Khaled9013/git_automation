"""Async wrapper around the local ``git`` CLI.

All commands run non-blocking via :mod:`git_automation.core.process`. Functions
that operate on a repository validate the path first.
"""

from __future__ import annotations

from pathlib import Path

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.models import (
    Branch,
    BranchList,
    BranchRef,
    Changes,
    CommandResult,
    CommitDetail,
    CommitFile,
    Conflict,
    DiffResult,
    FileChange,
    FileHunks,
    GraphCommit,
    Hunk,
    HunkLine,
    MergeResult,
    MergeStatus,
    ReflogEntry,
    RefsBundle,
    Remote,
    RepoStatus,
    Stash,
    Tag,
    UndoResult,
    Upstream,
    Worktree,
)
from git_automation.core.process import validate_repo_path

# Unit-separator delimiter for plumbing output; cannot appear in ref names.
_SEP = "\x1f"


def _reject_option(value: str, label: str) -> str:
    """Reject a value git could misread as an option (one starting with ``-``).

    Remote names, branch names, and other git refs can never legitimately begin
    with ``-`` (see ``git check-ref-format``). Rejecting such values closes an
    option-injection vector: a crafted ``remote`` like ``--upload-pack=<cmd>``
    (or ``--receive-pack`` on push) would otherwise be parsed as a flag by
    ``git fetch``/``pull``/``push`` and lead to arbitrary command execution.
    ``--`` is not a usable separator for every affected command (e.g. ``git
    checkout -- main`` means a *pathspec*, not a branch), so this guard is the
    uniform defense.

    Args:
        value: The caller-supplied remote/branch/ref value.
        label: Human-readable name of the field, used in the error message.

    Returns:
        ``value`` unchanged when it is safe.

    Raises:
        GitAutomationError: ``invalid_argument`` when ``value`` starts with ``-``.
    """
    if value.startswith("-"):
        raise GitAutomationError(
            "invalid_argument",
            f"Invalid {label}: must not start with '-'.",
            400,
        )
    return value


def _resolve_worktree_file(cwd: str, file: str) -> Path:
    """Resolve a caller-supplied repo-relative path, refusing any escape.

    The merge editor reads and writes the *working-tree* copy of a conflicted
    file (:func:`get_conflict` / :func:`resolve_conflict`). Because ``file``
    arrives from the client, it must be confined to the validated repository:
    an absolute path, a ``../`` traversal, or an intermediate symlink pointing
    outside would otherwise let a caller read or overwrite arbitrary files
    (e.g. ``/etc/...``) or — by targeting ``.git/config`` / a ``.git`` hook —
    reach code execution. The path is resolved with symlinks followed and is
    required to stay strictly within the worktree and outside ``.git``.

    Args:
        cwd: The validated repository working-tree directory.
        file: The caller-supplied, repo-relative file path.

    Returns:
        The resolved absolute :class:`~pathlib.Path` of the target file.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) when the path escapes the
            worktree, equals the worktree root, or enters the ``.git`` directory.
    """
    root = Path(cwd).resolve()
    candidate = (root / file).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise GitAutomationError(
            "invalid_argument",
            "Invalid file path: must be inside the repository working tree.",
            400,
        )
    if ".git" in candidate.relative_to(root).parts:
        raise GitAutomationError(
            "invalid_argument",
            "Invalid file path: must not be inside the .git directory.",
            400,
        )
    return candidate


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


def _parse_status_v2(output: str) -> tuple[str | None, Upstream | None, int, int, bool]:
    """Parse ``git status --porcelain=v2 --branch`` into the status fields.

    A single ``git status --porcelain=v2 --branch`` invocation reports everything
    :func:`get_status` needs, replacing the former four-process fan-out
    (``branch --show-current`` + ``rev-parse @{upstream}`` +
    ``rev-list --left-right`` + ``status --porcelain``). Header lines start with
    ``#``; each remaining non-empty line is one changed entry.

    Header lines of interest (others such as ``# branch.oid`` are ignored):

    * ``# branch.head <name>`` -- the current branch, or the literal
      ``(detached)`` for a detached HEAD. We map ``(detached)`` to ``None`` to
      match the old ``branch --show-current`` behavior (empty -> ``None``). An
      unborn branch still reports its configured name (e.g. ``main``), exactly as
      ``--show-current`` did.
    * ``# branch.upstream <remote>/<branch>`` -- present only when an upstream is
      configured; absent otherwise (``upstream`` stays ``None``).
    * ``# branch.ab +<ahead> -<behind>`` -- present only alongside an upstream.
      When absent, ``ahead``/``behind`` stay ``0`` (matching the old code, which
      only computed them ``if upstream``).

    Any non-header line (``1``/``2`` tracked changes, ``?`` untracked, ``u``
    unmerged) means the tree is dirty. ``!`` ignored entries are not emitted by
    default, so their absence needs no special handling.

    Returns:
        ``(current_branch, upstream, ahead, behind, dirty)``.
    """
    current_branch: str | None = None
    upstream: Upstream | None = None
    ahead = behind = 0
    dirty = False
    for line in output.splitlines():
        if not line:
            continue
        if not line.startswith("# "):
            # Any porcelain-v2 change entry (1/2/u/?) marks the tree dirty; this
            # mirrors the old ``status --porcelain`` non-empty check, under which
            # untracked files also counted as dirty.
            dirty = True
            continue
        if line.startswith("# branch.head "):
            name = line[len("# branch.head ") :].strip()
            current_branch = None if name == "(detached)" else (name or None)
        elif line.startswith("# branch.upstream "):
            full = line[len("# branch.upstream ") :].strip()
            if "/" in full:
                remote, branch = full.split("/", 1)
                upstream = Upstream(remote=remote, branch=branch)
        elif line.startswith("# branch.ab "):
            parts = line[len("# branch.ab ") :].split()
            # Format is exactly ``+<ahead> -<behind>``.
            if len(parts) == 2 and parts[0].startswith("+") and parts[1].startswith("-"):
                try:
                    ahead, behind = int(parts[0][1:]), int(parts[1][1:])
                except ValueError:
                    ahead = behind = 0
    # ahead/behind only make sense with an upstream; drop stray counts otherwise
    # so the result matches the old ``if upstream`` gate byte-for-byte.
    if upstream is None:
        ahead = behind = 0
    return current_branch, upstream, ahead, behind, dirty


async def get_status(path: str) -> RepoStatus:
    """Return a :class:`RepoStatus` snapshot for the repository at ``path``.

    Args:
        path: Path to a local repository (validated as an existing directory).
    """
    cwd = validate_repo_path(path)
    # One porcelain-v2 invocation yields branch, upstream, ahead/behind, and the
    # dirty flag together (see :func:`_parse_status_v2`). ``list_remotes`` stays
    # a separate call -- it reads ``git remote -v``, which v2 status does not
    # cover. Use run_process for raw stdout: v2 header lines are stable and
    # newline-delimited, and this avoids CommandResult.output's strip().
    status_result = await process.run_process(
        ["git", "status", "--porcelain=v2", "--branch"], cwd=cwd
    )
    current_branch, upstream, ahead, behind, dirty = _parse_status_v2(status_result.stdout)
    remotes = await list_remotes(cwd)
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
    _reject_option(remote, "remote")
    return await run_git(["fetch", remote], cwd=cwd)


async def pull(path: str, remote: str, branch: str | None = None) -> CommandResult:
    """Pull ``remote`` (optionally a specific ``branch``) into ``path``."""
    cwd = validate_repo_path(path)
    _reject_option(remote, "remote")
    args = ["pull", remote]
    if branch:
        _reject_option(branch, "branch")
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
    _reject_option(remote, "remote")
    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    args.append(remote)
    if branch:
        _reject_option(branch, "branch")
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

    Notes:
        Porcelain v1 is leading-column significant: a line is ``XY<space>path``
        where ``X`` is the index (staged) status and ``Y`` the worktree
        (unstaged) status. The raw, *unstripped* stdout is parsed here -- never
        :attr:`CommandResult.output`, which is ``.strip()``-ped. Stripping would
        drop the leading space of an unstaged-only change (e.g. `` M f.txt`` ->
        ``M f.txt``), misreading it as a staged ``M`` with a mangled path.
    """
    cwd = validate_repo_path(path)
    result = await process.run_process(["git", "status", "--porcelain"], cwd=cwd)
    staged: list[FileChange] = []
    unstaged: list[FileChange] = []
    untracked: list[str] = []
    for line in result.stdout.splitlines():
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
    """Stage ``files`` in the repository at ``path`` (``git add``).

    Each caller-supplied path is confined to the worktree (outside ``.git``) via
    :func:`_resolve_worktree_file` before it reaches git, so a traversal
    (``../``) or absolute path is rejected at the app layer rather than relying
    on git's own pathspec handling.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if any path escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    for file in files:
        _resolve_worktree_file(cwd, file)
    return await run_git(["add", "--", *files], cwd=cwd)


async def unstage(path: str, files: list[str]) -> CommandResult:
    """Unstage ``files`` in the repository at ``path`` (``git restore --staged``).

    Each path is confined to the worktree (outside ``.git``) via
    :func:`_resolve_worktree_file` before reaching git.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if any path escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    for file in files:
        _resolve_worktree_file(cwd, file)
    return await run_git(["restore", "--staged", "--", *files], cwd=cwd)


async def discard(path: str, files: list[str]) -> CommandResult:
    """Discard working-tree changes to ``files`` (``git restore``).

    Destructive: overwrites the working-tree copy with the index version. The
    UI confirms before calling this. Each path is confined to the worktree
    (outside ``.git``) via :func:`_resolve_worktree_file` before reaching git.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if any path escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    for file in files:
        _resolve_worktree_file(cwd, file)
    return await run_git(["restore", "--", *files], cwd=cwd)


async def delete_files(path: str, files: list[str]) -> CommandResult:
    """Delete ``files`` from the working tree of the repository at ``path``.

    Tracked files are removed with ``git rm -f`` (which also stages the
    deletion); untracked files are unlinked from disk. Mixed lists are handled
    in a single call. Every path is confined to the worktree (outside ``.git``)
    via :func:`_resolve_worktree_file`, so traversal/absolute paths are rejected.

    Destructive: the working-tree copy is removed. The UI confirms first.

    Args:
        path: Path to a local repository.
        files: Repo-relative paths to delete (tracked and/or untracked).

    Returns:
        A :class:`CommandResult` whose ``output`` combines git's output for the
        tracked deletions; ``ok`` is False if the ``git rm`` step failed.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if any path escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    resolved = {file: _resolve_worktree_file(cwd, file) for file in files}
    tracked: list[str] = []
    untracked: list[str] = []
    for file in files:
        check = await run_git(["ls-files", "--error-unmatch", "--", file], cwd=cwd)
        (tracked if check.ok else untracked).append(file)
    outputs: list[str] = []
    ok = True
    if tracked:
        removed = await run_git(["rm", "-f", "--", *tracked], cwd=cwd)
        ok = removed.ok
        if removed.output:
            outputs.append(removed.output)
    for file in untracked:
        target = resolved[file]
        if target.is_file() or target.is_symlink():
            target.unlink()
    return CommandResult(ok=ok, output="\n".join(outputs))


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
        # An empty tracked diff means either an untracked file or a tracked file
        # with no changes. Only render full content (as an added-file diff) when
        # the file is genuinely untracked; a clean tracked file has no diff.
        tracked = await run_git(["ls-files", "--error-unmatch", "--", file], cwd=cwd)
        if not tracked.ok:
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
    _reject_option(name, "branch name")
    args = ["checkout", "-b", name] if checkout else ["branch", name]
    return await run_git(args, cwd=cwd)


async def checkout_branch(path: str, name: str) -> CommandResult:
    """Switch the repository at ``path`` to branch ``name``."""
    cwd = validate_repo_path(path)
    _reject_option(name, "branch name")
    return await run_git(["checkout", name], cwd=cwd)


async def delete_branch(path: str, name: str, force: bool = False) -> CommandResult:
    """Delete branch ``name``. Destructive; the UI confirms first.

    Args:
        path: Path to a local repository.
        name: The branch to delete.
        force: When True, force-delete unmerged branches (``-D``).
    """
    cwd = validate_repo_path(path)
    _reject_option(name, "branch name")
    flag = "-D" if force else "-d"
    return await run_git(["branch", flag, "--", name], cwd=cwd)


async def _conflicting_paths(cwd: str) -> list[str]:
    """Return the repo-relative paths with unmerged (conflict) entries."""
    result = await run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    return [line for line in result.output.splitlines() if line]


async def merge_branch(path: str, name: str) -> MergeResult:
    """Merge branch ``name`` into the current branch, reporting conflicts.

    Args:
        path: Path to a local repository.
        name: The branch (or any commit-ish) to merge in.

    Returns:
        A :class:`MergeResult`. On a conflicting merge, ``ok`` is ``False``,
        ``conflicted`` is ``True``, and ``conflicts`` lists the unmerged paths.
        The UI confirms before calling this.
    """
    cwd = validate_repo_path(path)
    _reject_option(name, "branch name")
    result = await run_git(["merge", name], cwd=cwd)
    conflicts = await _conflicting_paths(cwd)
    return MergeResult(
        ok=result.ok,
        output=result.output,
        conflicted=bool(conflicts),
        conflicts=conflicts,
    )


# --- Commit graph ---------------------------------------------------------


def _parse_graph_line(line: str) -> GraphCommit | None:
    """Parse one delimited ``git log`` line into a :class:`GraphCommit`.

    The subject (``%s``) is the only free-text field, so it is placed last and
    captured with a bounded split (``maxsplit=6``): an embedded ``_SEP`` in the
    subject then stays within the subject instead of shifting later fields.

    The trailing field can be empty (e.g. an empty subject); because
    ``CommandResult.output`` is whitespace-stripped and ``_SEP`` counts as
    whitespace, that empty field can vanish, so missing fields are padded.
    """
    fields = (line.split(_SEP, 6) + [""] * 7)[:7]
    sha, short, parents, author, date, refs, subject = fields
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
    fmt = _SEP.join(["%H", "%h", "%P", "%an", "%aI", "%D", "%s"])
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


# --- Refs sidebar ---------------------------------------------------------


async def _list_tags(cwd: str) -> list[Tag]:
    """Return the repository's tags (``git tag``)."""
    result = await run_git(["tag"], cwd=cwd)
    return [Tag(name=name) for name in result.output.splitlines() if name]


async def _list_worktrees(cwd: str) -> list[Worktree]:
    """Return the repository's working trees (``git worktree list --porcelain``)."""
    result = await run_git(["worktree", "list", "--porcelain"], cwd=cwd)
    here = Path(cwd).resolve()
    worktrees: list[Worktree] = []
    wt_path: str | None = None
    branch: str | None = None

    def flush() -> None:
        if wt_path is not None:
            worktrees.append(
                Worktree(
                    path=wt_path,
                    branch=branch,
                    is_current=Path(wt_path).resolve() == here,
                )
            )

    for line in result.output.splitlines():
        if line.startswith("worktree "):
            flush()
            wt_path = line[len("worktree ") :]
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    flush()
    return worktrees


async def _list_stashes(cwd: str) -> list[Stash]:
    """Return the stash entries (``git stash list``)."""
    result = await run_git(["stash", "list", "--format=%gd%x1f%gs"], cwd=cwd)
    stashes: list[Stash] = []
    for line in result.output.splitlines():
        if _SEP not in line:
            continue
        selector, message = line.split(_SEP, 1)
        raw = selector[len("stash@{") : -1] if selector.startswith("stash@{") else ""
        try:
            stashes.append(Stash(index=int(raw), message=message))
        except ValueError:
            continue
    return stashes


async def get_refs(path: str) -> RefsBundle:
    """Return every ref powering the sidebar for the repository at ``path``.

    Aggregates local branches (with upstream + ahead/behind), remote-tracking
    refs, tags, working trees, and stashes.

    Args:
        path: Path to a local repository.
    """
    cwd = validate_repo_path(path)
    branches = await list_branches(cwd)
    return RefsBundle(
        local=branches.local,
        remote=branches.remote,
        tags=await _list_tags(cwd),
        worktrees=await _list_worktrees(cwd),
        stashes=await _list_stashes(cwd),
    )


# --- Commit detail --------------------------------------------------------


def _parse_numstat(output: str) -> dict[str, tuple[int, int]]:
    """Parse ``git show --numstat`` lines into ``{path: (additions, deletions)}``.

    Binary files report ``-`` for both counts; those are recorded as ``(0, 0)``.
    """
    counts: dict[str, tuple[int, int]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_raw, del_raw, file = parts[0], parts[1], parts[-1]
        additions = int(add_raw) if add_raw.isdigit() else 0
        deletions = int(del_raw) if del_raw.isdigit() else 0
        counts[file] = (additions, deletions)
    return counts


async def get_commit_detail(path: str, sha: str) -> CommitDetail:
    """Return full metadata and the changed-file list for commit ``sha``.

    Args:
        path: Path to a local repository.
        sha: The commit to describe (branch name, tag, or SHA).

    Returns:
        A :class:`CommitDetail` with parents, author/email/date, subject, body,
        ref decorations, and per-file additions/deletions.

    Raises:
        GitAutomationError: ``invalid_argument`` if ``sha`` starts with ``-``;
            ``unknown_commit`` (404) if the commit cannot be resolved.
    """
    cwd = validate_repo_path(path)
    _reject_option(sha, "commit")
    meta_fmt = _SEP.join(["%H", "%h", "%P", "%an", "%ae", "%aI", "%D", "%s"])
    meta = await run_git(["show", "-s", f"--format={meta_fmt}", sha], cwd=cwd)
    if not meta.ok or _SEP not in meta.output:
        raise GitAutomationError("unknown_commit", f"Unknown commit: {sha}", 404)
    fields = (meta.output.split(_SEP) + [""] * 8)[:8]
    full, short, parents, author, email, date, refs, subject = fields
    body_result = await run_git(["show", "-s", "--format=%b", sha], cwd=cwd)
    # ``--first-parent`` makes ``git show`` emit an ordinary (non-combined) diff
    # for merge commits. Without it, ``--name-status`` yields *nothing* for a
    # merge (git suppresses the combined diff), so a merge commit's changed-file
    # list would always come back empty. For non-merge and root commits the flag
    # is a no-op. It is applied to both queries so their file sets stay aligned.
    numstat = await run_git(["show", "--first-parent", "--numstat", "--format=", sha], cwd=cwd)
    name_status = await run_git(
        ["show", "--first-parent", "--name-status", "--format=", sha], cwd=cwd
    )
    counts = _parse_numstat(numstat.output)
    files: list[CommitFile] = []
    for line in name_status.output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]
        file = parts[-1]
        additions, deletions = counts.get(file, (0, 0))
        files.append(CommitFile(path=file, status=status, additions=additions, deletions=deletions))
    ref_list = [ref.strip() for ref in refs.split(",") if ref.strip()]
    return CommitDetail(
        sha=full,
        short=short,
        parents=parents.split() if parents else [],
        author=author,
        email=email,
        date=date,
        subject=subject,
        body=body_result.output,
        refs=ref_list,
        files=files,
    )


# --- Navigation / history rewrite -----------------------------------------


async def checkout_ref(path: str, ref: str) -> CommandResult:
    """Check out ``ref`` -- a branch name (attached) or a commit SHA (detached).

    Args:
        path: Path to a local repository.
        ref: A branch name or commit-ish; a non-branch checks out detached HEAD.
    """
    cwd = validate_repo_path(path)
    _reject_option(ref, "ref")
    return await run_git(["checkout", ref], cwd=cwd)


async def reset(path: str, sha: str, mode: str) -> CommandResult:
    """Move ``HEAD`` to ``sha`` with the given reset ``mode``. Destructive.

    Args:
        path: Path to a local repository.
        sha: The commit-ish to reset onto.
        mode: One of ``soft``, ``mixed``, or ``hard``.

    Raises:
        GitAutomationError: ``invalid_argument`` if ``mode`` is unrecognized or
            ``sha`` starts with ``-``.
    """
    cwd = validate_repo_path(path)
    if mode not in {"soft", "mixed", "hard"}:
        raise GitAutomationError(
            "invalid_argument",
            "Invalid reset mode: must be one of soft, mixed, hard.",
            400,
        )
    _reject_option(sha, "commit")
    return await run_git(["reset", f"--{mode}", sha], cwd=cwd)


async def cherry_pick(path: str, sha: str) -> CommandResult:
    """Apply commit ``sha`` onto the current branch (``git cherry-pick``)."""
    cwd = validate_repo_path(path)
    _reject_option(sha, "commit")
    return await run_git(["cherry-pick", sha], cwd=cwd)


# --- Merge lifecycle + conflicts ------------------------------------------


async def _git_path(cwd: str, name: str) -> Path:
    """Resolve a path inside the git dir (handles linked worktrees)."""
    result = await run_git(["rev-parse", "--git-path", name], cwd=cwd)
    candidate = Path(result.output.strip())
    return candidate if candidate.is_absolute() else Path(cwd) / candidate


async def merge_status(path: str) -> MergeStatus:
    """Return the in-progress merge state of the repository at ``path``.

    Args:
        path: Path to a local repository.

    Returns:
        A :class:`MergeStatus`: whether a merge is underway, the unmerged paths,
        and the prepared ``MERGE_MSG`` text (empty when not merging).
    """
    cwd = validate_repo_path(path)
    head = await run_git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=cwd)
    merging = head.ok
    conflicts = await _conflicting_paths(cwd)
    message = ""
    if merging:
        merge_msg = await _git_path(cwd, "MERGE_MSG")
        if merge_msg.is_file():
            message = merge_msg.read_text(errors="replace")
    return MergeStatus(merging=merging, conflicts=conflicts, message=message)


async def _show_stage(cwd: str, stage: int, file: str) -> str | None:
    """Return the blob content of a merge ``stage`` for ``file``, or ``None``.

    Uses :func:`process.run_process` directly so the blob's exact bytes (including
    any trailing newline) are preserved rather than whitespace-stripped. A missing
    stage (e.g. a file added or deleted on only one side) yields ``None``.
    """
    result = await process.run_process(["git", "show", f":{stage}:{file}"], cwd=cwd)
    return result.stdout if result.ok else None


async def get_conflict(path: str, file: str) -> Conflict:
    """Return the three merge stages plus the working copy of ``file``.

    Args:
        path: Path to a local repository.
        file: The repo-relative conflicted path.

    Returns:
        A :class:`Conflict` with ``base`` (stage 1), ``ours`` (stage 2),
        ``theirs`` (stage 3), and ``merged`` (the working file with markers).
        A missing stage is ``None`` and does not crash. ``binary`` is set when
        any retrieved content contains a NUL byte.
    """
    cwd = validate_repo_path(path)
    working = _resolve_worktree_file(cwd, file)
    base = await _show_stage(cwd, 1, file)
    ours = await _show_stage(cwd, 2, file)
    theirs = await _show_stage(cwd, 3, file)
    merged = working.read_text(errors="replace") if working.is_file() else None
    binary = any("\x00" in text for text in (base, ours, theirs, merged) if text is not None)
    return Conflict(
        file=file,
        base=base,
        ours=ours,
        theirs=theirs,
        merged=merged,
        binary=binary,
    )


async def resolve_conflict(path: str, file: str, content: str) -> CommandResult:
    """Write ``content`` to the working ``file`` and stage it (``git add``).

    Args:
        path: Path to a local repository.
        file: The repo-relative path being resolved.
        content: The resolved file contents to write.
    """
    cwd = validate_repo_path(path)
    target = _resolve_worktree_file(cwd, file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return await run_git(["add", "--", file], cwd=cwd)


async def merge_continue(path: str, message: str | None = None) -> CommandResult:
    """Finalize an in-progress merge by committing it.

    Args:
        path: Path to a local repository.
        message: Optional commit message; defaults to the prepared ``MERGE_MSG``.

    Raises:
        GitAutomationError: ``unresolved_conflicts`` (409) if conflicts remain.
    """
    cwd = validate_repo_path(path)
    if await _conflicting_paths(cwd):
        raise GitAutomationError(
            "unresolved_conflicts",
            "Cannot continue the merge while conflicts remain unresolved.",
            409,
        )
    args = ["commit", "-m", message] if message else ["commit", "--no-edit"]
    return await run_git(args, cwd=cwd)


async def merge_abort(path: str) -> CommandResult:
    """Abort an in-progress merge and restore the pre-merge state."""
    cwd = validate_repo_path(path)
    return await run_git(["merge", "--abort"], cwd=cwd)


# --- Stash ----------------------------------------------------------------


async def stash(
    path: str,
    message: str | None = None,
    include_untracked: bool = False,
    files: list[str] | None = None,
) -> CommandResult:
    """Save the working-tree changes onto the stash stack.

    Args:
        path: Path to a local repository.
        message: Optional stash label.
        include_untracked: When True, also stash untracked files (``-u``).
        files: When given, stash only these repo-relative paths
            (``git stash push -- <files>``), leaving other changes in the
            working tree. Each path is confined via :func:`_resolve_worktree_file`.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if any path in ``files``
            escapes the worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    args = ["stash", "push"]
    if include_untracked:
        args.append("--include-untracked")
    if message:
        args += ["-m", message]
    if files:
        for file in files:
            _resolve_worktree_file(cwd, file)
        args += ["--", *files]
    return await run_git(args, cwd=cwd)


def _stash_ref(index: int) -> str:
    """Return the ``stash@{N}`` selector for a stash ``index``."""
    return f"stash@{{{index}}}"


async def stash_pop(path: str, index: int | None = None) -> CommandResult:
    """Apply and remove a stash entry (default: the most recent)."""
    cwd = validate_repo_path(path)
    args = ["stash", "pop"]
    if index is not None:
        args.append(_stash_ref(index))
    return await run_git(args, cwd=cwd)


async def stash_apply(path: str, index: int) -> CommandResult:
    """Apply stash ``index`` without removing it from the stack."""
    cwd = validate_repo_path(path)
    return await run_git(["stash", "apply", _stash_ref(index)], cwd=cwd)


async def stash_drop(path: str, index: int) -> CommandResult:
    """Remove stash ``index`` from the stack. Destructive; the UI confirms."""
    cwd = validate_repo_path(path)
    return await run_git(["stash", "drop", _stash_ref(index)], cwd=cwd)


# --- Undo / Redo (reflog-based) -------------------------------------------


async def get_reflog(path: str, limit: int = 50) -> list[ReflogEntry]:
    """Return up to ``limit`` ``HEAD`` reflog entries (newest first).

    Args:
        path: Path to a local repository.
        limit: Maximum number of entries to return.
    """
    cwd = validate_repo_path(path)
    result = await run_git(["reflog", "--format=%gd%x1f%gs", f"-n{limit}"], cwd=cwd)
    entries: list[ReflogEntry] = []
    for line in result.output.splitlines():
        if _SEP not in line:
            continue
        selector, subject = line.split(_SEP, 1)
        entries.append(ReflogEntry(selector=selector, subject=subject))
    return entries


async def _reflog_subject(cwd: str, selector: str) -> str:
    """Return the reflog subject for ``selector`` (e.g. ``HEAD@{0}``)."""
    result = await run_git(["reflog", "--format=%gs", "-n1", selector], cwd=cwd)
    lines = result.output.splitlines()
    return lines[0] if lines else ""


async def _reflog_step(cwd: str) -> UndoResult:
    """Move ``HEAD`` one reflog step with ``git reset --keep HEAD@{1}``.

    ``--keep`` refuses (non-zero exit) when the step would discard uncommitted
    work, so that safety is enforced by git itself.
    """
    undone = await _reflog_subject(cwd, "HEAD@{0}")
    result = await run_git(["reset", "--keep", "HEAD@{1}"], cwd=cwd)
    return UndoResult(ok=result.ok, output=result.output, undone=undone)


async def undo(path: str) -> UndoResult:
    """Move ``HEAD`` back one reflog step (best-effort undo).

    Uses ``git reset --keep HEAD@{1}``; this refuses if it would discard
    uncommitted work and cannot reverse pushes or other destructive operations.
    """
    cwd = validate_repo_path(path)
    return await _reflog_step(cwd)


async def redo(path: str) -> UndoResult:
    """Move ``HEAD`` forward one reflog step (best-effort redo).

    After an undo, the prior position is recorded at ``HEAD@{1}``, so a redo is
    the same reflog step. Best-effort: it cannot reverse pushes or destructive
    operations.
    """
    cwd = validate_repo_path(path)
    return await _reflog_step(cwd)


# --- Slice 4: hunk staging ------------------------------------------------


def _hunk_header(line: str) -> str:
    """Return the canonical ``@@ -a,b +c,d @@`` portion of a hunk header line.

    A hunk header may carry a trailing section heading (e.g.
    ``@@ -1,3 +1,4 @@ def foo():``); only the ``@@ ... @@`` range marker is kept.
    """
    parts = line.split("@@")
    if len(parts) >= 3:
        return f"@@{parts[1]}@@"
    return line


def _parse_file_hunks(diff: str) -> list[Hunk]:
    """Split a single-file unified diff into independently applyable hunks.

    The lines preceding the first ``@@`` (``diff --git``, ``index``, ``---``,
    ``+++``, any mode lines) form the shared file header. Each hunk's ``patch``
    is that header plus the one ``@@`` block, terminated by a single newline so
    it applies cleanly with ``git apply``.

    Args:
        diff: The raw (unstripped) ``git diff`` output for one file.

    Returns:
        One :class:`Hunk` per ``@@`` block, in file order.
    """
    lines = diff.split("\n")
    header: list[str] = []
    idx = 0
    while idx < len(lines) and not lines[idx].startswith("@@"):
        header.append(lines[idx])
        idx += 1

    hunks: list[Hunk] = []
    while idx < len(lines):
        if not lines[idx].startswith("@@"):
            idx += 1
            continue
        head_line = lines[idx]
        body = [head_line]
        idx += 1
        while idx < len(lines) and not lines[idx].startswith("@@"):
            if lines[idx].startswith("diff --git"):
                break
            body.append(lines[idx])
            idx += 1
        hunk_lines = [
            HunkLine(type=line[0], text=line[1:])
            for line in body[1:]
            if line and line[0] in (" ", "+", "-")
        ]
        patch = "\n".join(header + body).rstrip("\n") + "\n"
        hunks.append(Hunk(header=_hunk_header(head_line), patch=patch, lines=hunk_lines))
    return hunks


async def get_hunks(path: str, file: str, staged: bool = False) -> FileHunks:
    """Return the per-hunk breakdown of ``file``'s diff in the repo at ``path``.

    Args:
        path: Path to a local repository.
        file: Repository-relative path of the file to diff.
        staged: When True, diff the staged version (``--cached``).

    Returns:
        A :class:`FileHunks`. ``binary`` files report no hunks. Each hunk's
        ``patch`` can be fed straight to :func:`stage_hunk`/:func:`unstage_hunk`.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if ``file`` escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    _resolve_worktree_file(cwd, file)
    args = ["diff"]
    if staged:
        args.append("--cached")
    args += ["--", file]
    # Use run_process for the *raw* stdout: CommandResult.output is stripped,
    # which would corrupt patch round-tripping (leading context, trailing
    # newline). git apply needs the exact bytes.
    result = await process.run_process(["git", *args], cwd=cwd)
    diff = result.stdout
    binary = _is_binary_diff(diff)
    hunks = [] if binary else _parse_file_hunks(diff)
    return FileHunks(file=file, binary=binary, hunks=hunks)


async def stage_hunk(path: str, file: str, patch: str) -> CommandResult:
    """Stage a single hunk by applying ``patch`` to the index.

    Pipes ``patch`` to ``git apply --cached`` so only the lines in that one hunk
    are staged; other unstaged changes to ``file`` are left untouched.

    Args:
        path: Path to a local repository.
        file: Repository-relative path the patch targets (validated, not passed
            to git -- the patch itself names the file).
        patch: A self-contained hunk patch (from :func:`get_hunks`).

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if ``file`` escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    _resolve_worktree_file(cwd, file)
    result = await process.run_process(["git", "apply", "--cached"], cwd=cwd, stdin=patch)
    return CommandResult(ok=result.ok, output=result.combined)


async def unstage_hunk(path: str, file: str, patch: str) -> CommandResult:
    """Unstage a single hunk by reverse-applying ``patch`` against the index.

    Pipes ``patch`` to ``git apply --cached -R`` so only that one hunk is
    removed from the index; other staged changes to ``file`` are preserved.

    Args:
        path: Path to a local repository.
        file: Repository-relative path the patch targets (validated only).
        patch: A self-contained hunk patch (from :func:`get_hunks` with
            ``staged=True``).

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if ``file`` escapes the
            worktree or enters ``.git``.
    """
    cwd = validate_repo_path(path)
    _resolve_worktree_file(cwd, file)
    result = await process.run_process(
        ["git", "apply", "--cached", "-R"], cwd=cwd, stdin=patch
    )
    return CommandResult(ok=result.ok, output=result.combined)


# --- Slice 4: historical commit-file diff ---------------------------------


async def get_commit_diff(path: str, sha: str, file: str) -> DiffResult:
    """Return the diff a commit introduced to a single ``file``.

    Uses ``git show <sha> --first-parent -- <file>`` so the diff resolves for
    merge commits (the first-parent diff) as well as ordinary commits.

    Args:
        path: Path to a local repository.
        sha: The commit to inspect (branch name, tag, or SHA).
        file: Repository-relative path of the file to diff.

    Returns:
        A :class:`DiffResult` with binary detection reused from :func:`get_diff`.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if ``sha`` starts with
            ``-`` or ``file`` escapes the worktree / enters ``.git``.
    """
    cwd = validate_repo_path(path)
    _reject_option(sha, "commit")
    _resolve_worktree_file(cwd, file)
    result = await run_git(["show", sha, "--first-parent", "--", file], cwd=cwd)
    diff = result.output
    return DiffResult(file=file, diff=diff, binary=_is_binary_diff(diff))


# --- Slice 4: branch rename / track / amend -------------------------------


async def rename_branch(path: str, name: str, new_name: str) -> CommandResult:
    """Rename branch ``name`` to ``new_name`` (``git branch -m``).

    Args:
        path: Path to a local repository.
        name: The existing branch name.
        new_name: The new branch name.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if either name starts
            with ``-``.
    """
    cwd = validate_repo_path(path)
    _reject_option(name, "branch name")
    _reject_option(new_name, "branch name")
    return await run_git(["branch", "-m", name, new_name], cwd=cwd)


async def track_branch(path: str, remote_ref: str) -> CommandResult:
    """Create and check out a local branch tracking ``remote_ref``.

    Runs ``git switch -c <leaf> --track <remote_ref>`` where ``leaf`` is the
    portion of ``remote_ref`` after its first ``/`` (the remote prefix), e.g.
    ``origin/feature`` -> local branch ``feature``.

    Args:
        path: Path to a local repository.
        remote_ref: A remote-tracking ref such as ``origin/feature``.

    Raises:
        GitAutomationError: ``invalid_argument`` (400) if ``remote_ref`` starts
            with ``-``.
    """
    cwd = validate_repo_path(path)
    _reject_option(remote_ref, "remote ref")
    leaf = remote_ref.split("/", 1)[1] if "/" in remote_ref else remote_ref
    return await run_git(["switch", "-c", leaf, "--track", remote_ref], cwd=cwd)


async def amend_commit(path: str, message: str | None = None) -> CommandResult:
    """Amend the most recent commit.

    Replaces the subject/body with ``message`` (``git commit --amend -m``) when
    one is given; otherwise keeps the existing message (``--amend --no-edit``).
    Either way the currently staged changes are folded into ``HEAD``.

    Args:
        path: Path to a local repository.
        message: Optional replacement commit message; when omitted (or blank)
            the existing message is preserved.
    """
    cwd = validate_repo_path(path)
    if message and message.strip():
        return await run_git(["commit", "--amend", "-m", message], cwd=cwd)
    return await run_git(["commit", "--amend", "--no-edit"], cwd=cwd)
