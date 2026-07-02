"""Tests for Slice 4 commit-diff, branch rename/track, and amend.

Covers the git client functions and the FastAPI endpoints on real temp repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client
from git_automation.web.app import create_app


@pytest.fixture
def api() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True, check=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(path: Path, name: str = "file.txt", content: str = "hello") -> str:
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", f"add {name}")
    return _git(path, "rev-parse", "HEAD")


# --- commit-file diff -----------------------------------------------------


async def test_get_commit_diff_shows_file_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1\n")
    (repo / "f.txt").write_text("v2\n")
    sha = _commit(repo, "f.txt", "v2\n")  # overwrites & re-commits

    result = await client.get_commit_diff(str(repo), sha, "f.txt")
    assert result.binary is False
    assert "+v2" in result.diff
    assert "-v1" in result.diff


async def test_get_commit_diff_first_parent_for_merge(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "base.txt", "base\n")
    _git(repo, "checkout", "-b", "feature")
    _commit(repo, "feat.txt", "feature\n")
    _git(repo, "checkout", "main")
    _commit(repo, "main.txt", "main\n")
    _git(repo, "merge", "--no-ff", "-m", "merge feature", "feature")
    merge_sha = _git(repo, "rev-parse", "HEAD")

    # --first-parent makes a merge commit yield an ordinary diff (no crash).
    result = await client.get_commit_diff(str(repo), merge_sha, "feat.txt")
    assert result.binary is False


def test_commit_diff_endpoint(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "one\n")
    sha = _commit(repo, "f.txt", "two\n")

    resp = api.get(
        "/api/repo/commit-diff",
        params={"path": str(repo), "sha": sha, "file": "f.txt"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["file"] == "f.txt"
    assert "+two" in body["diff"]


async def test_get_commit_diff_rejects_option_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError) as excinfo:
        await client.get_commit_diff(str(repo), "--output=/tmp/x", "f.txt")
    assert excinfo.value.status_code == 400


# --- branch rename --------------------------------------------------------


async def test_rename_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "branch", "old-name")

    result = await client.rename_branch(str(repo), "old-name", "new-name")
    assert result.ok is True
    branches = _git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert "new-name" in branches
    assert "old-name" not in branches


def test_branch_rename_endpoint(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "branch", "wip")

    resp = api.post(
        "/api/git/branch/rename",
        json={"path": str(repo), "name": "wip", "new_name": "ready"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert "ready" in _git(repo, "branch", "--format=%(refname:short)").splitlines()


async def test_rename_branch_rejects_option(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError):
        await client.rename_branch(str(repo), "main", "--force")


# --- branch track ---------------------------------------------------------


def _setup_remote(tmp_path: Path) -> Path:
    """Create a bare remote with a ``feature`` branch and a clone tracking it."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], capture_output=True,
                   check=True)
    seed = _init_repo(tmp_path / "seed")
    _commit(seed, "f.txt", "seed\n")
    _git(seed, "checkout", "-b", "feature")
    _commit(seed, "g.txt", "feature\n")
    _git(seed, "checkout", "main")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "origin", "main")
    _git(seed, "push", "origin", "feature")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], capture_output=True, check=True)
    _git(clone, "config", "user.name", "Test User")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "commit.gpgsign", "false")
    _git(clone, "fetch", "origin")
    return clone


async def test_track_branch_creates_tracking_branch(tmp_path: Path) -> None:
    clone = _setup_remote(tmp_path)
    # The remote-tracking ref exists but no local 'feature' branch yet.
    assert "feature" not in _git(clone, "branch", "--format=%(refname:short)").splitlines()

    result = await client.track_branch(str(clone), "origin/feature")
    assert result.ok is True

    # A local 'feature' branch is now checked out and tracks origin/feature.
    assert _git(clone, "branch", "--show-current") == "feature"
    upstream = _git(clone, "rev-parse", "--abbrev-ref", "feature@{upstream}")
    assert upstream == "origin/feature"


def test_branch_track_endpoint(api: TestClient, tmp_path: Path) -> None:
    clone = _setup_remote(tmp_path)
    resp = api.post(
        "/api/git/branch/track",
        json={"path": str(clone), "remote_ref": "origin/feature"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _git(clone, "branch", "--show-current") == "feature"


async def test_track_branch_rejects_option(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError):
        await client.track_branch(str(repo), "--track=evil")


# --- amend ----------------------------------------------------------------


async def test_amend_commit_new_message(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")

    result = await client.amend_commit(str(repo), "reworded subject")
    assert result.ok is True
    assert _git(repo, "log", "-1", "--format=%s") == "reworded subject"


async def test_amend_commit_no_edit_folds_staged_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")
    original_subject = _git(repo, "log", "-1", "--format=%s")
    count_before = _git(repo, "rev-list", "--count", "HEAD")

    # Stage an extra file and amend without a message.
    (repo / "extra.txt").write_text("extra")
    _git(repo, "add", "extra.txt")
    result = await client.amend_commit(str(repo))
    assert result.ok is True

    # Same message, same commit count, but the new file is now in HEAD.
    assert _git(repo, "log", "-1", "--format=%s") == original_subject
    assert _git(repo, "rev-list", "--count", "HEAD") == count_before
    assert "extra.txt" in _git(repo, "show", "--name-only", "--format=", "HEAD")


def test_commit_amend_endpoint(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")

    resp = api.post(
        "/api/git/commit/amend",
        json={"path": str(repo), "message": "amended via api"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _git(repo, "log", "-1", "--format=%s") == "amended via api"


def test_commit_amend_endpoint_no_message(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")
    subject = _git(repo, "log", "-1", "--format=%s")

    resp = api.post("/api/git/commit/amend", json={"path": str(repo)})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _git(repo, "log", "-1", "--format=%s") == subject
