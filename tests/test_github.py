"""Tests for the GitHub cockpit module (core client + web router).

Every test monkeypatches ``core.process.run_process`` so neither ``gh`` nor the
network is ever touched; assertions cover the exact argv built (and any stdin),
JSON->model mapping, and error handling on gh failure.
"""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.github import client
from git_automation.core.process import ProcessResult
from git_automation.web.api import github as github_api


def _patch_run(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Patch process.run_process; record each call's args/cwd/stdin; delegate."""
    recorded: list[dict] = []

    async def fake_run(
        args: list[str], cwd: str | None = None, stdin: str | None = None
    ) -> ProcessResult:
        recorded.append({"args": args, "cwd": cwd, "stdin": stdin})
        return handler(args, stdin)

    monkeypatch.setattr(process, "run_process", fake_run)
    return recorded


def _ok(payload) -> ProcessResult:
    return ProcessResult(0, json.dumps(payload), "")


# --- fixtures: sample gh JSON ------------------------------------------------

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

SEARCH_ISSUES_JSON = [
    {
        "number": 42,
        "title": "Fix the bug",
        "state": "open",
        "repository": {"name": "hello", "nameWithOwner": "octocat/hello"},
        "author": {"login": "octocat"},
        "assignees": [{"login": "octocat"}, {"login": "hubot"}],
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "commentsCount": 3,
        "updatedAt": "2026-06-30T10:00:00Z",
        "url": "https://github.com/octocat/hello/issues/42",
    }
]

LIST_ISSUES_JSON = [
    {
        "number": 5,
        "title": "Repo issue",
        "state": "OPEN",
        "author": {"login": "octocat"},
        "assignees": [{"login": "octocat"}],
        "labels": [{"name": "enhancement"}],
        "comments": [{"author": {"login": "a"}}, {"author": {"login": "b"}}],
        "updatedAt": "2026-06-30T11:00:00Z",
        "url": "https://github.com/octocat/hello/issues/5",
    }
]

ISSUE_DETAIL_JSON = {
    "number": 42,
    "title": "Fix the bug",
    "state": "open",
    "author": {"login": "octocat"},
    "body": "Something is broken.",
    "assignees": [{"login": "hubot"}],
    "labels": [{"name": "bug"}],
    "comments": [
        {"author": {"login": "alice"}, "body": "I can repro.", "createdAt": "2026-06-30T12:00:00Z"},
        {"author": {"login": "bob"}, "body": "On it.", "createdAt": "2026-06-30T13:00:00Z"},
    ],
    "url": "https://github.com/octocat/hello/issues/42",
}


# --- list_notifications ------------------------------------------------------


async def test_list_notifications_default_argv_and_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(NOTIFICATIONS_JSON))
    notes = await client.list_notifications()

    assert recorded[0]["args"] == ["gh", "api", "/notifications"]
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
    # PR number is derived from the subject url too.
    assert notes[1].number == 7
    assert notes[1].subject_type == "PullRequest"


async def test_list_notifications_all_and_participating_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    await client.list_notifications(all=True, participating=True)
    assert recorded[0]["args"] == [
        "gh",
        "api",
        "/notifications?all=true&participating=true",
    ]


async def test_list_notifications_all_only(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    await client.list_notifications(all=True)
    assert recorded[0]["args"] == ["gh", "api", "/notifications?all=true"]


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
    _patch_run(monkeypatch, lambda a, s: _ok(payload))
    notes = await client.list_notifications()
    assert notes[0].number is None
    assert notes[0].url is None


async def test_list_notifications_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "HTTP 401: Bad credentials"))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_notifications()
    assert exc.value.code == "github_notifications_failed"
    assert "401" in exc.value.message


async def test_list_notifications_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(0, "not json", ""))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_notifications()
    assert exc.value.code == "github_notifications_failed"


# --- mark_notification_read --------------------------------------------------


async def test_mark_notification_read_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: ProcessResult(0, "", ""))
    await client.mark_notification_read("100")
    assert recorded[0]["args"] == [
        "gh",
        "api",
        "-X",
        "PATCH",
        "/notifications/threads/100",
    ]


async def test_mark_notification_read_rejects_option_like_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: ProcessResult(0, "", ""))
    with pytest.raises(GitAutomationError) as exc:
        await client.mark_notification_read("--evil")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_mark_notification_read_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "not found"))
    with pytest.raises(GitAutomationError) as exc:
        await client.mark_notification_read("100")
    assert exc.value.code == "github_mark_read_failed"


# --- list_issues -------------------------------------------------------------


async def test_list_issues_search_assigned_argv_and_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(SEARCH_ISSUES_JSON))
    issues = await client.list_issues()

    assert recorded[0]["args"] == [
        "gh",
        "search",
        "issues",
        "--assignee",
        "@me",
        "--state",
        "open",
        "--json",
        "number,title,state,repository,author,assignees,labels,commentsCount,updatedAt,url",
    ]
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


async def test_list_issues_search_mentioned_uses_mentions_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    await client.list_issues(filter="mentioned", state="closed")
    assert recorded[0]["args"][:7] == [
        "gh",
        "search",
        "issues",
        "--mentions",
        "@me",
        "--state",
        "closed",
    ]


async def test_list_issues_search_created_uses_author_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    await client.list_issues(filter="created")
    assert recorded[0]["args"][3] == "--author"


async def test_list_issues_repo_uses_issue_list_and_comment_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(LIST_ISSUES_JSON))
    issues = await client.list_issues(repo="octocat/hello", filter="all", state="open")

    assert recorded[0]["args"] == [
        "gh",
        "issue",
        "list",
        "--repo",
        "octocat/hello",
        "--state",
        "open",
        "--json",
        "number,title,state,author,assignees,labels,comments,updatedAt,url",
    ]
    assert issues[0].repo == "octocat/hello"
    assert issues[0].number == 5
    # comments list -> count
    assert issues[0].comments == 2


async def test_list_issues_repo_with_filter_adds_mention_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    await client.list_issues(repo="octocat/hello", filter="mentioned")
    args = recorded[0]["args"]
    # gh issue list uses singular --mention
    assert "--mention" in args
    assert args[args.index("--mention") + 1] == "@me"


async def test_list_issues_all_without_repo_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(filter="all")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_list_issues_invalid_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(filter="bogus")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_list_issues_invalid_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok([]))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues(repo="not-a-slug")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_list_issues_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "search failed"))
    with pytest.raises(GitAutomationError) as exc:
        await client.list_issues()
    assert exc.value.code == "github_issues_failed"


# --- get_issue ---------------------------------------------------------------


async def test_get_issue_argv_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(ISSUE_DETAIL_JSON))
    detail = await client.get_issue("octocat/hello", 42)

    assert recorded[0]["args"] == [
        "gh",
        "issue",
        "view",
        "42",
        "--repo",
        "octocat/hello",
        "--json",
        "number,title,state,author,body,assignees,labels,comments,url",
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


async def test_get_issue_rejects_bad_number(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(ISSUE_DETAIL_JSON))
    with pytest.raises(GitAutomationError) as exc:
        await client.get_issue("octocat/hello", 0)
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_get_issue_rejects_bool_number(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(ISSUE_DETAIL_JSON))
    with pytest.raises(GitAutomationError):
        await client.get_issue("octocat/hello", True)  # noqa: FBT003
    assert recorded == []


async def test_get_issue_rejects_bad_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok(ISSUE_DETAIL_JSON))
    with pytest.raises(GitAutomationError) as exc:
        await client.get_issue("--evil/x", 1)
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_get_issue_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "could not resolve to an Issue"))
    with pytest.raises(GitAutomationError) as exc:
        await client.get_issue("octocat/hello", 999)
    assert exc.value.code == "github_issue_failed"


# --- add_comment -------------------------------------------------------------


async def test_add_comment_body_on_stdin_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(args, stdin):
        # The /user lookup feeds the returned Comment's author.
        if args[:3] == ["gh", "api", "/user"]:
            return _ok({"login": "octocat"})
        return ProcessResult(0, "https://github.com/octocat/hello/issues/42#issuecomment-1", "")

    recorded = _patch_run(monkeypatch, handler)
    comment = await client.add_comment("octocat/hello", 42, "Thanks for the report!")

    post = recorded[0]
    assert post["args"] == [
        "gh",
        "issue",
        "comment",
        "42",
        "--repo",
        "octocat/hello",
        "--body-file",
        "-",
    ]
    assert post["stdin"] == "Thanks for the report!"
    # Body never appears in argv.
    assert "Thanks for the report!" not in " ".join(post["args"])
    assert comment.author == "octocat"
    assert comment.body == "Thanks for the report!"


async def test_add_comment_rejects_bad_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: ProcessResult(0, "", ""))
    with pytest.raises(GitAutomationError) as exc:
        await client.add_comment("bad", 1, "hi")
    assert exc.value.code == "invalid_argument"
    assert recorded == []


async def test_add_comment_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "permission denied"))
    with pytest.raises(GitAutomationError) as exc:
        await client.add_comment("octocat/hello", 42, "hi")
    assert exc.value.code == "github_comment_failed"


# --- current_login -----------------------------------------------------------


async def test_current_login_argv_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: _ok({"login": "octocat"}))
    login = await client.current_login()
    assert recorded[0]["args"] == ["gh", "api", "/user"]
    assert login == "octocat"


async def test_current_login_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "not logged in"))
    with pytest.raises(GitAutomationError) as exc:
        await client.current_login()
    assert exc.value.code == "github_user_failed"


async def test_current_login_missing_login_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: _ok({}))
    with pytest.raises(GitAutomationError) as exc:
        await client.current_login()
    assert exc.value.code == "github_user_failed"


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
    _patch_run(monkeypatch, lambda a, s: _ok(NOTIFICATIONS_JSON))
    resp = _router_client().get("/api/github/notifications", params={"all": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["number"] == 42
    assert body[0]["reason"] == "mention"


def test_router_mark_read(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda a, s: ProcessResult(0, "", ""))
    resp = _router_client().post("/api/github/notifications/100/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert recorded[0]["args"][-1] == "/notifications/threads/100"


def test_router_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: _ok(SEARCH_ISSUES_JSON))
    resp = _router_client().get("/api/github/issues", params={"filter": "assigned"})
    assert resp.status_code == 200
    assert resp.json()[0]["number"] == 42


def test_router_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: _ok(ISSUE_DETAIL_JSON))
    resp = _router_client().get("/api/github/issue", params={"repo": "octocat/hello", "number": 42})
    assert resp.status_code == 200
    assert resp.json()["comments"][0]["author"] == "alice"


def test_router_issue_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(args, stdin):
        if args[:3] == ["gh", "api", "/user"]:
            return _ok({"login": "octocat"})
        return ProcessResult(0, "ok", "")

    recorded = _patch_run(monkeypatch, handler)
    resp = _router_client().post(
        "/api/github/issue/comment",
        json={"repo": "octocat/hello", "number": 42, "body": "hello"},
    )
    assert resp.status_code == 200
    assert resp.json()["author"] == "octocat"
    assert resp.json()["body"] == "hello"
    assert recorded[0]["stdin"] == "hello"


def test_router_me(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: _ok({"login": "octocat"}))
    resp = _router_client().get("/api/github/me")
    assert resp.status_code == 200
    assert resp.json() == {"login": "octocat"}


def test_router_error_maps_to_domain_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda a, s: ProcessResult(1, "", "HTTP 401"))
    resp = _router_client().get("/api/github/notifications")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "github_notifications_failed"
