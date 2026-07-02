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

# Conditional-request cache for ``/notifications`` plus the advertised poll
# interval, keyed by the request's query string so distinct
# ``all``/``participating`` views never serve each other's cached body. Each
# entry is ``(etag, last_modified, notifications)`` where ``etag`` and
# ``last_modified`` are the validators to replay (either may be ``None``).
_NotificationCacheEntry = tuple[str | None, str | None, list[Notification]]
_etag_cache: dict[str, _NotificationCacheEntry] = {}
_poll_interval: int = _DEFAULT_POLL_INTERVAL

# One lock per cache key makes the read-request-write cycle in
# :func:`list_notifications` atomic per view. Without it, the background poller
# and a request handler can interleave around the ``await`` on the network call:
# a ``304`` could return a list captured *before* an interleaving ``200`` wrote
# fresh state (a stale snapshot), and two concurrent ``200``s could race so the
# last writer clobbers the newer cache entry. Serialising per key closes both.
_cache_locks: dict[str, asyncio.Lock] = {}


def _cache_lock(cache_key: str) -> asyncio.Lock:
    """Return the per-``cache_key`` lock, creating it on first use.

    Creation is safe without its own lock because the event loop only switches
    at ``await`` points and this function contains none: the ``setdefault`` runs
    to completion atomically for a given key.
    """
    lock = _cache_locks.get(cache_key)
    if lock is None:
        lock = _cache_locks.setdefault(cache_key, asyncio.Lock())
    return lock


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
    raw_id = item.get("id")
    return Comment(
        id=raw_id if isinstance(raw_id, int) else None,
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


def _usable_etag(etag: str | None) -> str | None:
    """Return ``etag`` only if it carries a real validator, else ``None``.

    GitHub's ``/notifications`` can return an *empty* weak validator (``W/""`` or
    ``""``). Echoing that back as ``If-None-Match`` makes GitHub answer ``304`` to
    *every* subsequent request regardless of new activity, so our cache would keep
    serving a stale list forever (the notifications pane appears frozen while the
    uncached issues pane updates fine). Treating an empty validator as "no ETag"
    forces a fresh ``200`` fetch each poll, which is correct behaviour.
    """
    if not etag:
        return None
    core = etag.strip()
    if core.startswith(("W/", "w/")):
        core = core[2:].strip()
    if core.strip('"') == "":
        return None
    return etag


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

    Uses a conditional request: the previously seen ``ETag`` (as
    ``If-None-Match``) and/or ``Last-Modified`` (as ``If-Modified-Since``) are
    replayed and a ``304 Not Modified`` returns the cached list without
    re-parsing (and, importantly for the poller, without consuming a rate-limit
    unit). GitHub serves an *empty* ETag for ``/notifications`` but does populate
    ``Last-Modified``, so the ``Last-Modified`` path is what actually keeps polls
    cheap here. The response's ``X-Poll-Interval`` is captured for
    :func:`notifications_poll_interval`.

    Concurrency: the read-request-write cycle is serialised per view by a
    per-``cache_key`` :class:`asyncio.Lock`, so the background poller and request
    handlers cannot interleave to serve a stale snapshot or clobber fresher cache
    state. A ``304`` returns the value cached under the lock at that moment, never
    a snapshot captured before an interleaving write.

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

    # Hold the per-key lock across read-request-write so a 304 resolves against
    # the cache state at *this* moment and concurrent 200s cannot clobber each
    # other (last-writer-wins on stale data).
    async with _cache_lock(cache_key):
        cached = _etag_cache.get(cache_key)
        extra_headers: dict[str, str] = {}
        if cached is not None:
            cached_etag, cached_last_modified, _ = cached
            if cached_etag:
                extra_headers["If-None-Match"] = cached_etag
            if cached_last_modified:
                extra_headers["If-Modified-Since"] = cached_last_modified

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

        if resp.status_code == 304:
            # Re-read under the lock: another awaited call may have replaced the
            # entry while we were on the wire. Never return a pre-await snapshot.
            current = _etag_cache.get(cache_key)
            if current is not None:
                return current[2]
            return []

        data = resp.json() or []
        notifications = [_to_notification(item) for item in data]

        # Cache a *usable* ETag and/or the ``Last-Modified`` validator. An empty
        # ETag (GitHub returns ``W/""`` for this endpoint) is dropped so we never
        # send it as If-None-Match — otherwise GitHub 304s every request and the
        # list freezes on stale data. ``Last-Modified`` fills that gap: it is
        # populated for ``/notifications`` and drives cheap conditional polls.
        etag = _usable_etag(resp.headers.get("ETag"))
        last_modified = resp.headers.get("Last-Modified")
        if etag or last_modified:
            _etag_cache[cache_key] = (etag, last_modified, notifications)
        else:
            _etag_cache.pop(cache_key, None)
        return notifications


async def _apply_read_to_cache(thread_id: str | None) -> None:
    """Reflect a just-applied mark-read in the conditional-request cache.

    GitHub's ``/notifications`` list is eventually consistent (~60s) and its
    ``Last-Modified`` lags a mark-read, so a subsequent conditional poll gets a
    ``304`` and :func:`list_notifications` re-serves the *cached* list -- which
    still shows the thread as unread. That is the "I read it, refreshed, and it
    came back" bug. Patching the cached lists here so the affected thread's
    ``unread`` becomes ``False`` means a ``304`` can no longer resurface it.

    ``thread_id`` is the thread just read, or ``None`` to mark *every* cached
    thread read (for :func:`mark_all_notifications_read`). Validators (ETag /
    ``Last-Modified``) are preserved, so conditional polling stays cheap; a fresh
    ``200`` still overwrites the cache with GitHub's latest state. Each cache key
    is patched under its own lock so this cannot race :func:`list_notifications`'
    read-request-write cycle.
    """
    for cache_key in list(_etag_cache):
        async with _cache_lock(cache_key):
            entry = _etag_cache.get(cache_key)
            if entry is None:
                continue
            etag, last_modified, notes = entry
            patched = [
                note.model_copy(update={"unread": False})
                if note.unread and (thread_id is None or note.id == thread_id)
                else note
                for note in notes
            ]
            _etag_cache[cache_key] = (etag, last_modified, patched)


async def mark_notification_read(thread_id: str) -> None:
    """Mark a notification thread read via ``PATCH /notifications/threads/{id}``.

    On success the conditional cache is patched (:func:`_apply_read_to_cache`) so
    a later ``304`` cannot re-serve the just-read thread as unread.

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
    await _apply_read_to_cache(thread_id)


async def mark_all_notifications_read() -> None:
    """Mark every notification thread read via ``PUT /notifications``.

    GitHub marks all threads read as of "now" and answers ``202``/``205`` (no
    body), so nothing is returned. On success the conditional cache is patched so
    a later ``304`` cannot re-serve any thread as unread.

    Raises:
        GitAutomationError: ``github_mark_read_failed`` on an API error.
    """
    await _request(
        "PUT",
        "/notifications",
        error_code="github_mark_read_failed",
        json_body={"read": True},
    )
    await _apply_read_to_cache(None)


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
    raw_id = data.get("id")
    return Comment(
        id=raw_id if isinstance(raw_id, int) else None,
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
