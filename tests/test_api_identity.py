"""API tests for identity/onboarding endpoints. No real gh auth is performed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from git_automation.core import process
from git_automation.core.process import ProcessResult
from git_automation.web.app import create_app

LOGGED_IN = "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _identity_run(name, email, gh_ok, recorder=None):
    async def fake_run(args, cwd=None, stdin=None):
        if recorder is not None:
            recorder.append({"args": args, "stdin": stdin})
        if args[:2] == ["gh", "auth"]:
            return ProcessResult(0 if gh_ok else 1, "", LOGGED_IN if gh_ok else "no")
        if args[:4] == ["git", "config", "--global", "--get"]:
            value = {"user.name": name, "user.email": email}[args[4]]
            return ProcessResult(0 if value else 1, (value or "") + "\n", "")
        return ProcessResult(0, "", "")

    return fake_run


def test_get_identity(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setattr(process, "run_process", _identity_run("Ada", "ada@x.com", True))
    resp = client.get("/api/identity")
    assert resp.status_code == 200
    assert resp.json() == {
        "git_name": "Ada",
        "git_email": "ada@x.com",
        "gh_authenticated": True,
        "gh_user": "octocat",
        "needs_onboarding": False,
    }


def test_get_identity_needs_onboarding(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setattr(process, "run_process", _identity_run(None, None, False))
    resp = client.get("/api/identity")
    assert resp.json()["needs_onboarding"] is True


def test_set_git_identity(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    recorder: list[dict] = []
    monkeypatch.setattr(process, "run_process", _identity_run("Ada", "ada@x.com", True, recorder))
    resp = client.post("/api/identity/git", json={"name": "Ada", "email": "ada@x.com"})
    assert resp.status_code == 200
    assert resp.json()["git_name"] == "Ada"
    set_calls = [
        c["args"]
        for c in recorder
        if c["args"][:3] == ["git", "config", "--global"] and "--get" not in c["args"]
    ]
    assert ["git", "config", "--global", "user.name", "Ada"] in set_calls


def test_gh_login_instructions(client: TestClient) -> None:
    resp = client.get("/api/gh/login/instructions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["supports_token"] is True
    assert isinstance(body["steps"], list) and body["steps"]


def test_gh_login_token_success(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    recorder: list[dict] = []

    async def fake_run(args, cwd=None, stdin=None):
        recorder.append({"args": args, "stdin": stdin})
        if args[:2] == ["gh", "auth"] and "login" in args:
            return ProcessResult(0, "", "Logged in")
        if args[:2] == ["gh", "auth"]:
            return ProcessResult(0, "", LOGGED_IN)
        if args[:4] == ["git", "config", "--global", "--get"]:
            value = {"user.name": "Ada", "user.email": "ada@x.com"}[args[4]]
            return ProcessResult(0, value + "\n", "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(process, "run_process", fake_run)
    resp = client.post("/api/gh/login/token", json={"token": "secret-token"})
    assert resp.status_code == 200
    assert resp.json()["gh_authenticated"] is True

    login_call = next(c for c in recorder if "login" in c["args"])
    assert login_call["stdin"] == "secret-token"
    assert "secret-token" not in " ".join(login_call["args"])


def test_gh_login_token_failure_error_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def fake_run(args, cwd=None, stdin=None):
        return ProcessResult(1, "", "error: bad credentials")

    monkeypatch.setattr(process, "run_process", fake_run)
    resp = client.post("/api/gh/login/token", json={"token": "secret-token"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "gh_login_failed"
    assert "secret-token" not in body["error"]["message"]
