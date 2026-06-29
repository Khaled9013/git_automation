"""API tests for repo inspection and remote git operations (real temp repos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from git_automation.web.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True, check=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(path: Path, name: str = "file.txt", content: str = "hi") -> None:
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", f"add {name}")


def test_get_repo_status(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "remote", "add", "origin", "https://example.com/a.git")

    resp = client.get("/api/repo", params={"path": str(repo)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(repo)
    assert body["current_branch"] == "main"
    assert body["upstream"] is None
    assert body["dirty"] is False
    assert body["ahead"] == 0 and body["behind"] == 0
    assert body["remotes"] == [{"name": "origin", "url": "https://example.com/a.git"}]


def test_get_repo_remotes(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", "https://example.com/a.git")
    resp = client.get("/api/repo/remotes", params={"path": str(repo)})
    assert resp.status_code == 200
    assert resp.json() == [{"name": "origin", "url": "https://example.com/a.git"}]


def test_get_repo_invalid_path_error_shape(client: TestClient) -> None:
    resp = client.get("/api/repo", params={"path": "/nope/not/here-xyz"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"


def test_push_pull_fetch_endpoints(client: TestClient, tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True
    )

    work = _init_repo(tmp_path / "work")
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work)

    push = client.post(
        "/api/git/push",
        json={"path": str(work), "remote": "origin", "branch": "main", "set_upstream": True},
    )
    assert push.status_code == 200
    assert push.json()["ok"] is True

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], capture_output=True, check=True)
    _git(clone, "config", "user.name", "Clone User")
    _git(clone, "config", "user.email", "clone@example.com")

    _commit(work, name="second.txt", content="more")
    client.post("/api/git/push", json={"path": str(work), "remote": "origin"})

    fetch = client.post("/api/git/fetch", json={"path": str(clone), "remote": "origin"})
    assert fetch.status_code == 200
    assert fetch.json()["ok"] is True

    pull = client.post(
        "/api/git/pull", json={"path": str(clone), "remote": "origin", "branch": "main"}
    )
    assert pull.status_code == 200
    assert pull.json()["ok"] is True
    assert (clone / "second.txt").exists()


def test_push_invalid_path_error_shape(client: TestClient) -> None:
    resp = client.post(
        "/api/git/push",
        json={"path": "/nope/not/here-xyz", "remote": "origin"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"
