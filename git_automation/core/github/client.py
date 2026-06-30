"""Direct GitHub REST client (via ``httpx``) for notifications and issues.

This module talks to ``https://api.github.com`` directly through a shared,
module-level :class:`httpx.AsyncClient` (keep-alive connection pooling, a fixed
``base_url`` and ``Accept`` header). It replaces the previous ``gh``-subprocess
implementation while keeping **every public function signature and pydantic
model output identical**, so the web UI and future agentic consumers see no
change -- only the internals (and the latency) differ.

Authentication: the GitHub token is obtained once via ``gh auth token`` and
cached at module level. On a ``401`` the token is refreshed once and the request
retried. If ``gh auth token`` cannot produce a token, a
``GitAutomationError("github_not_authenticated", ...)`` is raised. The token is
never logged, never placed in an error message, and never interpolated into a
path or query.

Security guards (see ``docs/SECURITY-REVIEW.md`` G1-G6) are preserved verbatim:
repo ``owner/name`` regex, positive-int issue number (rejecting ``bool``),
``filter``/``state`` allow-lists, and a digits-only notification ``thread_id``
(closing the path-injection vector). GitHub error responses (401/403 rate limit/
404) map to clean :class:`GitAutomationError` instances with no token, body, or
traceback leakage.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.github.models import (
    Comment,
    IssueDetail,
    IssueSummary,
    Notification,
)

# --- HTTP layer --------------------------------------------------------------

_API_BASE = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_DEFAULT_POLL_INTERVAL = 60

# Shared, lazily-built async client (keep-alive pooling) and the cached token.
# Both are module-level so connections and the token survive across calls; tests
# replace ``_client``/``_get_token`` to mock the HTTP + auth layers entirely.
_client: httpx.AsyncClient | None = None
_token: str | None = None

# Conditional-request (ETag) cache for ``/notifications`` plus the advertised
# poll interval, keyed by the request's query string so distinct
# ``all``/``participating`` views never serve each other's cached body.
_etag_cache: dict[str, tuple[str, list[Notification]]] = {}
_poll_interval: int = _DEFAULT_POLL_INTERVAL


# --- validation (security guards, preserved) ---------------------------------

# A ``owner/name`` slug. Each segment is GitHub-safe and, by excluding a leading
# dash on the owner, cannot be misread as a command-line option (G5).
_REPO_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")

# The trailing integer of a ``subject.url`` such as
# ``https://api.github.com/repos/o/r/issues/123`` -> ``123``.
_SUBJECT_NUMBER_RE = re.compile(r"/(\d+)$")

# A GitHub notification thread id is an integer string. Digits-only also keeps
# the value from altering the ``/notifications/threads/{id}`` path it is
# interpolated into (no ``/``, ``?``, ``..`` or option-like prefixes) (G1).
_THREAD_ID_RE = re.compile(r"^[0-9]+$")

_VALID_FILTERS = {"assigned", "mentioned", "created", "all"}
_VALID_STATES = {"open", "closed"}

# Filter -> the GitHub search qualifier used with ``@me``.
_SEARCH_QUALIFIER = {"assigned": "assignee", "mentioned": "mentions", "created": "author"}


def _validate_repo(repo: str) -> str:
    """Validate that ``repo`` is a safe ``owner/name`` slug."""
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
    """Validate a notification ``thread_id`` (digits only) (G1)."""
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        raise GitAutomationError(
            "invalid_argument",
            "Invalid notification thread id: expected a numeric id.",
            400,
        )
    return thread_id


def _validate_state(state: str) -> str:
    """Validate an issue ``state`` against the ``{open, closed}`` allow-list (G2)."""
    if state not in _VALID_STATES:
        raise GitAutomationError(
            "invalid_argument",
            f"Invalid state: expected one of {sorted(_VALID_STATES)}.",
            400,
        )
    return state


# --- token + shared client ---------------------------------------------------


async def _get_client() -> httpx.AsyncClient:
    """Return the shared keep-alive client, building it on first use."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_API_BASE,
            headers={"Accept": _ACCEPT, "X-GitHub-Api-Version": _API_VERSION},
            timeout=httpx.Timeout(15.0),
        )
    return _client


async def _get_token(*, force: bool = False) -> str:
    """Return the cached GitHub token, fetching it via ``gh auth token`` if needed.

    Args:
        force: Re-run ``gh auth token`` even if a token is cached (used once on a
            ``401`` to recover from an expired/rotated token).

    Raises:
        GitAutomationError: ``github_not_authenticated`` when ``gh auth token``
            fails or yields nothing. The error never contains the token.
    """
    global _token
    if _token is not None and not force:
        return _token
    result = await process.run_process(["gh", "auth", "token"])
    candidate = result.stdout.strip() if result.ok else ""
    if not candidate:
        raise GitAutomationError(
            "github_not_authenticated",
            "GitHub CLI is not authenticated; run 'gh auth login'.",
            400,
        )
    _token = candidate
    return _token


def _error_message(resp: httpx.Response) -> str:
    """Build a clean, token-free error message from a GitHub error response.

    Includes the HTTP status and GitHub's own ``message`` field (e.g. "Bad
    credentials", "API rate limit exceeded", "Not Found") -- never the request
    headers/token or a traceback (G6).
    """
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = str(data.get("message") or "")
    except (ValueError, httpx.HTTPError):
        detail = ""
    detail = detail or resp.reason_phrase or "request failed"
    return f"GitHub API error {resp.status_code}: {detail}"


async def _request(
    method: str,
    path: str,
    *,
    error_code: str,
    params: dict[str, str] | None = None,
    json_body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
    allow_304: bool = False,
) -> httpx.Response:
    """Issue one authenticated request, refreshing the token once on a ``401``.

    The ``Authorization: Bearer <token>`` header is attached per request so a
    token refresh on ``401`` takes effect immediately without rebuilding the
    pooled connection. Non-2xx responses (other than an allowed ``304``) raise a
    clean :class:`GitAutomationError` carrying ``error_code``.
    """
    client = await _get_client()
    token = await _get_token()
    headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
    try:
        resp = await client.request(method, path, params=params, json=json_body, headers=headers)
        if resp.status_code == 401:
            token = await _get_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            resp = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
    except httpx.HTTPError as exc:
        raise GitAutomationError(error_code, "Could not reach the GitHub API.", 400) from exc
    if allow_304 and resp.status_code == 304:
        return resp
    if resp.status_code >= 400:
        raise GitAutomationError(error_code, _error_message(resp), 400)
    return resp


# --- JSON -> model mapping ---------------------------------------------------


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


def _issue_repo(item: dict) -> str:
    """Derive ``owner/name`` from a REST issue object's repository fields.

    Cross-repo ``/issues`` responses carry ``repository.full_name``; ``/search``
    items instead carry a ``repository_url`` (``.../repos/owner/name``).
    """
    repository = item.get("repository")
    if isinstance(repository, dict) and repository.get("full_name"):
        return repository["full_name"]
    repo_url = item.get("repository_url")
    if isinstance(repo_url, str) and "/repos/" in repo_url:
        return repo_url.split("/repos/", 1)[1]
    return ""


def _to_issue_summary(item: dict, repo_override: str | None = None) -> IssueSummary:
    """Map one REST issue object to :class:`IssueSummary`."""
    return IssueSummary(
        repo=repo_override or _issue_repo(item),
        number=item["number"],
        title=item.get("title", ""),
        state=item.get("state", ""),
        author=(item.get("user") or {}).get("login", ""),
        assignees=_logins(item.get("assignees")),
        labels=_label_names(item.get("labels")),
        comments=int(item.get("comments") or 0),
        updated_at=item.get("updated_at", ""),
        url=item.get("html_url", ""),
    )


def _to_comment(item: dict) -> Comment:
    """Map one REST issue-comment object to :class:`Comment`."""
    return Comment(
        author=(item.get("user") or {}).get("login", ""),
        body=item.get("body", ""),
        created_at=item.get("created_at", ""),
    )


def _to_notification(item: dict) -> Notification:
    """Map one REST notification thread to :class:`Notification`."""
    subject = item.get("subject") or {}
    repository = item.get("repository") or {}
    url = subject.get("url")
    return Notification(
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


# --- public interface (unchanged signatures + model outputs) -----------------


def notifications_poll_interval() -> int:
    """Return GitHub's last-advertised ``X-Poll-Interval`` (seconds).

    Defaults to ``60`` until a ``/notifications`` response reports a value. The
    background poller throttles to ``max(60, notifications_poll_interval())``.
    """
    return _poll_interval


async def list_notifications(
    *, all: bool = False, participating: bool = False
) -> list[Notification]:
    """List the authenticated user's notification threads via ``GET /notifications``.

    Uses a conditional request: the previous ``ETag`` is sent as
    ``If-None-Match`` and a ``304 Not Modified`` returns the cached list without
    re-parsing (and, importantly for the poller, without consuming a rate-limit
    unit). The response's ``X-Poll-Interval`` is captured for
    :func:`notifications_poll_interval`.

    Args:
        all: Include read notifications (``?all=true``), not just unread.
        participating: Restrict to participating threads (``?participating=true``).

    Note:
        Returns only the **first page** (GitHub's default page size). This is
        deliberate: the inbox shows the most recent threads and the poller diffs
        ids across polls, so new items always surface; following ``Link``
        pagination would fan out unbounded requests every poll and burn the
        user's rate limit. Raise ``per_page`` before reaching for pagination.

    Raises:
        GitAutomationError: ``github_notifications_failed`` on an API error.
    """
    global _poll_interval

    params: dict[str, str] = {}
    if all:
        params["all"] = "true"
    if participating:
        params["participating"] = "true"
    cache_key = f"all={all}&participating={participating}"

    extra_headers: dict[str, str] = {}
    cached = _etag_cache.get(cache_key)
    if cached is not None:
        extra_headers["If-None-Match"] = cached[0]

    resp = await _request(
        "GET",
        "/notifications",
        error_code="github_notifications_failed",
        params=params or None,
        extra_headers=extra_headers or None,
        allow_304=True,
    )

    poll = resp.headers.get("X-Poll-Interval")
    if poll:
        try:
            _poll_interval = int(poll)
        except ValueError:
            pass

    if resp.status_code == 304 and cached is not None:
        return cached[1]

    data = resp.json() or []
    notifications = [_to_notification(item) for item in data]

    etag = resp.headers.get("ETag")
    if etag:
        _etag_cache[cache_key] = (etag, notifications)
    return notifications


async def mark_notification_read(thread_id: str) -> None:
    """Mark a notification thread read via ``PATCH /notifications/threads/{id}``.

    Raises:
        GitAutomationError: ``invalid_argument`` for a bad ``thread_id`` or
            ``github_mark_read_failed`` on an API error.
    """
    _validate_thread_id(thread_id)
    await _request(
        "PATCH",
        f"/notifications/threads/{thread_id}",
        error_code="github_mark_read_failed",
    )


async def list_issues(
    *, repo: str | None = None, filter: str = "assigned", state: str = "open"
) -> list[IssueSummary]:
    """List issues, either across GitHub by filter or within a single repo.

    Mapping:
      * no repo, ``assigned``/``created`` -> ``GET /issues?filter=&state=``
      * no repo, ``mentioned`` -> ``GET /search/issues?q=mentions:@me ...``
      * ``repo`` + ``all`` -> ``GET /repos/{repo}/issues?state=``
      * ``repo`` + a filter -> ``GET /search/issues?q=repo:{repo} <qual>:@me ...``

    Pull requests (which the REST issue endpoints fold in) are filtered out so
    the result is issues-only, matching the previous behaviour.

    Raises:
        GitAutomationError: ``invalid_argument`` for an unknown ``filter``/
            ``repo``/``state``, or ``github_issues_failed`` on an API error.
    """
    if filter not in _VALID_FILTERS:
        raise GitAutomationError(
            "invalid_argument",
            f"Invalid filter: expected one of {sorted(_VALID_FILTERS)}.",
            400,
        )
    _validate_state(state)
    if repo is not None:
        _validate_repo(repo)
    elif filter == "all":
        raise GitAutomationError("invalid_argument", "filter 'all' requires a repo.", 400)

    if repo is None:
        if filter == "mentioned":
            q = f"mentions:@me state:{state} type:issue"
            resp = await _request(
                "GET", "/search/issues", error_code="github_issues_failed", params={"q": q}
            )
            items = (resp.json() or {}).get("items", [])
            return [_to_issue_summary(item) for item in items]
        resp = await _request(
            "GET",
            "/issues",
            error_code="github_issues_failed",
            params={"filter": filter, "state": state},
        )
        data = resp.json() or []
        return [_to_issue_summary(item) for item in data if "pull_request" not in item]

    if filter == "all":
        resp = await _request(
            "GET",
            f"/repos/{repo}/issues",
            error_code="github_issues_failed",
            params={"state": state},
        )
        data = resp.json() or []
        return [_to_issue_summary(item, repo) for item in data if "pull_request" not in item]

    qualifier = _SEARCH_QUALIFIER[filter]
    q = f"repo:{repo} {qualifier}:@me state:{state} type:issue"
    resp = await _request(
        "GET", "/search/issues", error_code="github_issues_failed", params={"q": q}
    )
    items = (resp.json() or {}).get("items", [])
    return [_to_issue_summary(item, repo) for item in items]


async def get_issue(repo: str, number: int) -> IssueDetail:
    """Fetch one issue (body + comments), running the two GETs concurrently.

    Raises:
        GitAutomationError: ``invalid_argument`` for a bad ``repo``/``number`` or
            ``github_issue_failed`` on an API error.
    """
    _validate_repo(repo)
    _validate_number(number)
    detail_resp, comments_resp = await asyncio.gather(
        _request("GET", f"/repos/{repo}/issues/{number}", error_code="github_issue_failed"),
        _request(
            "GET", f"/repos/{repo}/issues/{number}/comments", error_code="github_issue_failed"
        ),
    )
    item = detail_resp.json() or {}
    comments = comments_resp.json() or []
    return IssueDetail(
        repo=repo,
        number=item.get("number", number),
        title=item.get("title", ""),
        state=item.get("state", ""),
        author=(item.get("user") or {}).get("login", ""),
        body=item.get("body") or "",
        assignees=_logins(item.get("assignees")),
        labels=_label_names(item.get("labels")),
        comments=[_to_comment(c) for c in comments],
        url=item.get("html_url", ""),
    )


async def add_comment(repo: str, number: int, body: str) -> Comment:
    """Post a comment via ``POST /repos/{repo}/issues/{n}/comments``.

    The created comment is returned as GitHub reports it (real author and
    timestamp), avoiding a second round-trip for the login.

    Raises:
        GitAutomationError: ``invalid_argument`` for a bad ``repo``/``number`` or
            ``github_comment_failed`` on an API error.
    """
    _validate_repo(repo)
    _validate_number(number)
    resp = await _request(
        "POST",
        f"/repos/{repo}/issues/{number}/comments",
        error_code="github_comment_failed",
        json_body={"body": body},
    )
    data = resp.json() or {}
    return Comment(
        author=(data.get("user") or {}).get("login", ""),
        body=data.get("body", body),
        created_at=data.get("created_at", ""),
    )


async def current_login() -> str:
    """Return the authenticated user's login via ``GET /user``.

    Raises:
        GitAutomationError: ``github_user_failed`` on an API error or a missing
            ``login`` field.
    """
    resp = await _request("GET", "/user", error_code="github_user_failed")
    data = resp.json() or {}
    login = data.get("login") if isinstance(data, dict) else None
    if not login:
        raise GitAutomationError(
            "github_user_failed",
            "Could not determine the authenticated user.",
            400,
        )
    return login
