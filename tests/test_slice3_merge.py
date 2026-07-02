"""Slice-3 merge-lifecycle tests: a real 3-way conflict, resolve, continue, abort.

The merge router is not yet wired into the global app (the API manager registers
it separately), so these API tests mount it onto a local app. Client-level tests
drive the conflict machinery directly against real temp repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from git_automation.core.git import client
from git_automation.web.api import merge as merge_api
from git_automation.web.api import register_error_handlers


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(merge_api.router, prefix="/api")
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True, check=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _conflicting_repo(path: Path) -> Path:
    """Build a repo where merging ``feature`` into ``main`` conflicts on f.txt."""
    repo = _init_repo(path)
    (repo / "f.txt").write_text("base line\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-b", "feature")
    (repo / "f.txt").write_text("feature line\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature change")

    _git(repo, "checkout", "main")
    (repo / "f.txt").write_text("main line\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main change")
    return repo


# --- Client lifecycle -----------------------------------------------------


async def test_merge_reports_conflict(tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")
    result = await client.merge_branch(str(repo), "feature")
    assert result.ok is False
    assert result.conflicted is True
    assert result.conflicts == ["f.txt"]
    assert "CONFLICT" in result.output


async def test_merge_status_and_conflict_stages(tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")
    await client.merge_branch(str(repo), "feature")

    status = await client.merge_status(str(repo))
    assert status.merging is True
    assert status.conflicts == ["f.txt"]
    assert status.message  # MERGE_MSG was prepared

    conflict = await client.get_conflict(str(repo), "f.txt")
    assert conflict.binary is False
    assert conflict.base == "base line\n"
    assert conflict.ours == "main line\n"
    assert conflict.theirs == "feature line\n"
    assert "<<<<<<<" in (conflict.merged or "")
    assert "feature line" in (conflict.merged or "")


async def test_resolve_then_continue_completes_merge(tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")
    await client.merge_branch(str(repo), "feature")

    resolved = await client.resolve_conflict(str(repo), "f.txt", "merged line\n")
    assert resolved.ok is True

    cont = await client.merge_continue(str(repo), message="merge feature")
    assert cont.ok is True
    assert (repo / "f.txt").read_text() == "merged line\n"
    # The merge commit has two parents.
    parents = _git(repo, "rev-list", "--parents", "-n1", "HEAD").split()
    assert len(parents) == 3
    after = await client.merge_status(str(repo))
    assert after.merging is False


async def test_merge_continue_errors_while_conflicts_remain(tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")
    await client.merge_branch(str(repo), "feature")
    with pytest.raises(Exception) as exc:  # GitAutomationError
        await client.merge_continue(str(repo))
    assert getattr(exc.value, "code", "") == "unresolved_conflicts"


async def test_merge_abort_restores(tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")
    head_before = _git(repo, "rev-parse", "HEAD")
    await client.merge_branch(str(repo), "feature")

    aborted = await client.merge_abort(str(repo))
    assert aborted.ok is True
    assert (repo / "f.txt").read_text() == "main line\n"
    assert _git(repo, "rev-parse", "HEAD") == head_before
    status = await client.merge_status(str(repo))
    assert status.merging is False


async def test_get_conflict_missing_stage_does_not_crash(tmp_path: Path) -> None:
    """A file added on only one side has no base/ours stages -> None, no crash."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")

    _git(repo, "checkout", "-b", "feature")
    (repo / "added.txt").write_text("from feature\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add on feature")

    _git(repo, "checkout", "main")
    (repo / "added.txt").write_text("from main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add on main")

    result = await client.merge_branch(str(repo), "feature")
    assert result.conflicted is True

    conflict = await client.get_conflict(str(repo), "added.txt")
    assert conflict.base is None  # no common ancestor version
    assert conflict.ours == "from main\n"
    assert conflict.theirs == "from feature\n"


# --- API lifecycle --------------------------------------------------------


def test_merge_conflict_lifecycle_via_api(api: TestClient, tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")

    merged = api.post("/api/git/merge", json={"path": str(repo), "name": "feature"})
    assert merged.status_code == 200
    body = merged.json()
    assert body["conflicted"] is True and body["conflicts"] == ["f.txt"]

    status = api.get("/api/repo/merge-status", params={"path": str(repo)})
    assert status.json()["merging"] is True

    conflict = api.get("/api/repo/conflict", params={"path": str(repo), "file": "f.txt"})
    assert conflict.json()["ours"] == "main line\n"

    # Continue while unresolved -> 409.
    early = api.post("/api/git/merge/continue", json={"path": str(repo)})
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "unresolved_conflicts"

    resolve = api.post(
        "/api/git/resolve",
        json={"path": str(repo), "file": "f.txt", "content": "resolved\n"},
    )
    assert resolve.status_code == 200 and resolve.json()["ok"] is True

    cont = api.post("/api/git/merge/continue", json={"path": str(repo), "message": "done"})
    assert cont.status_code == 200 and cont.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "resolved\n"


def test_merge_abort_via_api(api: TestClient, tmp_path: Path) -> None:
    repo = _conflicting_repo(tmp_path / "repo")
    api.post("/api/git/merge", json={"path": str(repo), "name": "feature"})
    abort = api.post("/api/git/merge/abort", json={"path": str(repo)})
    assert abort.status_code == 200 and abort.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "main line\n"
