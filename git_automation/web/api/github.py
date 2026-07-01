"""GitHub cockpit API endpoints (notifications + issues, backed by ``gh``).

Registered by ``web/api/__init__.py`` under the shared ``/api`` prefix, so the
routes resolve to ``/api/github/*``. Thin HTTP adapter over
:mod:`git_automation.core.github.client`; the same module is the seam future
(agentic) consumers call directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core.github import client
from git_automation.core.github.models import Comment, IssueDetail, IssueSummary, Notification

router = APIRouter()


class CommentRequest(BaseModel):
    """Body for posting an issue comment."""

    repo: str
    number: int
    body: str


@router.get("/github/notifications")
async def github_notifications(
    all: bool = Query(False), participating: bool = Query(False)
) -> list[Notification]:
    """List the authenticated user's notification threads."""
    return await client.list_notifications(all=all, participating=participating)


@router.post("/github/notifications/{thread_id}/read")
async def github_mark_notification_read(thread_id: str) -> dict:
    """Mark a notification thread as read."""
    await client.mark_notification_read(thread_id)
    return {"ok": True}


@router.post("/github/notifications/read-all")
async def github_mark_all_notifications_read() -> dict:
    """Mark every notification thread as read."""
    await client.mark_all_notifications_read()
    return {"ok": True}


@router.get("/github/issues")
async def github_issues(
    repo: str | None = Query(None),
    filter: str = Query("assigned"),
    state: str = Query("open"),
) -> list[IssueSummary]:
    """List issues by filter (``assigned``/``mentioned``/``created``/``all``)."""
    return await client.list_issues(repo=repo, filter=filter, state=state)


@router.get("/github/issue")
async def github_issue(repo: str = Query(...), number: int = Query(...)) -> IssueDetail:
    """Fetch one issue's detail (body + comment thread)."""
    return await client.get_issue(repo, number)


@router.post("/github/issue/comment")
async def github_issue_comment(body: CommentRequest) -> Comment:
    """Post a comment on an issue and return it."""
    return await client.add_comment(body.repo, body.number, body.body)


@router.get("/github/me")
async def github_me() -> dict:
    """Return the authenticated user's login."""
    return {"login": await client.current_login()}
