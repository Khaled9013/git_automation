"""Centralized, non-blocking subprocess execution.

Every external command (``git`` and ``gh``) flows through :func:`run_process`.
It uses ``asyncio.create_subprocess_exec`` with an argument *list* only -- never
a shell -- so untrusted values can never be interpreted by a shell. Tests
monkeypatch this single function to avoid touching the real ``git``/``gh`` CLIs.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from git_automation.core.errors import GitAutomationError


@dataclass(frozen=True)
class ProcessResult:
    """The outcome of a single subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True when the process exited successfully."""
        return self.returncode == 0

    @property
    def combined(self) -> str:
        """Combined stdout/stderr, trimmed of surrounding whitespace."""
        out = self.stdout
        if self.stderr:
            if out and not out.endswith("\n"):
                out += "\n"
            out += self.stderr
        return out.strip()


async def run_process(
    args: list[str],
    cwd: str | None = None,
    stdin: str | None = None,
) -> ProcessResult:
    """Run ``args`` as a subprocess without a shell and capture its output.

    Args:
        args: The command and its arguments as a list (e.g. ``["git", "status"]``).
        cwd: Working directory to run the command in, if any.
        stdin: Optional text written to the process's stdin. Used to pass secrets
            (such as a ``gh`` token) without ever exposing them in ``args``.

    Returns:
        A :class:`ProcessResult` with the return code and decoded output.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    input_bytes = stdin.encode() if stdin is not None else None
    stdout_bytes, stderr_bytes = await proc.communicate(input_bytes)
    return ProcessResult(
        returncode=proc.returncode if proc.returncode is not None else 0,
        stdout=stdout_bytes.decode(errors="replace"),
        stderr=stderr_bytes.decode(errors="replace"),
    )


def validate_repo_path(path: str) -> str:
    """Validate that ``path`` is an existing directory before running git in it.

    Args:
        path: A filesystem path supplied by the caller/client.

    Returns:
        The validated path unchanged.

    Raises:
        GitAutomationError: If the path is empty, missing, or not a directory.
    """
    if not path:
        raise GitAutomationError("invalid_path", "A repository path is required.", 400)
    expanded = os.path.expanduser(path)
    if not Path(expanded).is_dir():
        raise GitAutomationError(
            "invalid_path",
            f"Path does not exist or is not a directory: {path}",
            400,
        )
    return expanded
