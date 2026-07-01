"""GitHub pull-request API endpoints (backed by the ``gh`` CLI).

Registered by ``web/api/__init__.py`` under the shared ``/api`` prefix, so the
routes resolve to ``POST /api/gh/pr/create`` and ``GET /api/gh/pr/list``.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core.github.gh_cli import pr_create, pr_list
from git_automation.core.github.models import PullRequest

router = APIRouter()


class PrCreateRequest(BaseModel):
    """Body for opening a pull request via ``gh pr create``."""

    path: str
    title: str
    body: str | None = None
    base: str | None = None
    head: str | None = None


@router.post("/gh/pr/create")
async def gh_pr_create(body: PrCreateRequest) -> dict:
    """Open a pull request and return ``{number, url}``."""
    return await pr_create(body.path, body.title, body.body, body.base, body.head)


@router.get("/gh/pr/list")
async def gh_pr_list(path: str = Query(...)) -> list[PullRequest]:
    """Return the open pull requests for the repository at ``path``."""
    return await pr_list(path)
