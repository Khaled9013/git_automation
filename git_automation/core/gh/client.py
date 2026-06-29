"""Async wrapper around the GitHub CLI (``gh``) for auth/onboarding."""

from __future__ import annotations

import re

from git_automation.core import process
from git_automation.core.errors import GitAutomationError

# Matches both modern ("... account NAME") and legacy ("... as NAME") phrasing
# of `gh auth status` output.
_USER_RE = re.compile(r"Logged in to \S+ (?:account|as) (\S+)")


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
