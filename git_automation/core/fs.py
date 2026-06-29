"""Read-only filesystem browsing for the in-app repository picker.

Lists *directories only*. It never reads or executes file contents -- the only
way file contents reach the client is through ``git diff``. All paths are
treated as untrusted input and validated before use.
"""

from __future__ import annotations

import os
from pathlib import Path

from git_automation.core.errors import GitAutomationError
from git_automation.core.models import FsEntry


def home_dir() -> str:
    """Return the absolute path to the current user's home directory."""
    return str(Path.home())


def _is_git_repo(directory: Path) -> bool:
    """Return True when ``directory`` looks like a git repository root."""
    try:
        return (directory / ".git").exists()
    except OSError:
        return False


async def list_dirs(path: str) -> list[FsEntry]:
    """List the sub-directories of ``path`` (directories only), sorted.

    Args:
        path: An absolute directory path supplied by the client.

    Returns:
        One :class:`FsEntry` per sub-directory, sorted case-insensitively by
        name, each flagged with whether it is a git repository.

    Raises:
        GitAutomationError: ``invalid_path`` if the path is empty, missing, or
            not a directory; ``permission_denied`` if it cannot be read.
    """
    if not path:
        raise GitAutomationError("invalid_path", "A directory path is required.", 400)
    expanded = os.path.expanduser(path)
    root = Path(expanded)
    if not root.is_dir():
        raise GitAutomationError(
            "invalid_path",
            f"Path does not exist or is not a directory: {path}",
            400,
        )
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except PermissionError as exc:
        raise GitAutomationError(
            "permission_denied",
            f"Permission denied: {path}",
            403,
        ) from exc
    children.sort(key=lambda child: child.name.lower())
    return [
        FsEntry(
            name=child.name,
            path=str(child),
            is_dir=True,
            is_git_repo=_is_git_repo(child),
        )
        for child in children
    ]
