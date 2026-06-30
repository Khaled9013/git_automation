"""Tests for the GitHub cockpit module (httpx-based core client + web router).

The client talks to ``https://api.github.com`` directly via a shared
:class:`httpx.AsyncClient`. Every test installs an :class:`httpx.MockTransport`
on that shared client (no real network) and stubs the token getter (no ``gh``
subprocess), then asserts the exact METHOD + URL + params + body issued, the
REST-JSON -> pydantic-model mapping, the ETag/304 cache, and the clean error
mapping. The public function signatures and model outputs are unchanged from the
previous ``gh``-subprocess implementation.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.github import client
from git_automation.core.process import ProcessResult
from git_automation.web.api import github as github_api


@pytest.fixture(autouse=True)
def _reset_client_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate module-level client/token/etag/poll state between tests."""
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    monkeypatch.setattr(client, "_etag_cache", {})
    monkeypatch.setattr(client, "_poll_interval", 60)
    yield


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Install a MockTransport handler on the shared client; record requests."""
    recorded: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return handler(request)

    mock = httpx.AsyncClient(
        transport=httpx.MockTransport(_wrapped),
        base_url=client._API_BASE,
        headers={"Accept": client._ACCEPT, "X-GitHub-Api-Version": client._API_VERSION},
    )
    monkeypatch.setattr(client, "_client", mock)
    return recorded


def _json(payload, status: int = 200, headers: dict | None = None):
    """A handler that always returns ``payload`` as JSON with ``status``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or {})

    return handler


# --- fixtures: sample REST JSON ----------------------------------------------

NOTIFICATIONS_JSON = [
    {
        "id": "100",
        "reason": "mention",
        "unread": True,
        "updated_at": "2026-06-30T10:00:00Z",
        "subject": {
            "title": "Fix the bug",
            "type": "Issue",
            "url": "https://api.github.com/repos/octocat/hello/issues/42",
        },
        "repository": {"full_name": "octocat/hello"},
    },
    {
        "id": "101",
        "reason": "review_requested",
        "unread": False,
        "updated_at": "2026-06-29T09:00:00Z",
        "subject": {
            "title": "Add feature",
            "type": "PullRequest",
            "url": "https://api.github.com/repos/octocat/hello/pulls/7",
        },
        "repository": {"full_name": "octocat/hello"},
    },
]

# /issues and /repos/{repo}/issues REST shape: user.login, comments int, html_url.
ISSUES_REST_JSON = [
    {
        "number": 42,
        "title": "Fix the bug",
        "state": "open",
        "user": {"login": "octocat"},
        "assignees": [{"login": "octocat"}, {"login": "hubot"}],
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "comments": 3,
        "updated_at": "2026-06-30T10:00:00Z",
        "html_url": "https://github.com/octocat/hello/issues/42",
        "repository": {"full_name": "octocat/hello"},
    },
    {
        # A pull request folded into /issues -> filtered out.
        "number": 7,
        "title": "A PR",
        "state": "open",
        "user": {"login": "octocat"},
        "assignees": [],
        "labels": [],
        "comments": 0,
        "updated_at": "2026-06-30T10:00:00Z",
        "html_url": "https://github.com/octocat/hello/pull/7",
        "repository": {"full_name": "octocat/hello"},
        "pull_request": {"url": "https://api.github.com/repos/octocat/hello/pulls/7"},
    },
]

# /search/issues REST shape: items[] with repository_url instead of repository.
SEARCH_ISSUES_JSON = {
    "total_count": 1,
    "items": [
        {
            "number": 42,
            "title": "Fix the bug",
            "state": "open",
            "user": {"login": "octocat"},
            "assignees": [{"login": "octocat"}, {"login": "hubot"}],
            "labels": [{"name": "bug"}, {"name": "p1"}],
            "comments": 3,
            "updated_at": "2026-06-30T10:00:00Z",
            "html_url": "https://github.com/octocat/hello/issues/42",
            "repository_url": "https://api.github.com/repos/octocat/hello",
        }
    ],
}

ISSUE_DETAIL_JSON = {
    "number": 42,
    "title": "Fix the bug",
    "state": "open",
    "user": {"login": "octocat"},
    "body": "Something is broken.",
    "assignees": [{"login": "hubot"}],
    "labels": [{"name": "bug"}],
    "html_url": "https://github.com/octocat/hello/issues/42",
}

ISSUE_COMMENTS_JSON = [
    {"user": {"login": "alice"}, "body": "I can repro.", "created_at": "2026-06-30T12:00:00Z"},
    {"user": {"login": "bob"}, "body": "On it.", "created_at": "2026-06-30T13:00:00Z"},
]


# --- list_notifications ------------------------------------------------------


async def test_list_notifications_default_url_and_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install(monkeypatch, _json(NOTIFICATIONS_JSON))
    notes = await client.list_notifications()

    req = recorded[0]
    assert req.method == "GET"
    assert req.url.path == "/notifications"
    assert req.url.query == b""
    assert req.headers["Authorization"] == "Bearer test-token"
    assert req.headers["Accept"] == client._ACCEPT

    assert len(notes) == 2
    first = notes[0]
    assert first.id == "100"
    assert first.reason == "mention"
    assert first.unread is True
    assert first.title == "Fix the bug"
    assert first.subject_type == "Issue"
    assert first.repo == "octocat/hello"
    assert first.number == 42
    assert first.url == "https://api.github.com/repos/octocat/hello/issues/42"
    assert first.updated_at == "2026-06-30T10:00:00Z"
    assert notes[1].number == 7
    assert notes[1].subject_type == "PullRequest"


async def test_list_notifications_all_and_participating_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install(monkeypatch, _json([]))
    await client.list_notifications(all=True, participating=True)
    params = recorded[0].url.params
    assert params.get("all") == "true"
    assert params.get("participating") == "true"


async def test_list_notifications_all_only(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json([]))
    await client.list_notifications(all=True)
    params = recorded[0].url.params
    assert params.get("all") == "true"
    assert "participating" not in params


async def test_list_notifications_etag_and_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ETag is captured, sent as If-None-Match, and a 304 serves the cache."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == 'W/"abc"':
            return httpx.Response(304, headers={"X-Poll-Interval": "90", "ETag": 'W/"abc"'})
        return httpx.Response(
            200,
            json=NOTIFICATIONS_JSON,
            headers={"ETag": 'W/"abc"', "X-Poll-Interval": "75"},
        )

    recorded = _install(monkeypatch, handler)

    first = await client.list_notifications()
    assert len(first) == 2
    assert client.notifications_poll_interval() == 75
    assert "If-None-Match" not in recorded[0].headers

    second = await client.list_notifications()
    assert recorded[1].headers["If-None-Match"] == 'W/"abc"'
    # 304 -> same cached objects returned without re-parsing.
    assert second == first
    assert client.notifications_poll_interval() == 90


async def test_list_notifications_unparseable_number_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "id": "200",
            "reason": "comment",
            "unread": True,
            "updated_at": "x",
            "subject": {"title": "A release", "type": "Release", "url": None},
            "repository": {"full_name": "octocat/hello"},
        }
    ]
    _install(monkeypatch, _json(payload))
    notes = await client.list_notifications()
    assert notes[0].number is None
    assert notes[0].url is None


async def test_list_notifications_empty_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json([]))
    assert await client.list_notifications() == []


async def test_list_notifications_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 401 (after a token refresh) maps to a clean domain error."""
    _install(monkeypatch, _json({"message": "Bad credentials"}, status=401))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_notifications()
    assert exc.value.code == "github_notifications_failed"
    assert "401" in exc.value.message
    assert "test-token" not in exc.value.message


async def test_list_notifications_403_rate_limit_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _json({"message": "API rate limit exceeded"}, status=403),
    )
    with pytest.raises(GitAutomationError) as exc:
        await client.list_notifications()
    assert exc.value.code == "github_notifications_failed"
    assert "rate limit" in exc.value.message.lower()


async def test_401_triggers_single_token_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a 401 the token is refreshed once and the request retried."""
    monkeypatch.setattr(client, "_token", None)
    tokens = iter(["tok-1", "tok-2"])

    async def fake_run(args, cwd=None, stdin=None):
        assert args == ["gh", "auth", "token"]
        return ProcessResult(0, next(tokens) + "\n", "")

    monkeypatch.setattr(process, "run_process", fake_run)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"login": "octocat"})

    _install(monkeypatch, handler)
    assert await client.current_login() == "octocat"
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


# --- mark_notification_read --------------------------------------------------


async def test_mark_notification_read_method_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install(monkeypatch, _json({}, status=205))
    await client.mark_notification_read("100")
    req = recorded[0]
    assert req.method == "PATCH"
    assert req.url.path == "/notifications/threads/100"


async def test_mark_notification_read_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json({"message": "Not Found"}, status=404))
    with pytest.raises(GitAutomationError) as exc:
        await client.mark_notification_read("999")
    assert exc.value.code == "github_mark_read_failed"
    assert "404" in exc.value.message


@pytest.mark.parametrize(
    "bad_id",
    ["", "-1", "1/comments", "1?foo=bar", "../user", "12a", "abc", "1 2"],
)
async def test_mark_notification_read_rejects_non_numeric_id(
    monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    """A non-numeric thread id (G1) is rejected before any request."""
    recorded = _install(monkeypatch, _json({}, status=205))
    with pytest.raises(GitAutomationError) as exc:
        await client.mark_notification_read(bad_id)
    assert exc.value.code == "invalid_argument"
    assert recorded == []


# --- list_issues -------------------------------------------------------------


async def test_list_issues_assigned_url_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json(ISSUES_REST_JSON))
    issues = await client.list_issues()

    req = recorded[0]
    assert req.method == "GET"
    assert req.url.path == "/issues"
    assert req.url.params.get("filter") == "assigned"
    assert req.url.params.get("state") == "open"

    # PR folded into /issues is filtered out -> only the issue remains.
    assert len(issues) == 1
    issue = issues[0]
    assert issue.repo == "octocat/hello"
    assert issue.number == 42
    assert issue.title == "Fix the bug"
    assert issue.state == "open"
    assert issue.author == "octocat"
    assert issue.assignees == ["octocat", "hubot"]
    assert issue.labels == ["bug", "p1"]
    assert issue.comments == 3
    assert issue.updated_at == "2026-06-30T10:00:00Z"
    assert issue.url == "https://github.com/octocat/hello/issues/42"


async def test_list_issues_created_uses_filter_created(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json([]))
    await client.list_issues(filter="created", state="closed")
    req = recorded[0]
    assert req.url.path == "/issues"
    assert req.url.params.get("filter") == "created"
    assert req.url.params.get("state") == "closed"


async def test_list_issues_mentioned_uses_search(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json(SEARCH_ISSUES_JSON))
    issues = await client.list_issues(filter="mentioned", state="open")
    req = recorded[0]
    assert req.url.path == "/search/issues"
    assert req.url.params.get("q") == "mentions:@me state:open type:issue"
    # search items[] mapped; repo derived from repository_url.
    assert issues[0].repo == "octocat/hello"
    assert issues[0].number == 42


async def test_list_issues_repo_all_uses_repo_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install(monkeypatch, _json(ISSUES_REST_JSON))
    issues = await client.list_issues(repo="octocat/hello", filter="all", state="open")
    req = recorded[0]
    assert req.url.path == "/repos/octocat/hello/issues"
    assert req.url.params.get("state") == "open"
    assert issues[0].repo == "octocat/hello"
    assert issues[0].number == 42


async def test_list_issues_repo_with_filter_uses_search_qualifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install(monkeypatch, _json(SEARCH_ISSUES_JSON))
    await client.list_issues(repo="octocat/hello", filter="mentioned")
    req = recorded[0]
    assert req.url.path == "/search/issues"
    assert req.url.params.get("q") == "repo:octocat/hello mentions:@me state:open type:issue"


async def test_list_issues_all_without_repo_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(filter="all")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


@pytest.mark.parametrize("bad_state", ["all", "merged", "--repo", "Open", "", "open closed"])
async def test_list_issues_rejects_invalid_state(
    monkeypatch: pytest.MonkeyPatch, bad_state: str
) -> None:
    """state is allow-listed to {open, closed} (G2); nothing reaches the API."""
    recorded = _install(monkeypatch, _json([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(state=bad_state)
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_list_issues_empty_list_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json([]))
    assert await client.list_issues() == []


async def test_list_issues_rate_limited_is_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _json({"message": "API rate limit exceeded"}, status=403))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues()
    assert exc.value.code == "github_issues_failed"
    assert "rate limit" in exc.value.message.lower()


async def test_list_issues_invalid_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(filter="bogus")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_list_issues_invalid_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(repo="not-a-slug")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


# --- get_issue ---------------------------------------------------------------


async def test_get_issue_two_requests_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=ISSUE_COMMENTS_JSON)
        return httpx.Response(200, json=ISSUE_DETAIL_JSON)

    recorded = _install(monkeypatch, handler)
    detail = await client.get_issue("octocat/hello", 42)

    paths = sorted(r.url.path for r in recorded)
    assert paths == [
        "/repos/octocat/hello/issues/42",
        "/repos/octocat/hello/issues/42/comments",
    ]
    assert detail.repo == "octocat/hello"
    assert detail.number == 42
    assert detail.title == "Fix the bug"
    assert detail.state == "open"
    assert detail.author == "octocat"
    assert detail.body == "Something is broken."
    assert detail.assignees == ["hubot"]
    assert detail.labels == ["bug"]
    assert len(detail.comments) == 2
    assert detail.comments[0].author == "alice"
    assert detail.comments[0].body == "I can repro."
    assert detail.comments[0].created_at == "2026-06-30T12:00:00Z"
    assert detail.url == "https://github.com/octocat/hello/issues/42"


async def test_get_issue_no_body_no_comments_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail_payload = {
        "number": 9,
        "title": "Done",
        "state": "closed",
        "user": {"login": "octocat"},
        "body": None,
        "assignees": [],
        "labels": [],
        "html_url": "https://github.com/octocat/hello/issues/9",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=detail_payload)

    _install(monkeypatch, handler)
    detail = await client.get_issue("octocat/hello", 9)
    assert detail.state == "closed"
    assert detail.body == ""
    assert detail.comments == []
    assert detail.assignees == []
    assert detail.labels == []


async def test_get_issue_rejects_bad_number(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json(ISSUE_DETAIL_JSON))
    with pytest.raises(GitAutomationError) as exc:
        await client.get_issue("octocat/hello", 0)
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_get_issue_rejects_bool_number(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json(ISSUE_DETAIL_JSON))
    with pytest.raises(GitAutomationError):
        await client.get_issue("octocat/hello", True)  # noqa: FBT003
    assert recorded == []


async def test_get_issue_rejects_bad_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json(ISSUE_DETAIL_JSON))
    with pytest.raises(GitAutomationError) as exc:
        await client.get_issue("--evil/x", 1)
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_get_issue_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json({"message": "Not Found"}, status=404))
    with pytest.raises(GitAutomationError) as exc:
        await client.get_issue("octocat/hello", 999)
    assert exc.value.code == "github_issue_failed"


# --- add_comment -------------------------------------------------------------


async def test_add_comment_posts_body_json(monkeypatch: pytest.MonkeyPatch) -> None:
    created = {
        "user": {"login": "octocat"},
        "body": "Thanks for the report!",
        "created_at": "2026-06-30T14:00:00Z",
    }
    recorded = _install(monkeypatch, _json(created, status=201))
    comment = await client.add_comment("octocat/hello", 42, "Thanks for the report!")

    req = recorded[0]
    assert req.method == "POST"
    assert req.url.path == "/repos/octocat/hello/issues/42/comments"
    import json as _json_mod

    assert _json_mod.loads(req.content) == {"body": "Thanks for the report!"}
    assert comment.author == "octocat"
    assert comment.body == "Thanks for the report!"
    assert comment.created_at == "2026-06-30T14:00:00Z"


async def test_add_comment_preserves_special_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Markdown / newlines / shell metachars survive verbatim in the JSON body."""
    body = "## Heading\n\n- `rm -rf /` & $(whoami)\n> quote\n--flag not a flag\n"
    created = {"user": {"login": "octocat"}, "body": body, "created_at": "t"}
    recorded = _install(monkeypatch, _json(created, status=201))
    comment = await client.add_comment("octocat/hello", 42, body)

    import json as _json_mod

    assert _json_mod.loads(recorded[0].content) == {"body": body}
    assert comment.body == body


async def test_add_comment_rejects_bad_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json({}, status=201))
    with pytest.raises(GitAutomationError) as exc:
        await client.add_comment("bad", 1, "hi")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_add_comment_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json({"message": "Forbidden"}, status=403))
    with pytest.raises(GitAutomationError) as exc:
        await client.add_comment("octocat/hello", 42, "hi")
    assert exc.value.code == "github_comment_failed"


# --- current_login -----------------------------------------------------------


async def test_current_login_url_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install(monkeypatch, _json({"login": "octocat"}))
    login = await client.current_login()
    assert recorded[0].url.path == "/user"
    assert login == "octocat"


async def test_current_login_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json({"message": "Bad credentials"}, status=401))
    with pytest.raises(GitAutomationError) as exc:
        await client.current_login()
    assert exc.value.code == "github_user_failed"


async def test_current_login_missing_login_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _json({}))
    with pytest.raises(GitAutomationError) as exc:
        await client.current_login()
    assert exc.value.code == "github_user_failed"


# --- token / auth ------------------------------------------------------------


async def test_not_authenticated_when_gh_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing ``gh auth token`` surfaces as github_not_authenticated."""
    monkeypatch.setattr(client, "_token", None)

    async def fake_run(args, cwd=None, stdin=None):
        return ProcessResult(1, "", "not logged in")

    monkeypatch.setattr(process, "run_process", fake_run)
    _install(monkeypatch, _json({"login": "octocat"}))
    with pytest.raises(GitAutomationError) as exc:
        await client.current_login()
    assert exc.value.code == "github_not_authenticated"
    assert "not logged in" not in exc.value.message  # gh stderr not leaked


async def test_token_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh auth token`` runs once; the token is reused on subsequent calls."""
    monkeypatch.setattr(client, "_token", None)
    calls = {"n": 0}

    async def fake_run(args, cwd=None, stdin=None):
        calls["n"] += 1
        return ProcessResult(0, "cached-token\n", "")

    monkeypatch.setattr(process, "run_process", fake_run)
    _install(monkeypatch, _json({"login": "octocat"}))
    await client.current_login()
    await client.current_login()
    assert calls["n"] == 1


# --- web router --------------------------------------------------------------


def _router_client() -> TestClient:
    """Mount the github router under /api in an isolated app + error handler."""
    from git_automation.web.api import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(github_api.router)
    app.include_router(api)
    return TestClient(app)


def test_router_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    monkeypatch.setattr(client, "_etag_cache", {})
    _install(monkeypatch, _json(NOTIFICATIONS_JSON))
    resp = _router_client().get("/api/github/notifications", params={"all": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["number"] == 42
    assert body[0]["reason"] == "mention"


def test_router_mark_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    recorded = _install(monkeypatch, _json({}, status=205))
    resp = _router_client().post("/api/github/notifications/100/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert recorded[0].url.path == "/notifications/threads/100"


def test_router_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    _install(monkeypatch, _json(ISSUES_REST_JSON))
    resp = _router_client().get("/api/github/issues", params={"filter": "assigned"})
    assert resp.status_code == 200
    assert resp.json()[0]["number"] == 42


def test_router_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=ISSUE_COMMENTS_JSON)
        return httpx.Response(200, json=ISSUE_DETAIL_JSON)

    _install(monkeypatch, handler)
    resp = _router_client().get("/api/github/issue", params={"repo": "octocat/hello", "number": 42})
    assert resp.status_code == 200
    assert resp.json()["comments"][0]["author"] == "alice"


def test_router_issue_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    created = {"user": {"login": "octocat"}, "body": "hello", "created_at": "t"}
    recorded = _install(monkeypatch, _json(created, status=201))
    resp = _router_client().post(
        "/api/github/issue/comment",
        json={"repo": "octocat/hello", "number": 42, "body": "hello"},
    )
    assert resp.status_code == 200
    assert resp.json()["author"] == "octocat"
    assert resp.json()["body"] == "hello"
    import json as _json_mod

    assert _json_mod.loads(recorded[0].content) == {"body": "hello"}


def test_router_me(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    _install(monkeypatch, _json({"login": "octocat"}))
    resp = _router_client().get("/api/github/me")
    assert resp.status_code == 200
    assert resp.json() == {"login": "octocat"}


def test_router_error_maps_to_domain_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_token", "test-token")
    monkeypatch.setattr(client, "_etag_cache", {})
    _install(monkeypatch, _json({"message": "Bad credentials"}, status=401))
    resp = _router_client().get("/api/github/notifications")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "github_notifications_failed"
