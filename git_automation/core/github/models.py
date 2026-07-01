"""Typed models for the GitHub cockpit module (notifications + issues).

These mirror the contract in
``docs/superpowers/specs/2026-06-30-github-cockpit-contract.md`` exactly and are
the shapes returned by :mod:`git_automation.core.github.client` and serialized by
the ``/api/github/*`` endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel


class Notification(BaseModel):
    """A single GitHub notification thread (mention/assign/review/comment/...)."""

    id: str
    reason: str
    unread: bool
    title: str
    subject_type: str
    repo: str
    number: int | None
    url: str | None
    updated_at: str


class IssueSummary(BaseModel):
    """A row in an issues list (search results or a repo's issue list)."""

    repo: str
    number: int
    title: str
    state: str
    author: str
    assignees: list[str]
    labels: list[str]
    comments: int
    updated_at: str
    url: str


class Comment(BaseModel):
    """A single comment on an issue."""

    id: int | None = None
    author: str
    body: str
    created_at: str


class IssueDetail(BaseModel):
    """A fully expanded issue: body plus its comment thread."""

    repo: str
    number: int
    title: str
    state: str
    author: str
    body: str
    assignees: list[str]
    labels: list[str]
    comments: list[Comment]
    url: str
