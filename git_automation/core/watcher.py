"""Repository file-watcher built on :func:`watchfiles.awatch`.

What this emits -- and what it does NOT
---------------------------------------
:func:`watch_repo` is a pure *change-notification* source. It yields the
repo-relative paths that changed; it **never reads or transmits file
contents**. Consumers (e.g. the ``/api/watch`` WebSocket) use the notifications
only as a hint to re-query git/status -- they must not treat the path list as
trusted data and must not echo file bodies through it.

Filtering policy
----------------
The whole working tree is watched recursively. Git's internal bookkeeping under
``.git/`` is noisy (loose objects, lock files, logs, ...) and would cause a
refresh storm, so it is filtered out -- *except* the three things that signal a
user-visible repository state change:

* ``.git/index``      -- staging changed,
* ``.git/HEAD``       -- the checked-out ref moved (checkout/detach),
* ``.git/refs/**``    -- branches/tags created, moved, or deleted.

So commits, checkouts, and branch ops still notify the UI while raw object
churn does not.

Cancellation
------------
:func:`watch_repo` is a plain async generator. Stop it by cancelling the task
that drives it, or by calling ``aclose()`` on the generator -- either way
``awatch`` tears down its background watcher thread in its own ``finally``. No
watch task or OS watcher is left running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from watchfiles import awatch

from git_automation.core.process import validate_repo_path


def _relevant_relpath(root: Path, changed: str) -> str | None:
    """Return ``changed`` relative to ``root`` if it should notify, else None.

    Working-tree paths always notify. Paths under ``.git/`` are dropped unless
    they are ``.git/index``, ``.git/HEAD``, or live under ``.git/refs/``.

    Args:
        root: The resolved repository root.
        changed: An absolute path reported by the watcher.

    Returns:
        The POSIX repo-relative path string, or ``None`` to ignore the event.
    """
    try:
        rel = Path(changed).resolve().relative_to(root)
    except (ValueError, OSError):
        # Outside the repo root, or unresolvable -- ignore defensively.
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] != ".git":
        # Anything in the working tree is a real, user-visible change.
        return rel.as_posix()
    # Inside .git/: only a curated allow-list signals a state change.
    if len(parts) == 2 and parts[1] in {"index", "HEAD"}:
        return rel.as_posix()
    if len(parts) >= 2 and parts[1] == "refs":
        return rel.as_posix()
    return None


async def watch_repo(path: str) -> AsyncIterator[list[str]]:
    """Yield batches of changed repo-relative paths for the repo at ``path``.

    The path is validated immediately (on first iteration). Each yielded value
    is a sorted, de-duplicated list of repo-relative POSIX paths that changed in
    one ``awatch`` debounce window; batches that contain only filtered ``.git/``
    noise are skipped entirely (nothing is yielded for them). No file contents
    are ever read.

    Args:
        path: Filesystem path to the repository working tree.

    Yields:
        Non-empty lists of repo-relative paths that changed.

    Raises:
        GitAutomationError: If ``path`` is not an existing directory.
    """
    root = Path(validate_repo_path(path)).resolve()

    def _keep(_change: object, changed_path: str) -> bool:
        return _relevant_relpath(root, changed_path) is not None

    # awatch's default filter ignores ``.git``; we supply our own so that the
    # curated ``.git`` allow-list above is honoured. awatch debounces/batches
    # raw OS events, so no extra debounce is needed here.
    async for changes in awatch(str(root), watch_filter=_keep, recursive=True):
        rels = sorted(
            {
                rel
                for _change, changed_path in changes
                if (rel := _relevant_relpath(root, changed_path)) is not None
            }
        )
        if rels:
            yield rels
