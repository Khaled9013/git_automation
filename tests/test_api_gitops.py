"""API tests for working-tree / branch / graph endpoints (real temp repos)."""

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


def test_changes_stage_commit_flow(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "new.txt").write_text("data")

    changes = client.get("/api/repo/changes", params={"path": str(repo)})
    assert changes.status_code == 200
    assert changes.json()["untracked"] == ["new.txt"]

    stage = client.post("/api/git/stage", json={"path": str(repo), "files": ["new.txt"]})
    assert stage.status_code == 200 and stage.json()["ok"] is True

    after_stage = client.get("/api/repo/changes", params={"path": str(repo)}).json()
    assert [c["path"] for c in after_stage["staged"]] == ["new.txt"]
    assert after_stage["staged"][0]["status"] == "A"

    commit = client.post("/api/git/commit", json={"path": str(repo), "message": "add new"})
    assert commit.status_code == 200 and commit.json()["ok"] is True
    assert client.get("/api/repo/changes", params={"path": str(repo)}).json()["staged"] == []


def test_unstage_and_discard(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "original")
    (repo / "f.txt").write_text("changed")
    _git(repo, "add", "f.txt")

    unstage = client.post("/api/git/unstage", json={"path": str(repo), "files": ["f.txt"]})
    assert unstage.status_code == 200 and unstage.json()["ok"] is True

    discard = client.post("/api/git/discard", json={"path": str(repo), "files": ["f.txt"]})
    assert discard.status_code == 200 and discard.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "original"


def test_delete_endpoint_tracked_and_untracked(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "v1")
    (repo / "scratch.txt").write_text("temp")

    resp = client.post(
        "/api/git/delete",
        json={"path": str(repo), "files": ["tracked.txt", "scratch.txt"]},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert not (repo / "tracked.txt").exists()
    assert not (repo / "scratch.txt").exists()


def test_delete_endpoint_rejects_traversal(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    secret = tmp_path / "OUTSIDE.txt"
    secret.write_text("secret")

    resp = client.post(
        "/api/git/delete",
        json={"path": str(repo), "files": ["../OUTSIDE.txt"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"
    assert secret.exists()


def test_commit_empty_message_error_shape(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    resp = client.post("/api/git/commit", json={"path": str(repo), "message": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_commit_message"


def test_commit_nothing_staged_fails(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    resp = client.post("/api/git/commit", json={"path": str(repo), "message": "noop"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_diff_endpoint(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "line1\n")
    (repo / "f.txt").write_text("line1\nline2\n")

    resp = client.get("/api/repo/diff", params={"path": str(repo), "file": "f.txt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["file"] == "f.txt"
    assert body["binary"] is False
    assert "+line2" in body["diff"]


def test_branches_endpoint(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "branch", "feature")

    resp = client.get("/api/repo/branches", params={"path": str(repo)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "main"
    assert {b["name"] for b in body["local"]} == {"main", "feature"}


def test_branch_create_checkout_merge_delete(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)

    create = client.post(
        "/api/git/branch/create",
        json={"path": str(repo), "name": "feature", "checkout": True},
    )
    assert create.status_code == 200 and create.json()["ok"] is True

    _commit(repo, "feature.txt", "f")
    checkout = client.post("/api/git/branch/checkout", json={"path": str(repo), "name": "main"})
    assert checkout.status_code == 200 and checkout.json()["ok"] is True

    merge = client.post("/api/git/branch/merge", json={"path": str(repo), "name": "feature"})
    assert merge.status_code == 200 and merge.json()["ok"] is True
    assert (repo / "feature.txt").exists()

    delete = client.post(
        "/api/git/branch/delete",
        json={"path": str(repo), "name": "feature", "force": True},
    )
    assert delete.status_code == 200 and delete.json()["ok"] is True


def test_graph_endpoint(client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a")
    _commit(repo, "b.txt", "b")

    resp = client.get("/api/repo/graph", params={"path": str(repo)})
    assert resp.status_code == 200
    commits = resp.json()["commits"]
    assert len(commits) == 2
    assert commits[0]["is_head"] is True
    assert commits[0]["parents"] == [commits[1]["sha"]]
    assert commits[1]["parents"] == []


def test_graph_invalid_path_error_shape(client: TestClient) -> None:
    resp = client.get("/api/repo/graph", params={"path": "/nope/not/here-xyz"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"
