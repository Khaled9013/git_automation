"""Typed domain models for the Phase 1 slice (identity + remote-aware git ops)."""

from __future__ import annotations

from pydantic import BaseModel


class Remote(BaseModel):
    """A configured git remote."""

    name: str
    url: str


class Upstream(BaseModel):
    """The upstream tracking ref of a branch."""

    remote: str
    branch: str


class RepoStatus(BaseModel):
    """A snapshot of a repository's branch/remote/sync state."""

    path: str
    current_branch: str | None
    upstream: Upstream | None
    remotes: list[Remote]
    ahead: int
    behind: int
    dirty: bool


class IdentityState(BaseModel):
    """Combined git identity + ``gh`` auth state used for onboarding."""

    git_name: str | None
    git_email: str | None
    gh_authenticated: bool
    gh_user: str | None
    needs_onboarding: bool


class CommandResult(BaseModel):
    """The result of a git command surfaced to the API."""

    ok: bool
    output: str


__all__ = [
    "Remote",
    "Upstream",
    "RepoStatus",
    "IdentityState",
    "CommandResult",
]
