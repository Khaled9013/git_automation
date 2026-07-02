"""Tests for the gh PR client + web router. No real gh/network call is made.

Every test monkeypatches ``core.process.run_process`` so neither ``gh`` nor the
network is ever touched; assertions cover the exact argv built (including
``cwd``), JSON parsing into ``PullRequest``, and error handling on gh failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.github import gh_cli as client
from git_automation.core.github.models import PullRequest  # noqa: F401
from git_automation.core.process import ProcessResult
from git_automation.web.api import pr as pr_api

PR_URL = "https://github.com/octocat/hello/pull/42"
PR_LIST_JSON = (
    '[{"number": 7, "title": "Add feature", "url": '
    '"https://github.com/octocat/hello/pull/7", "state": "OPEN", '
    '"headRefName": "feature", "baseRefName": "main"}]'
)


def _patch_run(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Patch process.run_process; record each call's args/cwd/stdin; delegate."""
    recorded: list[dict] = []

    async def fake_run(
        args: list[str], cwd: str | None = None, stdin: str | None = None
    ) -> ProcessResult:
        recorded.append({"args": args, "cwd": cwd, "stdin": stdin})
        return handler(args, cwd, stdin)

    monkeypatch.setattr(process, "run_process", fake_run)
    return recorded


# --- pr_create ------------------------------------------------------------


async def test_pr_create_builds_full_argv_and_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, PR_URL + "\n", ""))
    result = await client.pr_create(
        str(tmp_path), "My title", body="Body text", base="main", head="feature"
    )

    call = recorded[0]
    assert call["args"] == [
        "gh",
        "pr",
        "create",
        "--title",
        "My title",
        "--body",
        "Body text",
        "--base",
        "main",
        "--head",
        "feature",
    ]
    assert call["cwd"] == str(tmp_path)
    assert result == {"number": 42, "url": PR_URL}


async def test_pr_create_omits_optional_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, PR_URL, ""))
    await client.pr_create(str(tmp_path), "Only a title")

    assert recorded[0]["args"] == ["gh", "pr", "create", "--title", "Only a title"]


async def test_pr_create_parses_number_from_noisy_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    noisy = f"Creating pull request for feature into main\n{PR_URL}\n"
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, noisy, ""))
    result = await client.pr_create(str(tmp_path), "t")
    assert result == {"number": 42, "url": PR_URL}


async def test_pr_create_surfaces_gh_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(1, "", "a pull request already exists"))
    with pytest.raises(GitAutomationError) as exc:
        await client.pr_create(str(tmp_path), "t")
    assert exc.value.code == "gh_pr_create_failed"
    assert "already exists" in exc.value.message


async def test_pr_create_unparseable_url_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, "no url here", ""))
    with pytest.raises(GitAutomationError) as exc:
        await client.pr_create(str(tmp_path), "t")
    assert exc.value.code == "gh_pr_create_failed"


async def test_pr_create_rejects_option_like_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, PR_URL, ""))
    with pytest.raises(GitAutomationError) as exc:
        await client.pr_create(str(tmp_path), "t", base="--upload-pack=evil")
    assert exc.value.code == "invalid_argument"
    assert recorded == []  # rejected before any subprocess call


# --- pr_list --------------------------------------------------------------


async def test_pr_list_builds_json_argv_and_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, PR_LIST_JSON, ""))
    prs = await client.pr_list(str(tmp_path))

    call = recorded[0]
    assert call["args"] == [
        "gh",
        "pr",
        "list",
        "--json",
        "number,title,url,state,headRefName,baseRefName",
    ]
    assert call["cwd"] == str(tmp_path)
    assert len(prs) == 1
    pr = prs[0]
    assert pr.number == 7
    assert pr.title == "Add feature"
    assert pr.url == "https://github.com/octocat/hello/pull/7"
    assert pr.state == "OPEN"
    assert pr.head == "feature"
    assert pr.base == "main"


async def test_pr_list_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, "[]", ""))
    assert await client.pr_list(str(tmp_path)) == []


async def test_pr_list_surfaces_gh_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(1, "", "no git remote found"))
    with pytest.raises(GitAutomationError) as exc:
        await client.pr_list(str(tmp_path))
    assert exc.value.code == "gh_pr_list_failed"
    assert "remote" in exc.value.message


async def test_pr_list_invalid_json_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, "not json", ""))
    with pytest.raises(GitAutomationError) as exc:
        await client.pr_list(str(tmp_path))
    assert exc.value.code == "gh_pr_list_failed"


# --- web router -----------------------------------------------------------


def _router_client() -> TestClient:
    """Mount the PR router under /api in an isolated app (manager wires it for real)."""
    app = FastAPI()
    api = APIRouter(prefix="/api")
    api.include_router(pr_api.router)
    app.include_router(api)
    return TestClient(app, base_url="http://127.0.0.1")


def test_router_create_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded = _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, PR_URL, ""))
    resp = _router_client().post(
        "/api/gh/pr/create",
        json={"path": str(tmp_path), "title": "t", "base": "main"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"number": 42, "url": PR_URL}
    assert recorded[0]["args"] == ["gh", "pr", "create", "--title", "t", "--base", "main"]
    assert recorded[0]["cwd"] == str(tmp_path)


def test_router_list_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, lambda a, c, s: ProcessResult(0, PR_LIST_JSON, ""))
    resp = _router_client().get("/api/gh/pr/list", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["number"] == 7
    assert body[0]["head"] == "feature"
    assert body[0]["base"] == "main"
