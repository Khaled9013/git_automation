"""Async wrapper around the local ``git`` CLI.

All commands run non-blocking via :mod:`git_automation.core.process`. Functions
that operate on a repository validate the path first.
"""

from __future__ import annotations

from git_automation.core import process
from git_automation.core.models import CommandResult, Remote, RepoStatus, Upstream
from git_automation.core.process import validate_repo_path


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
