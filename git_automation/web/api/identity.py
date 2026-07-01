"""Identity / onboarding API endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from git_automation.core.git.client import set_global_identity
from git_automation.core.github.gh_cli import login_instructions, login_with_token
from git_automation.core.identity import get_identity_state
from git_automation.core.models import IdentityState

router = APIRouter()


class SetGitIdentityRequest(BaseModel):
    """Body for setting the global git identity."""

    name: str
    email: str


class TokenLoginRequest(BaseModel):
    """Body for token-based ``gh`` login."""

    token: str


@router.get("/identity")
async def read_identity() -> IdentityState:
    """Return the current combined git/``gh`` identity state."""
    return await get_identity_state()


@router.post("/identity/git")
async def set_git_identity(body: SetGitIdentityRequest) -> IdentityState:
    """Set the global git identity and return the updated identity state."""
    await set_global_identity(body.name, body.email)
    return await get_identity_state()


@router.get("/gh/login/instructions")
async def gh_login_instructions() -> dict:
    """Return human-readable ``gh`` authentication guidance."""
    return login_instructions()


@router.post("/gh/login/token")
async def gh_login_token(body: TokenLoginRequest) -> IdentityState:
    """Authenticate ``gh`` with a token and return the updated identity state."""
    await login_with_token(body.token)
    return await get_identity_state()
