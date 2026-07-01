"""Combined git + ``gh`` identity state for the onboarding flow."""

from __future__ import annotations

import asyncio

from git_automation.core.git.client import get_global_identity
from git_automation.core.github.gh_cli import auth_status
from git_automation.core.models import IdentityState


async def get_identity_state() -> IdentityState:
    """Return the combined git identity and ``gh`` auth state.

    ``needs_onboarding`` is True when the git name or email is unset, or when
    ``gh`` is not authenticated.
    """
    (git_name, git_email), (gh_authenticated, gh_user) = await asyncio.gather(
        get_global_identity(),
        auth_status(),
    )
    needs_onboarding = not git_name or not git_email or not gh_authenticated
    return IdentityState(
        git_name=git_name,
        git_email=git_email,
        gh_authenticated=gh_authenticated,
        gh_user=gh_user,
        needs_onboarding=needs_onboarding,
    )
