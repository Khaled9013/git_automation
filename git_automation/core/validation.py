"""Shared argument-guards for values handed to external CLIs.

This module is the single home for validation that must be identical across the
git client (:mod:`git_automation.core.git.client`) and the ``gh`` CLI wrapper
(:mod:`git_automation.core.github.gh_cli`). Keeping one copy avoids the two
guards drifting apart and closes the option-injection vector uniformly.
"""

from __future__ import annotations

from git_automation.core.errors import GitAutomationError


def reject_option(value: str, label: str) -> str:
    """Reject a value git/``gh`` could misread as an option (starting with ``-``).

    Remote names, branch names, and other git refs can never legitimately begin
    with ``-`` (see ``git check-ref-format``). Rejecting such values closes an
    option-injection vector: a crafted ``remote`` like ``--upload-pack=<cmd>``
    (or ``--receive-pack`` on push) would otherwise be parsed as a flag by
    ``git fetch``/``pull``/``push`` (or by ``gh pr create`` flags) and lead to
    arbitrary command execution. ``--`` is not a usable separator for every
    affected command (e.g. ``git checkout -- main`` means a *pathspec*, not a
    branch), so this guard is the uniform defense.

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
