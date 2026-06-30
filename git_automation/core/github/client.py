"""Async wrapper around the GitHub CLI (``gh``) for notifications and issues.

Every external call flows through :func:`git_automation.core.process.run_process`
with an argument *list* (never a shell); bodies are passed on stdin and never
shell-interpolated. ``gh`` failures and unparseable output surface as
:class:`GitAutomationError`. Tests monkeypatch ``run_process`` so neither ``gh``
nor the network is ever touched.
"""

from __future__ import annotations

import json
import re

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.github.models import (
    Comment,
    IssueDetail,
    IssueSummary,
    Notification,
)

# A ``owner/name`` slug. Each segment is GitHub-safe and, by excluding a leading
# dash on the owner, cannot be misread by ``gh`` as a command-line option.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")

# The trailing integer of a ``subject.url`` such as
# ``https://api.github.com/repos/o/r/issues/123`` -> ``123``.
_SUBJECT_NUMBER_RE = re.compile(r"/(\d+)$")

# Filter -> the ``@me`` flag for ``gh search issues`` / ``gh issue list``.
# ``gh search`` uses ``--mentions`` while ``gh issue list`` uses ``--mention``.
_SEARCH_FILTER_FLAGS = {
    "assigned": "--assignee",
    "mentioned": "--mentions",
    "created": "--author",
}
_LIST_FILTER_FLAGS = {
    "assigned": "--assignee",
    "mentioned": "--mention",
    "created": "--author",
}
_VALID_FILTERS = {"assigned", "mentioned", "created", "all"}

_SEARCH_JSON = "number,title,state,repository,author,assignees,labels,commentsCount,updatedAt,url"
_LIST_JSON = "number,title,state,author,assignees,labels,comments,updatedAt,url"
_ISSUE_JSON = "number,title,state,author,body,assignees,labels,comments,url"


def _validate_repo(repo: str) -> str:
    """Validate that ``repo`` is a safe ``owner/name`` slug.

    Raises:
        GitAutomationError: ``invalid_argument`` if the value is empty or does
            not look like ``owner/name`` (also closing the option-injection
            vector of a value beginning with ``-``).
    """
    if not repo or not _REPO_RE.match(repo):
        raise GitAutomationError(
            "invalid_argument",
            "Invalid repository: expected 'owner/name'.",
            400,
        )
    return repo


def _validate_number(number: int) -> int:
    """Validate that ``number`` is a positive integer (rejecting ``bool``)."""
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise GitAutomationError(
            "invalid_argument",
            "Invalid issue number: expected a positive integer.",
            400,
        )
    return number


def _validate_thread_id(thread_id: str) -> str:
    """Validate a notification ``thread_id`` (non-empty, not option-like)."""
    if not thread_id or thread_id.startswith("-"):
        raise GitAutomationError(
            "invalid_argument",
            "Invalid notification thread id.",
            400,
        )
    return thread_id


def _parse_json(result: process.ProcessResult, code: str) -> object:
    """Parse ``result.stdout`` as JSON, raising ``code`` on failure."""
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GitAutomationError(code, "Could not parse the JSON output of gh.", 400) from exc


def _subject_number(url: str | None) -> int | None:
    """Derive an issue/PR number from a notification ``subject.url``."""
    if not url:
        return None
    match = _SUBJECT_NUMBER_RE.search(url)
    return int(match.group(1)) if match else None


def _logins(items: object) -> list[str]:
    """Map a list of ``{"login": ...}`` objects to their logins."""
    if not isinstance(items, list):
        return []
    return [i["login"] for i in items if isinstance(i, dict) and i.get("login")]


def _label_names(items: object) -> list[str]:
    """Map a list of ``{"name": ...}`` label objects to their names."""
    if not isinstance(items, list):
        return []
    return [i["name"] for i in items if isinstance(i, dict) and i.get("name")]


def _comment_count(item: dict) -> int:
    """Derive a comment count from either ``commentsCount`` or ``comments``.

    ``gh search issues`` exposes ``commentsCount`` (an int); ``gh issue list``
    exposes ``comments`` which may be an int count or a list of comment objects.
    """
    if "commentsCount" in item:
        return int(item["commentsCount"])
    comments = item.get("comments")
    if isinstance(comments, int):
        return comments
    if isinstance(comments, list):
        return len(comments)
    return 0


def _to_issue_summary(item: dict, repo_override: str | None = None) -> IssueSummary:
    """Map one ``gh`` JSON issue object to :class:`IssueSummary`."""
    repository = item.get("repository") or {}
    repo = repo_override or repository.get("nameWithOwner") or repository.get("name") or ""
    author = (item.get("author") or {}).get("login", "")
    return IssueSummary(
        repo=repo,
        number=item["number"],
        title=item.get("title", ""),
        state=item.get("state", ""),
        author=author,
        assignees=_logins(item.get("assignees")),
        labels=_label_names(item.get("labels")),
        comments=_comment_count(item),
        updated_at=item.get("updatedAt", ""),
        url=item.get("url", ""),
    )


def _to_comment(item: dict) -> Comment:
    """Map one ``gh issue view`` comment object to :class:`Comment`."""
    return Comment(
        author=(item.get("author") or {}).get("login", ""),
        body=item.get("body", ""),
        created_at=item.get("createdAt", ""),
    )


async def list_notifications(
    *, all: bool = False, participating: bool = False
) -> list[Notification]:
    """List the authenticated user's notification threads via ``gh api``.

    Args:
        all: Include read notifications (``?all=true``), not just unread.
        participating: Restrict to threads the user participates in
            (``?participating=true``).

    Returns:
        The notification threads mapped to :class:`Notification`.

    Raises:
        GitAutomationError: ``github_notifications_failed`` on gh failure or
            unparseable output.
    """
    path = "/notifications"
    params = []
    if all:
        params.append("all=true")
    if participating:
        params.append("participating=true")
    if params:
        path += "?" + "&".join(params)

    result = await process.run_process(["gh", "api", path])
    if not result.ok:
        raise GitAutomationError(
            "github_notifications_failed",
            result.combined or "gh api /notifications failed.",
            400,
        )
    data = _parse_json(result, "github_notifications_failed") or []
    notifications: list[Notification] = []
    for item in data:
        subject = item.get("subject") or {}
        repository = item.get("repository") or {}
        url = subject.get("url")
        notifications.append(
            Notification(
                id=str(item.get("id", "")),
                reason=item.get("reason", ""),
                unread=bool(item.get("unread", False)),
                title=subject.get("title", ""),
                subject_type=subject.get("type", ""),
                repo=repository.get("full_name", ""),
                number=_subject_number(url),
                url=url,
                updated_at=item.get("updated_at", ""),
            )
        )
    return notifications


async def mark_notification_read(thread_id: str) -> None:
    """Mark a notification thread read via ``gh api -X PATCH``.

    Raises:
        GitAutomationError: ``invalid_argument`` for a bad ``thread_id`` or
            ``github_mark_read_failed`` on gh failure.
    """
    _validate_thread_id(thread_id)
    result = await process.run_process(
        ["gh", "api", "-X", "PATCH", f"/notifications/threads/{thread_id}"]
    )
    if not result.ok:
        raise GitAutomationError(
            "github_mark_read_failed",
            result.combined or "gh api PATCH notification thread failed.",
            400,
        )


async def list_issues(
    *, repo: str | None = None, filter: str = "assigned", state: str = "open"
) -> list[IssueSummary]:
    """List issues, either across GitHub by filter or within a single repo.

    With ``repo`` set, uses ``gh issue list --repo <repo>`` (the ``filter``, when
    not ``all``, is applied as a ``--assignee/--mention/--author @me`` flag).
    Without ``repo``, uses ``gh search issues`` with the filter's ``@me`` flag;
    ``filter="all"`` is only valid alongside a ``repo``.

    Raises:
        GitAutomationError: ``invalid_argument`` for an unknown ``filter`` /
            ``repo``, or ``github_issues_failed`` on gh failure.
    """
    if filter not in _VALID_FILTERS:
        raise GitAutomationError(
            "invalid_argument",
            f"Invalid filter: expected one of {sorted(_VALID_FILTERS)}.",
            400,
        )

    if repo is not None:
        _validate_repo(repo)
        args = ["gh", "issue", "list", "--repo", repo, "--state", state]
        flag = _LIST_FILTER_FLAGS.get(filter)
        if flag:
            args += [flag, "@me"]
        args += ["--json", _LIST_JSON]
        repo_override: str | None = repo
        json_code = "github_issues_failed"
    else:
        if filter == "all":
            raise GitAutomationError(
                "invalid_argument",
                "filter 'all' requires a repo.",
                400,
            )
        args = ["gh", "search", "issues", _SEARCH_FILTER_FLAGS[filter], "@me", "--state", state]
        args += ["--json", _SEARCH_JSON]
        repo_override = None
        json_code = "github_issues_failed"

    result = await process.run_process(args)
    if not result.ok:
        raise GitAutomationError(
            "github_issues_failed",
            result.combined or "gh issue listing failed.",
            400,
        )
    data = _parse_json(result, json_code) or []
    return [_to_issue_summary(item, repo_override) for item in data]


async def get_issue(repo: str, number: int) -> IssueDetail:
    """Fetch one issue (body + comments) via ``gh issue view``.

    Raises:
        GitAutomationError: ``invalid_argument`` for a bad ``repo``/``number`` or
            ``github_issue_failed`` on gh failure.
    """
    _validate_repo(repo)
    _validate_number(number)
    result = await process.run_process(
        ["gh", "issue", "view", str(number), "--repo", repo, "--json", _ISSUE_JSON]
    )
    if not result.ok:
        raise GitAutomationError(
            "github_issue_failed",
            result.combined or "gh issue view failed.",
            400,
        )
    item = _parse_json(result, "github_issue_failed") or {}
    comments = item.get("comments") or []
    return IssueDetail(
        repo=repo,
        number=item.get("number", number),
        title=item.get("title", ""),
        state=item.get("state", ""),
        author=(item.get("author") or {}).get("login", ""),
        body=item.get("body", ""),
        assignees=_logins(item.get("assignees")),
        labels=_label_names(item.get("labels")),
        comments=[_to_comment(c) for c in comments],
        url=item.get("url", ""),
    )


async def add_comment(repo: str, number: int, body: str) -> Comment:
    """Post a comment via ``gh issue comment ... --body-file -`` (body on stdin).

    The body is written to ``gh``'s stdin and never appears in the command
    arguments. The returned :class:`Comment` is attributed to the authenticated
    user (looked up via :func:`current_login`).

    Raises:
        GitAutomationError: ``invalid_argument`` for a bad ``repo``/``number`` or
            ``github_comment_failed`` on gh failure.
    """
    _validate_repo(repo)
    _validate_number(number)
    result = await process.run_process(
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
        stdin=body,
    )
    if not result.ok:
        raise GitAutomationError(
            "github_comment_failed",
            result.combined or "gh issue comment failed.",
            400,
        )
    return Comment(author=await current_login(), body=body, created_at="")


async def current_login() -> str:
    """Return the authenticated user's login via ``gh api /user``.

    Raises:
        GitAutomationError: ``github_user_failed`` on gh failure or unparseable
            output.
    """
    result = await process.run_process(["gh", "api", "/user"])
    if not result.ok:
        raise GitAutomationError(
            "github_user_failed",
            result.combined or "gh api /user failed.",
            400,
        )
    data = _parse_json(result, "github_user_failed") or {}
    login = data.get("login") if isinstance(data, dict) else None
    if not login:
        raise GitAutomationError(
            "github_user_failed",
            "Could not determine the authenticated user.",
            400,
        )
    return login
