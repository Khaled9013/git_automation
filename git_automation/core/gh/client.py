"""Async wrapper around the GitHub CLI (``gh``) for auth/onboarding and PRs."""

from __future__ import annotations

import json
import re

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.process import validate_repo_path

# Matches both modern ("... account NAME") and legacy ("... as NAME") phrasing
# of `gh auth status` output.
_USER_RE = re.compile(r"Logged in to \S+ (?:account|as) (\S+)")

# Matches the pull-request URL `gh pr create` prints on success, capturing the
# full URL and the trailing PR number (e.g. ".../pull/42").
_PR_URL_RE = re.compile(r"(https?://\S+/pull/(\d+))")


def _parse_user(output: str) -> str | None:
    """Extract the authenticated username from ``gh auth status`` output."""
    match = _USER_RE.search(output)
    return match.group(1) if match else None


async def auth_status() -> tuple[bool, str | None]:
    """Return ``(authenticated, user)`` for the current ``gh`` session.

    Returns:
        ``(True, username)`` when a session exists (username may be ``None`` if
        it could not be parsed), otherwise ``(False, None)``.
    """
    result = await process.run_process(["gh", "auth", "status"])
    if not result.ok:
        return False, None
    return True, _parse_user(result.combined)


async def login_with_token(token: str) -> None:
    """Authenticate ``gh`` using ``gh auth login --with-token``.

    The token is written to the process's stdin and never appears in command
    arguments, logs, or error messages.

    Raises:
        GitAutomationError: If the login command fails. The error message
            contains only ``gh``'s own output, never the token.
    """
    result = await process.run_process(
        ["gh", "auth", "login", "--with-token"],
        stdin=token,
    )
    if not result.ok:
        raise GitAutomationError(
            "gh_login_failed",
            result.combined or "gh auth login failed.",
            400,
        )


def _reject_option(value: str, label: str) -> str:
    """Reject a value ``gh`` could misread as an option (one starting with ``-``).

    Branch refs can never legitimately begin with ``-`` (see
    ``git check-ref-format``); rejecting such values closes an option-injection
    vector against ``gh pr create`` flags. Mirrors the git client's guard.
    """
    if value.startswith("-"):
        raise GitAutomationError(
            "invalid_argument",
            f"Invalid {label}: must not start with '-'.",
            400,
        )
    return value


async def pr_create(
    path: str,
    title: str,
    body: str | None = None,
    base: str | None = None,
    head: str | None = None,
) -> dict:
    """Open a pull request via ``gh pr create`` in the repository at ``path``.

    The flags ``--title``, ``--body``, ``--base`` and ``--head`` are added only
    when their value is provided. ``base``/``head`` are guarded against
    option-injection.

    Args:
        path: Path to a local repository (validated as an existing directory).
        title: The PR title (required).
        body: Optional PR description.
        base: Optional base branch to merge into.
        head: Optional head branch containing the changes.

    Returns:
        ``{"number": int, "url": str}`` parsed from the URL ``gh`` prints.

    Raises:
        GitAutomationError: ``gh_pr_create_failed`` if ``gh`` fails or its output
            cannot be parsed into a PR URL.
    """
    cwd = validate_repo_path(path)
    args = ["gh", "pr", "create", "--title", title]
    if body is not None:
        args += ["--body", body]
    if base is not None:
        _reject_option(base, "base branch")
        args += ["--base", base]
    if head is not None:
        _reject_option(head, "head branch")
        args += ["--head", head]
    result = await process.run_process(args, cwd=cwd)
    if not result.ok:
        raise GitAutomationError(
            "gh_pr_create_failed",
            result.combined or "gh pr create failed.",
            400,
        )
    match = _PR_URL_RE.search(result.combined)
    if not match:
        raise GitAutomationError(
            "gh_pr_create_failed",
            result.combined or "Could not parse the PR URL from gh output.",
            400,
        )
    return {"number": int(match.group(2)), "url": match.group(1)}


async def pr_list(path: str) -> list:
    """List open pull requests via ``gh pr list`` for the repository at ``path``.

    Args:
        path: Path to a local repository (validated as an existing directory).

    Returns:
        A list of ``PullRequest`` models mapped from ``gh``'s JSON output.

    Raises:
        GitAutomationError: ``gh_pr_list_failed`` if ``gh`` fails or returns
            output that is not valid JSON.
    """
    # Imported lazily so this module stays importable before the models agent
    # adds ``PullRequest`` to ``git_automation.core.models``.
    from git_automation.core.models import PullRequest

    cwd = validate_repo_path(path)
    result = await process.run_process(
        [
            "gh",
            "pr",
            "list",
            "--json",
            "number,title,url,state,headRefName,baseRefName",
        ],
        cwd=cwd,
    )
    if not result.ok:
        raise GitAutomationError(
            "gh_pr_list_failed",
            result.combined or "gh pr list failed.",
            400,
        )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitAutomationError(
            "gh_pr_list_failed",
            "Could not parse the JSON output of gh pr list.",
            400,
        ) from exc
    return [
        PullRequest(
            number=item["number"],
            title=item["title"],
            url=item["url"],
            state=item["state"],
            head=item.get("headRefName", ""),
            base=item.get("baseRefName", ""),
        )
        for item in data
    ]


def login_instructions() -> dict:
    """Return human-readable guidance for authenticating ``gh`` on a fresh machine."""
    return {
        "steps": [
            "Install the GitHub CLI (gh) from https://cli.github.com if it is "
            "not already available.",
            "Create a Personal Access Token at "
            "https://github.com/settings/tokens with at least the 'repo' and "
            "'read:org' scopes.",
            "Paste the token into the field below and submit; it is sent to "
            "`gh auth login --with-token` and never stored by this app.",
            "Alternatively, run `gh auth login` in a terminal for an "
            "interactive browser-based login.",
        ],
        "supports_token": True,
    }
